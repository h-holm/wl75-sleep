"""Bluetooth access to the lamp: scan, inspect, and a session that sends one request at a time.

Rules learned from the device (see `docs/protocol.md`):

- every connection starts with the handshake `00 75`, otherwise the lamp drops the link;
- every command is answered, and commands sent faster than the answers come back garble the replies, so only one
  request is in flight at any time;
- every connection ends with a disconnect, also on error and timeout.
"""

import asyncio
import contextlib
import statistics
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from loguru import logger
lazy from bleak.backends.device import BLEDevice
lazy from bleak.backends.scanner import AdvertisementData

from wl75_sleep import protocol
from wl75_sleep.protocol import SleepLight, SleepSound, SleepState


ANSWER_TIMEOUT_SECONDS = 2.0
# One run makes up to three attempts and waits this long before the second and the third.
RETRY_DELAYS_SECONDS = (2.0, 5.0)
BLUETOOTH_READY_TIMEOUT_SECONDS = 20.0
# The lamp is at the edge of reception: a 60-second scan can see as few as two advertisements.
FIND_TIMEOUT_SECONDS = 45.0
CONNECT_TIMEOUT_SECONDS = 20.0

NotifyCallback = Callable[[object, bytearray], Awaitable[None] | None]


class LampError(Exception):
    """Communication with the lamp failed."""


class LampNotFoundError(LampError):
    """The lamp is not advertising. Usually the phone app holds the connection."""


class LampRefusedError(LampError):
    """The link works, but the lamp did not do what was asked. Trying again would not help."""


class LampRejectedError(LampRefusedError):
    """The lamp answered a command with something other than `<answer code> 01`."""


class LampVerificationError(LampRefusedError):
    """The status after a command sequence is not what the sequence should have produced."""


class Transport(Protocol):
    """The part of `bleak.BleakClient` that a session uses. `tests/fake_lamp.py` implements it too."""

    async def connect(self) -> None:
        """Open the connection."""
        ...

    async def disconnect(self) -> None:
        """Close the connection."""
        ...

    async def start_notify(self, char_specifier: str, callback: NotifyCallback) -> None:
        """Subscribe to notifications."""
        ...

    async def write_gatt_char(self, char_specifier: str, data: bytes, response: bool | None = None) -> None:  # noqa: FBT001
        """Write a characteristic."""
        ...


class LampSession:
    """An open connection to the lamp. Use `open_session` to get one."""

    def __init__(self, transport: Transport) -> None:
        """Wrap a connected transport. Nothing is sent until `handshake`."""
        self._transport = transport
        self._lock = asyncio.Lock()
        self._pending: tuple[int, asyncio.Future[bytes]] | None = None
        self.unprompted: list[bytes] = []

    async def handshake(self) -> None:
        """Subscribe to notifications and send the handshake.

        Raises:
            LampError: If the lamp does not answer `F0 75`.
        """
        await self._transport.start_notify(protocol.NOTIFY_UUID, self._on_notify)
        answer = await self.request(protocol.handshake())
        if answer != protocol.HANDSHAKE_ANSWER:
            message = f"Unexpected handshake answer {answer.hex(' ')}."
            raise LampError(message)

    async def request(self, payload: bytes) -> bytes:
        """Send one allowlisted request and return the payload of its answer.

        Raises:
            LampError: If no answer arrives within `ANSWER_TIMEOUT_SECONDS`.
            ValueError: If `payload` is not an allowlisted request.
        """
        frame = protocol.build_frame(payload)
        async with self._lock:
            future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
            self._pending = (protocol.response_code(payload[0]), future)
            try:
                logger.debug(f"-> {frame.hex(' ')}")
                await self._transport.write_gatt_char(protocol.WRITE_UUID, frame, response=False)
                async with asyncio.timeout(ANSWER_TIMEOUT_SECONDS):
                    return await future
            except TimeoutError as error:
                message = f"No answer to {payload.hex(' ')} within {ANSWER_TIMEOUT_SECONDS:g} s."
                raise LampError(message) from error
            finally:
                self._pending = None

    async def command(self, payload: bytes) -> None:
        """Send one allowlisted command and check that the lamp accepts it.

        Raises:
            LampRejectedError: If the answer is not `<answer code> 01`.
        """
        answer = await self.request(payload)
        if not protocol.is_ack(payload, answer):
            message = f"The lamp rejected {payload.hex(' ')}: {answer.hex(' ')}."
            raise LampRejectedError(message)

    async def status(self) -> SleepState:
        """Query the Sleep status."""
        try:
            return SleepState.from_payload(await self.request(protocol.query_sleep()))
        except protocol.ProtocolError as error:
            raise LampError(str(error)) from error

    def _on_notify(self, _characteristic: object, data: bytearray) -> None:
        logger.debug(f"<- {data.hex(' ')}")
        for payload in protocol.parse_frames(bytes(data)):
            if self._pending is not None:
                code, future = self._pending
                if payload[0] == code and not future.done():
                    future.set_result(payload)
                    continue
            logger.debug(f"Unprompted: {payload.hex(' ')}")
            self.unprompted.append(payload)


