import asyncio

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

from wl75_sleep import client, protocol
from wl75_sleep.client import (
    Inspection,
    LampError,
    LampNotFoundError,
    LampRejectedError,
    LampSession,
    LampVerificationError,
    Sighting,
    open_session,
)
from wl75_sleep.protocol import SleepLight, SleepSound, SleepState

from .fake_lamp import FakeLamp, frame


@pytest.fixture
def lamp() -> FakeLamp:
    return FakeLamp()


@pytest.fixture(autouse=True)
def short_answer_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "ANSWER_TIMEOUT_SECONDS", 0.05)


async def test_status_sends_handshake_then_query_and_disconnects(lamp: FakeLamp) -> None:
    async with open_session(lamp) as session:
        state = await session.status()

    assert lamp.written == [bytes.fromhex("00 75"), bytes.fromhex("1D")]
    assert not state.is_on
    assert state.minutes == 2
    assert (lamp.connects, lamp.disconnects) == (1, 1)
    assert not lamp.is_connected


async def test_start_sequence_against_the_fake(lamp: FakeLamp) -> None:
    async with open_session(lamp) as session:
        await session.command(protocol.switch_sleep(on=True))
        await session.command(protocol.set_sleep_light(60, SleepLight.OFF, 10))
        await session.command(protocol.set_sleep_sound(SleepSound.RAIN, 5, fade_out=False))
        state = await session.status()

    assert state.is_on
    assert (state.minutes, state.light, state.sound, state.volume, state.fade_out) == (60, 0, 5, 5, False)


async def test_configuration_is_rejected_while_sleep_is_off(lamp: FakeLamp) -> None:
    with pytest.raises(LampRejectedError, match="rejected 1f 01 3c 00 0a: ef 00"):
        async with open_session(lamp) as session:
            await session.command(protocol.set_sleep_light(60, SleepLight.OFF, 10))
    assert lamp.disconnects == 1


async def _status(lamp: FakeLamp) -> SleepState:
    async with open_session(lamp) as session:
        return await session.status()


async def test_wrong_handshake_answer_fails_and_disconnects(lamp: FakeLamp) -> None:
    lamp.canned_answers[protocol.CMD_HANDSHAKE] = bytes.fromhex("F0 00")
    with pytest.raises(LampError, match="Unexpected handshake answer f0 00"):
        await _status(lamp)
    assert lamp.written == [bytes.fromhex("00 75")]
    assert lamp.disconnects == 1


async def test_missing_answer_times_out_and_disconnects(lamp: FakeLamp) -> None:
    lamp.unanswered.add(protocol.CMD_SLEEP_STATUS)
    with pytest.raises(LampError, match="No answer to 1d"):
        await _status(lamp)
    assert lamp.disconnects == 1


async def test_malformed_status_answer_is_a_lamp_error(lamp: FakeLamp) -> None:
    lamp.canned_answers[protocol.CMD_SLEEP_STATUS] = bytes.fromhex("ED 00 02")
    with pytest.raises(LampError, match="seven data bytes"):
        await _status(lamp)
    assert lamp.disconnects == 1


async def test_connect_failure_is_a_lamp_error_and_still_disconnects(lamp: FakeLamp) -> None:
    lamp.connect_error = BleakError("device disappeared")
    with pytest.raises(LampError, match="device disappeared"):
        await _status(lamp)
    assert lamp.written == []
    assert lamp.disconnects == 1


async def test_an_error_without_a_message_is_named_by_its_type(lamp: FakeLamp) -> None:
    lamp.connect_error = TimeoutError()
    with pytest.raises(LampError, match="Bluetooth failure: TimeoutError"):
        await _status(lamp)


async def test_write_failure_is_a_lamp_error_and_disconnects(lamp: FakeLamp) -> None:
    lamp.write_errors[protocol.CMD_SLEEP_STATUS] = OSError("link lost")
    with pytest.raises(LampError, match="link lost"):
        await _status(lamp)
    assert lamp.disconnects == 1


async def test_error_inside_the_block_still_disconnects(lamp: FakeLamp) -> None:
    with pytest.raises(RuntimeError):
        async with open_session(lamp):
            raise RuntimeError
    assert lamp.disconnects == 1


async def test_session_refuses_requests_off_the_allowlist(lamp: FakeLamp) -> None:
    async with open_session(lamp) as session:
        with pytest.raises(ValueError, match="allowlist"):
            await session.request(bytes.fromhex("03 00 00 00 01 01 1A"))  # Set the clock.
    assert lamp.written == [bytes.fromhex("00 75")]


