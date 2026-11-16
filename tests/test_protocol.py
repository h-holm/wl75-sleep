import inspect
import itertools

import pytest

from wl75_sleep import protocol
from wl75_sleep.protocol import ProtocolError, SleepLight, SleepSound, SleepState


# The golden frames from docs/protocol.md.
HANDSHAKE = bytes.fromhex("FE EF 03 00 75 76")
SLEEP_STATUS = bytes.fromhex("FE EF 02 1D 1F")
SLEEP_ON = bytes.fromhex("FE EF 03 1E 01 1C")
SLEEP_OFF = bytes.fromhex("FE EF 03 1E 00 1D")
SIXTY_MINUTES_LIGHT_OFF = bytes.fromhex("FE EF 06 1F 01 3C 00 0A 2E")
RAIN_VOLUME_5_FADE_OFF = bytes.fromhex("FE EF 06 1F 02 05 05 00 1B")
RAIN_VOLUME_5_FADE_ON = bytes.fromhex("FE EF 06 1F 02 05 05 01 1A")
DOCUMENTED_STATUS_ANSWER = bytes.fromhex("FE EF 09 ED 00 02 01 63 00 06 01 83")
HANDSHAKE_ANSWER = bytes.fromhex("FE EF 03 F0 75 86")

DOCUMENTED_STATUS_PAYLOAD = bytes.fromhex("ED 00 02 01 63 00 06 01")


@pytest.mark.parametrize(
    ("payload", "frame"),
    [
        (protocol.handshake(), HANDSHAKE),
        (protocol.query_sleep(), SLEEP_STATUS),
        (protocol.switch_sleep(on=True), SLEEP_ON),
        (protocol.switch_sleep(on=False), SLEEP_OFF),
        (protocol.set_sleep_light(60, SleepLight.OFF, 10), SIXTY_MINUTES_LIGHT_OFF),
        (protocol.set_sleep_sound(SleepSound.RAIN, 5, fade_out=False), RAIN_VOLUME_5_FADE_OFF),
        (protocol.set_sleep_sound(SleepSound.RAIN, 5, fade_out=True), RAIN_VOLUME_5_FADE_ON),
    ],
)
def test_golden_request_frames(payload: bytes, frame: bytes) -> None:
    assert protocol.build_frame(payload) == frame


def test_golden_status_answer() -> None:
    assert protocol.parse_frames(DOCUMENTED_STATUS_ANSWER) == [DOCUMENTED_STATUS_PAYLOAD]
    # Rebuild the frame by hand: `build_frame` refuses answer codes.
    rebuilt = bytes([*protocol.HEADER, len(DOCUMENTED_STATUS_PAYLOAD) + 1, *DOCUMENTED_STATUS_PAYLOAD])
    assert rebuilt + bytes([protocol.checksum(DOCUMENTED_STATUS_PAYLOAD)]) == DOCUMENTED_STATUS_ANSWER


def test_documented_status_decodes() -> None:
    state = SleepState.from_payload(DOCUMENTED_STATUS_PAYLOAD)
    assert state == SleepState(
        is_on=False, minutes=2, light=SleepLight.SUNSET, brightness=99, sound=SleepSound.OFF, volume=6, fade_out=True
    )
    assert state.describe() == "sleep off | light sunset, brightness 99 | sound off, volume 6, fade on"


def test_running_status_describes_remaining_minutes() -> None:
    state = SleepState.from_payload(bytes.fromhex("ED 01 2D 00 0A 05 05 00"))
    assert state.describe() == "sleep on, 45 min left | light off, brightness 10 | sound rain, volume 5, fade off"