@contextlib.asynccontextmanager
async def open_session(transport: Transport) -> AsyncGenerator[LampSession]:
    """Connect, handshake, yield the session, and always disconnect.

    Raises:
        LampError: If the connection, the handshake or a request inside the block fails.
    """
    try:
        try:
            await transport.connect()
            session = LampSession(transport)
            await session.handshake()
            yield session
        finally:
            # Also after a failed `connect`: it may have left a half-open link.
            with contextlib.suppress(BleakError, OSError, TimeoutError):
                await transport.disconnect()
    except (BleakError, OSError, TimeoutError) as error:
        message = f"Bluetooth failure: {str(error) or type(error).__name__}"
        raise LampError(message) from error


async def run_on_lamp[T](
    device_uuid: str,
    operation: Callable[[LampSession], Awaitable[T]],
    on_found: Callable[[str], None] | None = None,
) -> T:
    """Find the lamp, open a session, run `operation` and disconnect. Makes up to three attempts.

    `operation` must be safe to repeat: every attempt runs it from the start on a new connection. `on_found` receives
    the address the lamp was found under, which differs from `device_uuid` if only the name matched.

    Raises:
        LampNotFoundError: If the lamp did not advertise on the last attempt.
        LampRefusedError: If the lamp rejected a command or ended in an unexpected state. Not retried.
        LampError: If the last attempt failed for another reason.
    """
    attempts = len(RETRY_DELAYS_SECONDS) + 1
    for attempt, delay in enumerate(RETRY_DELAYS_SECONDS, start=1):
        try:
            return await _attempt(device_uuid, operation, on_found)
        except LampRefusedError:
            raise
        except LampNotFoundError as error:
            logger.warning(f"Attempt {attempt} of {attempts}: the lamp is not advertising. {error}")
        except LampError as error:
            logger.warning(f"Attempt {attempt} of {attempts}: the connection failed. {error}")
        await asyncio.sleep(delay)
    return await _attempt(device_uuid, operation, on_found)


async def _attempt[T](
    device_uuid: str,
    operation: Callable[[LampSession], Awaitable[T]],
    on_found: Callable[[str], None] | None,
) -> T:
    device = await find_lamp(device_uuid)
    if on_found is not None:
        on_found(device.address)
    async with open_session(make_transport(device)) as session:
        return await operation(session)


def clamp_brightness(brightness: int) -> int:
    """Bring a brightness reported by the lamp into the range the app can send."""
    return max(protocol.MIN_BRIGHTNESS, min(protocol.MAX_BRIGHTNESS, brightness))