async def test_unprompted_and_bundled_notifications(lamp: FakeLamp) -> None:
    async with open_session(lamp) as session:
        lamp.push(frame(protocol.SLEEP_TIMER_FINISHED))

        # An answer that arrives bundled with an unprompted frame and a truncated one is still matched.
        lamp.unanswered.add(protocol.CMD_SLEEP_STATUS)
        request = asyncio.ensure_future(session.request(protocol.query_sleep()))
        await asyncio.sleep(0)
        lamp.push(frame(bytes.fromhex("EE 01")))
        lamp.push(b"\xfe\xef\x03" + frame(protocol.SLEEP_TIMER_FINISHED) + frame(bytes([0xED, *lamp.sleep])))
        answer = await request

    assert answer == bytes.fromhex("ED 00 02 01 63 00 06 01")
    assert session.unprompted == [protocol.SLEEP_TIMER_FINISHED, bytes.fromhex("EE 01"), protocol.SLEEP_TIMER_FINISHED]


# The three sequences.


async def _start_rain(session: LampSession) -> SleepState:
    return await client.start_sleep(
        session, minutes=60, light=SleepLight.OFF, sound=SleepSound.RAIN, volume=5, fade_out=False
    )


async def _start(lamp: FakeLamp) -> SleepState:
    async with open_session(lamp) as session:
        return await _start_rain(session)


async def test_start_sends_the_documented_sequence(lamp: FakeLamp) -> None:
    async with open_session(lamp) as session:
        state = await _start_rain(session)

    assert [payload.hex(" ") for payload in lamp.written] == [
        "00 75",
        "1d",
        "1e 01",
        "1f 01 3c 00 63",  # Brightness 99 is echoed from the status.
        "1f 02 05 05 00",
        "1d",
    ]
    assert state.is_on
    assert state.describe() == "sleep on, 60 min left | light off, brightness 99 | sound rain, volume 5, fade off"


async def test_start_does_not_switch_on_a_running_sleep(lamp: FakeLamp) -> None:
    lamp.sleep[:2] = bytes([1, 12])
    async with open_session(lamp) as session:
        await _start_rain(session)

    assert bytes.fromhex("1E 01") not in lamp.written
    assert lamp.sleep[1] == 60


async def test_start_clamps_a_faded_brightness_into_the_app_range(lamp: FakeLamp) -> None:
    lamp.sleep[3] = 3
    async with open_session(lamp) as session:
        await _start_rain(session)

    assert bytes.fromhex("1F 01 3C 00 0A") in lamp.written


async def test_start_fails_verification_if_the_lamp_ignores_the_sound(lamp: FakeLamp) -> None:
    lamp.sleep[0] = 1
    lamp.canned_answers[protocol.CMD_SLEEP_STATUS] = bytes.fromhex("ED 01 3C 00 63 00 05 00")
    with pytest.raises(LampVerificationError, match="sound is 0, expected 5"):
        await _start(lamp)
    assert lamp.disconnects == 1


@pytest.mark.parametrize(("reported", "accepted"), [(60, True), (59, True), (58, False), (2, False)])
async def test_verification_accepts_one_minute_of_rounding(lamp: FakeLamp, reported: int, accepted: bool) -> None:
    lamp.sleep[0] = 1
    lamp.canned_answers[protocol.CMD_SLEEP_STATUS] = bytes([0xED, 1, reported, 0, 99, 5, 5, 0])
    if accepted:
        assert (await _start(lamp)).minutes == reported
    else:
        with pytest.raises(LampVerificationError, match="expected 59 or 60"):
            await _start(lamp)


async def test_extend_restarts_the_countdown_and_keeps_the_sound(lamp: FakeLamp) -> None:
    lamp.sleep = bytearray.fromhex("01 0E 00 2D 05 05 00")  # Running, 14 minutes left, brightness faded to 45.
    async with open_session(lamp) as session:
        state = await client.extend_sleep(session, minutes=60, light=SleepLight.OFF)

    assert [payload.hex(" ") for payload in lamp.written] == ["00 75", "1d", "1f 01 3c 00 2d", "1d"]
    assert (state.is_on, state.minutes, state.sound, state.volume) == (True, 60, SleepSound.RAIN, 5)


async def test_extend_sends_nothing_when_sleep_is_off(lamp: FakeLamp) -> None:
    async with open_session(lamp) as session:
        state = await client.extend_sleep(session, minutes=60, light=SleepLight.OFF)

    assert not state.is_on
    assert lamp.written == [bytes.fromhex("00 75"), bytes.fromhex("1D")]


async def test_stop_switches_off_and_verifies(lamp: FakeLamp) -> None:
    lamp.sleep[:2] = bytes([1, 30])
    async with open_session(lamp) as session:
        state = await client.stop_sleep(session)

    assert [payload.hex(" ") for payload in lamp.written] == ["00 75", "1e 00", "1d"]
    assert not state.is_on
    assert state.minutes == 2