def test_unknown_light_and_sound_are_reported_not_raised() -> None:
    state = SleepState.from_payload(bytes.fromhex("ED 01 2D 07 0A 09 05 00"))
    assert "light unknown (7)" in state.describe()
    assert "sound unknown (9)" in state.describe()


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        bytes.fromhex("ED 00 02 01 63 00 06"),  # Six data bytes.
        bytes.fromhex("ED 00 02 01 63 00 06 01 00"),  # Eight data bytes.
        bytes.fromhex("EE 00 02 01 63 00 06 01"),  # Wrong answer code.
    ],
)
def test_status_rejects_other_answers(payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        SleepState.from_payload(payload)


@pytest.mark.parametrize(("command", "code"), [(0x00, 0xF0), (0x0F, 0xFF), (0x10, 0xE0), (0x1D, 0xED), (0x1F, 0xEF)])
def test_response_code(command: int, code: int) -> None:
    assert protocol.response_code(command) == code


def test_handshake_answer_matches_documented_frame() -> None:
    assert protocol.parse_frames(HANDSHAKE_ANSWER) == [protocol.HANDSHAKE_ANSWER]


# The allowlist.


@pytest.mark.parametrize("command", sorted(set(range(256)) - {0x00, 0x1D, 0x1E, 0x1F}))
def test_build_frame_refuses_every_other_command(command: int) -> None:
    for arguments in (b"", b"\x01", b"\x75", b"\x01\x3c\x00\x0a"):
        with pytest.raises(ValueError, match="allowlist"):
            protocol.build_frame(bytes([command]) + arguments)


def test_allowlist_is_exactly_the_four_commands() -> None:
    assert {0x00, 0x1D, 0x1E, 0x1F} == protocol.ALLOWED_COMMANDS


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        bytes.fromhex("00"),  # Handshake without the key.
        bytes.fromhex("00 76"),  # Handshake with a wrong key.
        bytes.fromhex("00 75 00"),
        bytes.fromhex("1D 00"),  # Status takes no arguments.
        bytes.fromhex("1E"),
        bytes.fromhex("1E 02"),
        bytes.fromhex("1E 01 00"),
        bytes.fromhex("1F"),
        bytes.fromhex("1F 01 3C 00"),  # Too short.
        bytes.fromhex("1F 01 3C 00 0A 00"),  # Too long.
        bytes.fromhex("1F 03 3C 00 0A"),  # Unknown configuration half.
        bytes.fromhex("1F 01 00 00 0A"),  # 0 minutes.
        bytes.fromhex("1F 01 3D 00 0A"),  # 61 minutes.
        bytes.fromhex("1F 01 3C 04 0A"),  # Unknown light.
        bytes.fromhex("1F 01 3C 00 09"),  # Brightness 9.
        bytes.fromhex("1F 01 3C 00 65"),  # Brightness 101.
        bytes.fromhex("1F 02 06 05 00"),  # Unknown sound.
        bytes.fromhex("1F 02 05 00 00"),  # Volume 0.
        bytes.fromhex("1F 02 05 0B 00"),  # Volume 11.
        bytes.fromhex("1F 02 05 05 02"),  # Fade is 0 or 1.
    ],
)
def test_build_frame_refuses_malformed_or_out_of_range_requests(payload: bytes) -> None:
    with pytest.raises(ValueError, match=r"malformed|Empty"):
        protocol.build_frame(payload)


def test_every_public_function_that_returns_bytes_stays_on_the_allowlist() -> None:
    """Call every public payload builder with every in-range argument and check what comes out."""
    builders = {
        name
        for name, function in inspect.getmembers(protocol, inspect.isfunction)
        if not name.startswith("_") and inspect.signature(function).return_annotation is bytes
    }
    assert builders == {
        "build_frame",
        "handshake",
        "query_sleep",
        "switch_sleep",
        "set_sleep_light",
        "set_sleep_sound",
    }

    payloads = [protocol.handshake(), protocol.query_sleep()]
    payloads += [protocol.switch_sleep(on=on) for on in (True, False)]
    payloads += [
        protocol.set_sleep_light(minutes, light, brightness)
        for minutes, light, brightness in itertools.product(range(1, 61), SleepLight, range(10, 101))
    ]
    payloads += [
        protocol.set_sleep_sound(sound, volume, fade_out=fade_out)
        for sound, volume, fade_out in itertools.product(SleepSound, range(1, 11), (True, False))
    ]
    for payload in payloads:
        frame = protocol.build_frame(payload)
        assert frame[3] in {0x00, 0x1D, 0x1E, 0x1F}
        assert protocol.parse_frames(frame) == [payload]


# Builders raise, they do not clamp.


@pytest.mark.parametrize("minutes", [-1, 0, 61, 120, 256])
def test_set_sleep_light_refuses_minutes_out_of_range(minutes: int) -> None:
    with pytest.raises(ValueError, match="minutes"):
        protocol.set_sleep_light(minutes, SleepLight.OFF, 10)