async def start_sleep(  # noqa: PLR0913
    session: LampSession,
    *,
    minutes: int,
    light: SleepLight,
    sound: SleepSound,
    volume: int,
    fade_out: bool,
) -> SleepState:
    """Start the Sleep function, or reconfigure it if it already runs, and return the verified status.

    Sequence: status, `1E 01` only if Sleep is off, `1F 01`, `1F 02`, status. The brightness sent is the one the lamp
    reports, so its stored setting stays as it is.

    Raises:
        LampVerificationError: If the final status does not show the requested run.
    """
    before = await session.status()
    if not before.is_on:
        await session.command(protocol.switch_sleep(on=True))
    await session.command(protocol.set_sleep_light(minutes, light, clamp_brightness(before.brightness)))
    await session.command(protocol.set_sleep_sound(sound, volume, fade_out=fade_out))
    after = await session.status()
    _verify(after, is_on=True, minutes=minutes, light=light, sound=sound, volume=volume, fade_out=fade_out)
    return after


async def extend_sleep(session: LampSession, *, minutes: int, light: SleepLight) -> SleepState:
    """Restart the countdown of a running Sleep function at `minutes` and return the verified status.

    Sequence: status, then `1F 01` and another status if Sleep runs. If Sleep is off, nothing is sent and the returned
    status says so: this function never starts a run.

    Raises:
        LampVerificationError: If the final status does not show the new countdown.
    """
    before = await session.status()
    if not before.is_on:
        return before
    await session.command(protocol.set_sleep_light(minutes, light, clamp_brightness(before.brightness)))
    after = await session.status()
    _verify(after, is_on=True, minutes=minutes, light=light)
    return after


async def stop_sleep(session: LampSession) -> SleepState:
    """Stop the Sleep function and return the verified status. Sequence: `1E 00`, status.

    Raises:
        LampVerificationError: If the final status still says on.
    """
    await session.command(protocol.switch_sleep(on=False))
    after = await session.status()
    _verify(after, is_on=False)
    return after


def _verify(state: SleepState, *, is_on: bool, minutes: int | None = None, **expected: int) -> None:
    """Compare `state` with what a sequence should have produced.

    The lamp reports the remaining minutes rounded sometimes down and sometimes up, so one minute less than sent passes.
    """
    problems = [
        f"{name} is {getattr(state, name)}, expected {value}"
        for name, value in {"is_on": is_on, **expected}.items()
        if getattr(state, name) != value
    ]
    if minutes is not None and state.minutes not in {minutes - 1, minutes}:
        problems.append(f"minutes is {state.minutes}, expected {minutes - 1} or {minutes}")
    if problems:
        message = f"The lamp is not in the expected state: {'; '.join(problems)}. It reports: {state.describe()}"
        raise LampVerificationError(message)


@dataclass(slots=True)
class Sighting:
    """Advertisements seen from one device during a scan."""

    address: str
    name: str
    rssi_samples: list[int] = field(default_factory=list)
    service_uuids: list[str] = field(default_factory=list)
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)
    service_data: dict[str, bytes] = field(default_factory=dict)

    def record(self, advertisement: AdvertisementData) -> None:
        """Add one advertisement."""
        self.rssi_samples.append(advertisement.rssi)
        self.service_uuids = sorted(set(self.service_uuids) | set(advertisement.service_uuids))
        self.manufacturer_data.update(advertisement.manufacturer_data)
        self.service_data.update(advertisement.service_data)

    def describe(self) -> str:
        """Return a multi-line summary with RSSI statistics and the advertisement data."""
        samples = self.rssi_samples
        rssi = f"min {min(samples)}, median {statistics.median(samples):g}, max {max(samples)} dBm"
        lines = [f"{self.address}  name={self.name!r}", f"    rssi: {len(samples)} samples, {rssi}"]
        if self.service_uuids:
            lines.append(f"    services: {', '.join(self.service_uuids)}")
        lines += [
            f"    manufacturer 0x{company:04x}: {payload.hex(' ')}"
            for company, payload in sorted(self.manufacturer_data.items())
        ]
        lines += [f"    service data {uuid}: {payload.hex(' ')}" for uuid, payload in sorted(self.service_data.items())]
        return "\n".join(lines)