async def test_stop_fails_verification_if_sleep_keeps_running(lamp: FakeLamp) -> None:
    lamp.canned_answers[protocol.CMD_SLEEP_STATUS] = bytes.fromhex("ED 01 1E 00 63 05 05 00")
    with pytest.raises(LampVerificationError, match="is_on is True, expected False"):
        async with open_session(lamp) as session:
            await client.stop_sleep(session)


# Retries.


@pytest.fixture
def hardware(lamp: FakeLamp, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Route `run_on_lamp` to the fake lamp without waiting between attempts. Returns the UUIDs searched for."""
    searched: list[str] = []

    async def find_lamp(device_uuid: str) -> BLEDevice:
        searched.append(device_uuid)
        return _device()

    monkeypatch.setattr(client, "find_lamp", find_lamp)
    monkeypatch.setattr(client, "make_transport", lambda _device: lamp)
    monkeypatch.setattr(client, "RETRY_DELAYS_SECONDS", (0, 0))
    return searched


async def test_run_on_lamp_succeeds_first_time(lamp: FakeLamp, hardware: list[str]) -> None:
    found: list[str] = []
    state = await client.run_on_lamp("saved-uuid", LampSession.status, found.append)

    assert not state.is_on
    assert hardware == ["saved-uuid"]
    assert found == ["11111111-2222-3333-4444-555555555555"]
    assert (lamp.connects, lamp.disconnects) == (1, 1)


async def test_run_on_lamp_retries_a_failed_connection(lamp: FakeLamp, hardware: list[str]) -> None:
    failures = [BleakError("failed to connect"), OSError("link lost")]

    async def connect() -> None:
        lamp.connects += 1
        if failures:
            raise failures.pop(0)
        lamp.is_connected = True

    lamp.connect = connect
    state = await client.run_on_lamp("saved-uuid", LampSession.status)

    assert not state.is_on
    assert len(hardware) == 3
    assert (lamp.connects, lamp.disconnects) == (3, 3)


async def test_run_on_lamp_gives_up_after_three_attempts(lamp: FakeLamp, hardware: list[str]) -> None:
    lamp.unanswered.add(protocol.CMD_HANDSHAKE)
    with pytest.raises(LampError, match="No answer to 00 75"):
        await client.run_on_lamp("saved-uuid", LampSession.status)

    assert len(hardware) == 3
    assert (lamp.connects, lamp.disconnects) == (3, 3)


async def test_run_on_lamp_retries_when_the_lamp_is_not_advertising(
    lamp: FakeLamp, hardware: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def find_lamp(device_uuid: str) -> BLEDevice:
        hardware.append(device_uuid)
        if len(hardware) < 3:
            message = "No WL75 advertised."
            raise LampNotFoundError(message)
        return _device()

    monkeypatch.setattr(client, "find_lamp", find_lamp)
    await client.run_on_lamp("saved-uuid", LampSession.status)

    assert len(hardware) == 3
    assert lamp.connects == 1


async def test_run_on_lamp_does_not_retry_a_rejected_command(lamp: FakeLamp, hardware: list[str]) -> None:
    async def configure_without_starting(session: LampSession) -> None:
        await session.command(protocol.set_sleep_light(60, SleepLight.OFF, 10))

    with pytest.raises(LampRejectedError):
        await client.run_on_lamp("saved-uuid", configure_without_starting)

    assert len(hardware) == 1
    assert (lamp.connects, lamp.disconnects) == (1, 1)


async def test_run_on_lamp_repeats_the_whole_sequence_on_a_new_connection(lamp: FakeLamp, hardware: list[str]) -> None:
    """The answer to `1F 02` gets lost once. The second attempt finds Sleep running and must not switch it on again."""
    original = lamp.write_gatt_char
    lost = [protocol.set_sleep_sound(SleepSound.RAIN, 5, fade_out=False)]

    async def write_gatt_char(char_specifier: str, data: bytes, response: bool | None = None) -> None:
        (payload,) = protocol.parse_frames(data)
        if payload in lost:
            lost.remove(payload)
            lamp.unanswered.add(protocol.CMD_SLEEP_CONFIG)
        else:
            lamp.unanswered.discard(protocol.CMD_SLEEP_CONFIG)
        await original(char_specifier, data, response)

    lamp.write_gatt_char = write_gatt_char

    state = await client.run_on_lamp("saved-uuid", _start_rain)

    assert state.is_on
    assert len(hardware) == 2
    assert lamp.written.count(bytes.fromhex("1E 01")) == 1
    assert (lamp.connects, lamp.disconnects) == (2, 2)


def test_clamp_brightness() -> None:
    assert [client.clamp_brightness(value) for value in (0, 9, 10, 55, 100, 101, 255)] == [
        10,
        10,
        10,
        55,
        100,
        100,
        100,
    ]


# Scan bookkeeping.


def _advertisement(rssi: int, local_name: str | None = "WL75") -> AdvertisementData:
    return AdvertisementData(
        local_name=local_name,
        manufacturer_data={0x0611: b"\x01\x02"},
        service_data={"0000feaa-0000-1000-8000-00805f9b34fb": b"\x03"},
        service_uuids=["00007336-0000-1000-8000-00805f9b34fb"],
        tx_power=None,
        rssi=rssi,
        platform_data=(),
    )


def _device(address: str = "11111111-2222-3333-4444-555555555555", name: str | None = "WL75") -> BLEDevice:
    return BLEDevice(address, name, details=None)


def test_record_sighting_collects_rssi_per_lamp() -> None:
    sightings: dict[str, Sighting] = {}
    for rssi in (-70, -60, -80):
        client.record_sighting(sightings, _device(), _advertisement(rssi))
    client.record_sighting(sightings, _device("other", "Headphones"), _advertisement(-40, local_name="Headphones"))

    (sighting,) = sightings.values()
    assert sighting.rssi_samples == [-70, -60, -80]
    assert sighting.describe() == (
        "11111111-2222-3333-4444-555555555555  name='WL75'\n"
        "    rssi: 3 samples, min -80, median -70, max -60 dBm\n"
        "    services: 00007336-0000-1000-8000-00805f9b34fb\n"
        "    manufacturer 0x0611: 01 02\n"
        "    service data 0000feaa-0000-1000-8000-00805f9b34fb: 03"
    )


@pytest.mark.parametrize(
    ("device_name", "local_name", "expected"),
    [
        ("WL75", None, True),
        (None, "WL75", True),
        ("WL75-1A2B", None, True),
        ("TL100", "TL100", False),
        (None, None, False),
    ],
)
def test_is_lamp_matches_the_name_prefix(device_name: str | None, local_name: str | None, expected: bool) -> None:
    assert client.is_lamp(_device(name=device_name), _advertisement(-60, local_name=local_name)) is expected


# Inspection report.


def _inspection(**changes: object) -> Inspection:
    values = {
        "address": "11111111-2222-3333-4444-555555555555",
        "services": {
            protocol.DEVICE_INFORMATION_SERVICE_UUID: {protocol.SOFTWARE_REVISION_UUID: ["read"]},
            protocol.SERVICE_UUID: {
                protocol.WRITE_UUID: ["write-without-response"],
                protocol.NOTIFY_UUID: ["notify"],
            },
        },
        "software_revision": "V1.39.0518",
        "firmware_revision": "V2.0.0518",
        "hardware_revision": "V4",
        "notifications": [],
    }
    return Inspection(**(values | changes))  # ty: ignore[invalid-argument-type]


def test_inspection_of_the_documented_unit_matches() -> None:
    inspection = _inspection()
    assert inspection.layout_problems() == []
    assert inspection.firmware_matches()
    report = inspection.describe()
    assert "OK: service, write characteristic" in report
    assert "software  V1.39.0518      V1.39.0518      match" in report
    assert "DIFFERENT" not in report


def test_inspection_flags_a_different_firmware() -> None:
    inspection = _inspection(software_revision="V1.41.0101", notifications=[bytes.fromhex("FE EF 03 B0 04 B7")])
    assert not inspection.firmware_matches()
    report = inspection.describe()
    assert "software  V1.41.0101      V1.39.0518      DIFFERENT" in report
    assert "  fe ef 03 b0 04 b7" in report


def test_inspection_flags_layout_differences() -> None:
    assert _inspection(services={}).layout_problems() == [f"Service {protocol.SERVICE_UUID} is missing."]

    services = {protocol.SERVICE_UUID: {protocol.WRITE_UUID: ["write"]}}
    problems = _inspection(services=services).layout_problems()
    assert problems == [
        f"Write characteristic {protocol.WRITE_UUID} lacks 'write-without-response'. It has: write.",
        f"Notify characteristic {protocol.NOTIFY_UUID} is missing.",
    ]
    assert "MISMATCH" in _inspection(services=services).describe()


def test_fake_lamp_refuses_commands_off_the_allowlist() -> None:
    lamp = FakeLamp()
    lamp.handshake_done = True
    with pytest.raises(AssertionError, match="allowlist"):
        lamp._answer(bytes.fromhex("12 01"))  # noqa: SLF001