@pytest.mark.parametrize("brightness", [-1, 0, 9, 101, 255])
def test_set_sleep_light_refuses_brightness_out_of_range(brightness: int) -> None:
    with pytest.raises(ValueError, match="brightness"):
        protocol.set_sleep_light(60, SleepLight.OFF, brightness)


@pytest.mark.parametrize("volume", [-1, 0, 11, 255])
def test_set_sleep_sound_refuses_volume_out_of_range(volume: int) -> None:
    with pytest.raises(ValueError, match="volume"):
        protocol.set_sleep_sound(SleepSound.RAIN, volume, fade_out=False)


def test_builders_refuse_unknown_light_and_sound() -> None:
    with pytest.raises(ValueError, match="SleepLight"):
        protocol.set_sleep_light(60, 4, 10)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="SleepSound"):
        protocol.set_sleep_sound(6, 5, fade_out=False)  # ty: ignore[invalid-argument-type]


def test_builders_refuse_non_integers() -> None:
    with pytest.raises(ValueError, match="minutes"):
        protocol.set_sleep_light(True, SleepLight.OFF, 10)  # noqa: FBT003
    with pytest.raises(ValueError, match="volume"):
        protocol.set_sleep_sound(SleepSound.RAIN, 5.0, fade_out=False)  # ty: ignore[invalid-argument-type]


# The parser.


def test_parser_splits_bundled_frames() -> None:
    bundled = HANDSHAKE_ANSWER + DOCUMENTED_STATUS_ANSWER + bytes.fromhex("FE EF 03 EE 01 EC")
    assert protocol.parse_frames(bundled) == [
        protocol.HANDSHAKE_ANSWER,
        DOCUMENTED_STATUS_PAYLOAD,
        bytes.fromhex("EE 01"),
    ]


def test_parser_handles_the_documented_garbled_notification() -> None:
    garbled = bytes.fromhex("FE EF 03 E3 01 E1 FE EF 03 E3 01 E1 FE EF 03 FE EF")
    assert protocol.parse_frames(garbled) == [bytes.fromhex("E3 01"), bytes.fromhex("E3 01")]


@pytest.mark.parametrize("cut", range(len(DOCUMENTED_STATUS_ANSWER)))
def test_parser_drops_truncated_frames(cut: int) -> None:
    assert protocol.parse_frames(DOCUMENTED_STATUS_ANSWER[:cut]) == []


def test_parser_resynchronises_after_a_truncated_frame() -> None:
    assert protocol.parse_frames(DOCUMENTED_STATUS_ANSWER[:5] + HANDSHAKE_ANSWER) == [protocol.HANDSHAKE_ANSWER]


def test_parser_skips_leading_garbage() -> None:
    assert protocol.parse_frames(b"\x00\xfe\x01" + HANDSHAKE_ANSWER + b"\xef") == [protocol.HANDSHAKE_ANSWER]


def test_parser_drops_frames_with_a_bad_checksum() -> None:
    corrupted = bytearray(DOCUMENTED_STATUS_ANSWER)
    corrupted[-1] ^= 0xFF
    assert protocol.parse_frames(bytes(corrupted)) == []
    assert protocol.parse_frames(bytes(corrupted) + HANDSHAKE_ANSWER) == [protocol.HANDSHAKE_ANSWER]


@pytest.mark.parametrize("length", [0x00, 0x01, 0x21, 0xFF])
def test_parser_drops_frames_with_an_impossible_length(length: int) -> None:
    assert protocol.parse_frames(bytes([0xFE, 0xEF, length]) + bytes(40)) == []


def test_sleep_timer_finished_notification_parses() -> None:
    assert protocol.parse_frames(bytes.fromhex("FE EF 03 B0 04 B7")) == [protocol.SLEEP_TIMER_FINISHED]


# Acknowledgements.


def test_is_ack() -> None:
    request = protocol.set_sleep_light(60, SleepLight.OFF, 10)
    assert protocol.is_ack(request, bytes.fromhex("EF 01"))
    assert not protocol.is_ack(request, bytes.fromhex("EF 00"))  # Rejected: Sleep is not running.
    assert not protocol.is_ack(request, bytes.fromhex("EE 01"))  # Answer to a different command.
    assert not protocol.is_ack(request, bytes.fromhex("EF"))