def is_lamp(device: BLEDevice, advertisement: AdvertisementData) -> bool:
    """Return `True` if the advertisement comes from a WL75. The LightUp app matches on the name prefix too."""
    name = advertisement.local_name or device.name or ""
    return name.startswith(protocol.ADVERTISED_NAME)


def record_sighting(sightings: dict[str, Sighting], device: BLEDevice, advertisement: AdvertisementData) -> None:
    """Add an advertisement to `sightings` if it comes from a WL75."""
    if not is_lamp(device, advertisement):
        return
    name = advertisement.local_name or device.name or ""
    sightings.setdefault(device.address, Sighting(device.address, name)).record(advertisement)


async def scan(seconds: float) -> list[Sighting]:  # pragma: no cover
    """Listen for advertisements for `seconds` and return the WL75s seen, strongest first. Connects to nothing."""
    sightings: dict[str, Sighting] = {}
    scanner = BleakScanner(detection_callback=lambda device, adv: record_sighting(sightings, device, adv))
    try:
        # `start` waits for macOS to report the Bluetooth state, which never happens while a permission prompt is open.
        async with asyncio.timeout(BLUETOOTH_READY_TIMEOUT_SECONDS):
            await scanner.start()
    except TimeoutError as error:
        message = (
            f"Bluetooth did not become ready within {BLUETOOTH_READY_TIMEOUT_SECONDS:g} s. macOS may be showing a "
            "permission prompt for the terminal: answer it and run the command again."
        )
        raise LampError(message) from error
    except BleakError as error:
        raise LampError(str(error)) from error
    try:
        await asyncio.sleep(seconds)
    finally:
        await scanner.stop()
    return sorted(sightings.values(), key=lambda sighting: statistics.median(sighting.rssi_samples), reverse=True)


async def find_lamp(device_uuid: str) -> BLEDevice:  # pragma: no cover
    """Find the lamp by its saved CoreBluetooth UUID or by its name, whichever advertisement arrives first.

    Raises:
        LampNotFoundError: If no WL75 is advertising.
        LampError: If Bluetooth is off or not permitted.
    """

    def matches(device: BLEDevice, advertisement: AdvertisementData) -> bool:
        return device.address.casefold() == device_uuid.casefold() or is_lamp(device, advertisement)

    try:
        device = await BleakScanner.find_device_by_filter(matches, timeout=FIND_TIMEOUT_SECONDS)
    except BleakError as error:
        raise LampError(str(error)) from error
    if device is None:
        message = (
            f"No WL75 advertised within {FIND_TIMEOUT_SECONDS:g} s. Either the signal is too weak, or the LightUp app "
            "on the phone holds the connection: a connected lamp does not advertise."
        )
        raise LampNotFoundError(message)
    return device


def make_transport(device: BLEDevice) -> Transport:  # pragma: no cover
    """Return a `BleakClient` for `device`."""
    return BleakClient(device, timeout=CONNECT_TIMEOUT_SECONDS)


