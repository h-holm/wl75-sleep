"""BLE protocol of the Beurer WL75 wake-up light, restricted to the Sleep function.

Frame: ``FE EF LEN payload... CHK`` with ``LEN = len(payload) + 1`` and ``CHK = LEN ^ payload[0] ^ ... ^ payload[-1]``.
The first payload byte is the command (request) or the answer code (notification). See `docs/protocol.md`.

This module can only build the four allowlisted requests: handshake, Sleep status, Sleep on/off and Sleep
configuration. Clock, alarm, settings, light and radio commands are left out on purpose. `build_frame` is the single
place where a request becomes bytes, and it rejects everything else.

Framing, the frame parser and `SleepState` are ported from https://github.com/paukul/beurer-ha-bt
(`custom_components/beurer_wl75/protocol.py`, commit 247817fe), which is distributed under the following licence:

MIT License

Copyright (c) 2026 Pascal Friederich

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import enum
from dataclasses import dataclass


ADVERTISED_NAME = "WL75"

SERVICE_UUID = "14839ac4-7d7e-415c-9a42-167340cf2339"
WRITE_UUID = "8b00ace7-eb0b-49b0-bbe9-9aee0a26e1a3"
NOTIFY_UUID = "0734594a-a8e7-4b1a-a6b1-cd5243059a57"

# Device Information service (0x180A). The LightUp app reads the software revision (0x2A28).
DEVICE_INFORMATION_SERVICE_UUID = "0000180a-0000-1000-8000-00805f9b34fb"
FIRMWARE_REVISION_UUID = "00002a26-0000-1000-8000-00805f9b34fb"
HARDWARE_REVISION_UUID = "00002a27-0000-1000-8000-00805f9b34fb"
SOFTWARE_REVISION_UUID = "00002a28-0000-1000-8000-00805f9b34fb"

# The unit the published protocol was verified on.
DOCUMENTED_SOFTWARE_REVISION = "V1.39.0518"
DOCUMENTED_FIRMWARE_REVISION = "V2.0.0518"
DOCUMENTED_HARDWARE_REVISION = "V4"

HEADER = b"\xfe\xef"
# A frame's LEN byte counts the payload plus the checksum, so the shortest frame (one command byte) has LEN 2.
MIN_LEN = 2
# The longest known frame has LEN 0x0C. Allow some headroom.
MAX_LEN = 0x20

CMD_HANDSHAKE = 0x00
CMD_SLEEP_STATUS = 0x1D
CMD_SLEEP_SWITCH = 0x1E
CMD_SLEEP_CONFIG = 0x1F

ALLOWED_COMMANDS = frozenset({CMD_HANDSHAKE, CMD_SLEEP_STATUS, CMD_SLEEP_SWITCH, CMD_SLEEP_CONFIG})

# Second byte of `1F`: which half of the Sleep configuration is sent.
SLEEP_CONFIG_LIGHT = 0x01
SLEEP_CONFIG_SOUND = 0x02

HANDSHAKE_KEY = 0x75
HANDSHAKE_ANSWER = bytes([0xF0, HANDSHAKE_KEY])
ACK_OK = 0x01

# Unprompted notification "timer finished": `B0 <source>`.
CODE_TIMER_FINISHED = 0xB0
TIMER_SLEEP = 0x04
SLEEP_TIMER_FINISHED = bytes([CODE_TIMER_FINISHED, TIMER_SLEEP])

# Value ranges of the LightUp app. Nothing outside them is ever sent.
MIN_MINUTES = 1
MAX_MINUTES = 60
MIN_VOLUME = 1
MAX_VOLUME = 10
MIN_BRIGHTNESS = 10
MAX_BRIGHTNESS = 100

# `1E 01` alone starts a run of this many minutes, and a stopped lamp always reports this value.
DEFAULT_RUN_MINUTES = 2

_FIRST_EXTENDED_COMMAND = 0x10
_SLEEP_STATUS_DATA_BYTES = 7
_SLEEP_CONFIG_PAYLOAD_BYTES = 5


class ProtocolError(Exception):
    """An answer could not be decoded."""


class SleepLight(enum.IntEnum):
    """Light of the Sleep function."""

    OFF = 0
    SUNSET = 1
    MOOD = 2
    RELAX = 3


class SleepSound(enum.IntEnum):
    """Sound of the Sleep function."""

    OFF = 0
    RADIO = 1
    HARMONY = 2
    RELAX = 3
    INSTRUMENTAL = 4
    RAIN = 5


def response_code(command: int) -> int:
    """Return the code the lamp answers `command` with."""
    if command < _FIRST_EXTENDED_COMMAND:
        return 0xF0 | command
    return 0xE0 | (command & 0x0F)


def checksum(payload: bytes) -> int:
    """Return the checksum of a frame that carries `payload`: LEN xor every payload byte."""
    result = len(payload) + 1
    for byte in payload:
        result ^= byte
    return result


def validate_request(payload: bytes) -> None:
    """Raise `ValueError` unless `payload` is one of the allowlisted requests with every value in range."""
    if not payload:
        message = "Empty payload."
        raise ValueError(message)
    command, arguments = payload[0], payload[1:]
    if command not in ALLOWED_COMMANDS:
        message = f"Command 0x{command:02X} is not on the allowlist."
        raise ValueError(message)
    valid = False
    if command == CMD_HANDSHAKE:
        valid = arguments == bytes([HANDSHAKE_KEY])
    elif command == CMD_SLEEP_STATUS:
        valid = not arguments
    elif command == CMD_SLEEP_SWITCH:
        valid = arguments in {b"\x00", b"\x01"}
    elif len(payload) == _SLEEP_CONFIG_PAYLOAD_BYTES and arguments[0] == SLEEP_CONFIG_LIGHT:
        _, minutes, light, brightness = arguments
        valid = (
            MIN_MINUTES <= minutes <= MAX_MINUTES
            and light in set(SleepLight)
            and MIN_BRIGHTNESS <= brightness <= MAX_BRIGHTNESS
        )
    elif len(payload) == _SLEEP_CONFIG_PAYLOAD_BYTES and arguments[0] == SLEEP_CONFIG_SOUND:
        _, sound, volume, fade_out = arguments
        valid = sound in set(SleepSound) and MIN_VOLUME <= volume <= MAX_VOLUME and fade_out in {0, 1}
    if not valid:
        message = f"Request {payload.hex(' ')} is malformed or out of range."
        raise ValueError(message)


def build_frame(payload: bytes) -> bytes:
    """Wrap an allowlisted request payload into a WL75 frame.

    Raises:
        ValueError: If `payload` is not an allowlisted request with every value in range.
    """
    validate_request(payload)
    return bytes([*HEADER, len(payload) + 1, *payload, checksum(payload)])


def parse_frames(data: bytes) -> list[bytes]:
    """Extract all valid payloads from one notification.

    Under load the lamp bundles several frames into one notification and sometimes truncates them. Scan for the
    header, validate length and checksum, and skip garbage byte by byte.
    """
    payloads: list[bytes] = []
    index = 0
    while (index := data.find(HEADER, index)) != -1:
        if index + len(HEADER) >= len(data):
            break
        length = data[index + len(HEADER)]
        end = index + len(HEADER) + 1 + length
        if length < MIN_LEN or length > MAX_LEN or end > len(data):
            index += 1
            continue
        payload = bytes(data[index + len(HEADER) + 1 : end - 1])
        if checksum(payload) != data[end - 1]:
            index += 1
            continue
        payloads.append(payload)
        index = end
    return payloads


def handshake() -> bytes:
    """Return the payload of the handshake that must open every connection."""
    return bytes([CMD_HANDSHAKE, HANDSHAKE_KEY])


def query_sleep() -> bytes:
    """Return the payload of the Sleep status query."""
    return bytes([CMD_SLEEP_STATUS])


def switch_sleep(*, on: bool) -> bytes:
    """Return the payload that starts or stops the Sleep function."""
    return bytes([CMD_SLEEP_SWITCH, int(on)])


def set_sleep_light(minutes: int, light: SleepLight, brightness: int) -> bytes:
    """Return the payload that sets duration and light of a running Sleep function. Restarts the countdown.

    Raises:
        ValueError: If a value is outside the app's range. Nothing is clamped.
    """
    _require_range("minutes", minutes, MIN_MINUTES, MAX_MINUTES)
    _require_range("brightness", brightness, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
    payload = bytes([CMD_SLEEP_CONFIG, SLEEP_CONFIG_LIGHT, minutes, SleepLight(light), brightness])
    validate_request(payload)
    return payload


def set_sleep_sound(sound: SleepSound, volume: int, *, fade_out: bool) -> bytes:
    """Return the payload that sets the sound of a running Sleep function.

    Raises:
        ValueError: If a value is outside the app's range. Nothing is clamped.
    """
    _require_range("volume", volume, MIN_VOLUME, MAX_VOLUME)
    payload = bytes([CMD_SLEEP_CONFIG, SLEEP_CONFIG_SOUND, SleepSound(sound), volume, int(fade_out)])
    validate_request(payload)
    return payload


def is_ack(request: bytes, answer: bytes) -> bool:
    """Return `True` if `answer` accepts `request`, i.e. it is `<answer code> 01`."""
    return answer == bytes([response_code(request[0]), ACK_OK])


def _require_range(name: str, value: int, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        message = f"{name} must be an integer from {low} to {high}, got {value!r}."
        raise ValueError(message)


def _name(enumeration: type[enum.IntEnum], value: int) -> str:
    try:
        return enumeration(value).name.lower()
    except ValueError:
        return f"unknown ({value})"


@dataclass(frozen=True, slots=True)
class SleepState:
    """Sleep function status (answer `ED`).

    While the Sleep function runs, `minutes` is the remaining time and `brightness` the current, fading brightness.
    While it is off, `minutes` is always 2: the lamp does not keep the duration.
    """

    is_on: bool
    minutes: int
    light: int
    brightness: int
    sound: int
    volume: int
    fade_out: bool

    @classmethod
    def from_payload(cls, payload: bytes) -> SleepState:
        """Decode an `ED` answer.

        Raises:
            ProtocolError: If `payload` is not an `ED` answer with seven data bytes.
        """
        code = response_code(CMD_SLEEP_STATUS)
        if len(payload) != _SLEEP_STATUS_DATA_BYTES + 1 or payload[0] != code:
            message = f"Unexpected answer {payload.hex(' ')}, expected 0x{code:02X} with seven data bytes."
            raise ProtocolError(message)
        data = payload[1:]
        return cls(
            is_on=data[0] == 1,
            minutes=data[1],
            light=data[2],
            brightness=data[3],
            sound=data[4],
            volume=data[5],
            fade_out=data[6] == 1,
        )

    def describe(self) -> str:
        """Return a one-line, human-readable summary."""
        running = f"on, {self.minutes} min left" if self.is_on else "off"
        return (
            f"sleep {running} | light {_name(SleepLight, self.light)}, brightness {self.brightness} | "
            f"sound {_name(SleepSound, self.sound)}, volume {self.volume}, fade {'on' if self.fade_out else 'off'}"
        )