@dataclass(frozen=True, slots=True)
class Inspection:
    """What a connection without any command reveals about the lamp."""

    address: str
    # Service UUID -> characteristic UUID -> properties.
    services: dict[str, dict[str, list[str]]]
    software_revision: str
    firmware_revision: str
    hardware_revision: str
    notifications: list[bytes]

    def layout_problems(self) -> list[str]:
        """Compare services and characteristics with the published protocol. An empty list means they match."""
        characteristics = self.services.get(protocol.SERVICE_UUID)
        if characteristics is None:
            return [f"Service {protocol.SERVICE_UUID} is missing."]
        problems = []
        for label, uuid, needed in (
            ("Write", protocol.WRITE_UUID, "write-without-response"),
            ("Notify", protocol.NOTIFY_UUID, "notify"),
        ):
            if uuid not in characteristics:
                problems.append(f"{label} characteristic {uuid} is missing.")
            elif needed not in characteristics[uuid]:
                problems.append(
                    f"{label} characteristic {uuid} lacks {needed!r}. It has: {', '.join(characteristics[uuid])}."
                )
        return problems

    def firmware_matches(self) -> bool:
        """Return `True` if all three revision strings equal those of the documented unit."""
        return (self.software_revision, self.firmware_revision, self.hardware_revision) == (
            protocol.DOCUMENTED_SOFTWARE_REVISION,
            protocol.DOCUMENTED_FIRMWARE_REVISION,
            protocol.DOCUMENTED_HARDWARE_REVISION,
        )

    def describe(self) -> str:
        """Return the full report: GATT layout, comparison with the protocol facts, and the firmware gate table."""
        lines = [f"Lamp {self.address}", "", "Services and characteristics:"]
        for service, characteristics in self.services.items():
            lines.append(f"  {service}")
            lines += [f"    {uuid}  [{', '.join(properties)}]" for uuid, properties in characteristics.items()]
        problems = self.layout_problems()
        lines += ["", "Layout compared with the protocol facts:"]
        lines += [f"  MISMATCH: {problem}" for problem in problems] or [
            "  OK: service, write characteristic (write-without-response) and notify characteristic match."
        ]
        lines += ["", f"Notifications while listening, with no command sent: {len(self.notifications)}"]
        lines += [f"  {data.hex(' ')}" for data in self.notifications]
        lines += ["", "Firmware gate:", f"  {'':<10}{'this lamp':<16}{'documented':<16}"]
        for label, actual, documented in (
            ("software", self.software_revision, protocol.DOCUMENTED_SOFTWARE_REVISION),
            ("firmware", self.firmware_revision, protocol.DOCUMENTED_FIRMWARE_REVISION),
            ("hardware", self.hardware_revision, protocol.DOCUMENTED_HARDWARE_REVISION),
        ):
            verdict = "match" if actual == documented else "DIFFERENT"
            lines.append(f"  {label:<10}{actual:<16}{documented:<16}{verdict}")
        return "\n".join(lines)


async def inspect(device: BLEDevice, listen_seconds: float) -> Inspection:  # pragma: no cover
    """Connect without sending any command: list the GATT layout, read the revision strings, listen, disconnect.

    Raises:
        LampError: If the connection or a read fails.
    """
    notifications: list[bytes] = []
    client = BleakClient(device, timeout=CONNECT_TIMEOUT_SECONDS)
    try:
        try:
            await client.connect()
            services = {
                service.uuid: {
                    characteristic.uuid: [str(name) for name in characteristic.properties]
                    for characteristic in service.characteristics
                }
                for service in client.services
            }
            revisions = [
                (await client.read_gatt_char(uuid)).decode("ascii", errors="replace").strip("\x00 ")
                for uuid in (
                    protocol.SOFTWARE_REVISION_UUID,
                    protocol.FIRMWARE_REVISION_UUID,
                    protocol.HARDWARE_REVISION_UUID,
                )
            ]
            # Without the handshake the lamp may drop the link while we listen. That is expected, not an error.
            with contextlib.suppress(BleakError):
                await client.start_notify(protocol.NOTIFY_UUID, lambda _, data: notifications.append(bytes(data)))
                await asyncio.sleep(listen_seconds)
            if not client.is_connected:
                logger.info("The lamp closed the connection while listening (no handshake was sent).")
        finally:
            with contextlib.suppress(BleakError, OSError, TimeoutError):
                await client.disconnect()
    except (BleakError, OSError, TimeoutError) as error:
        message = f"Bluetooth failure: {str(error) or type(error).__name__}"
        raise LampError(message) from error
    return Inspection(device.address, services, *revisions, notifications=notifications)
