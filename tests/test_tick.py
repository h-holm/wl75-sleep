import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
lazy from collections.abc import Generator
lazy from pathlib import Path

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from loguru import logger
from typer.testing import CliRunner

from wl75_sleep import client, protocol, tick
from wl75_sleep.config import Config, load_config
from wl75_sleep.main import app
from wl75_sleep.session import Night, State
from wl75_sleep.tick import load_state, locked, run_tick, save_state, state_from_json, state_to_json

from .fake_lamp import FakeLamp


ZONE = timezone(timedelta(hours=1))
NIGHT = date(2026, 11, 15)
STATUS = bytes.fromhex("1D")
SWITCH_ON = bytes.fromhex("1E 01")
FULL_SEGMENT = bytes.fromhex("1F 01 3C 00 63")
RAIN_AT_6_WITH_FADE = bytes.fromhex("1F 02 05 06 01")


def at(hour: int, minute: int = 0, *, day: int = 15) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=ZONE)


class Clock:
    """A settable stand-in for `tick.local_now`."""

    def __init__(self, now: datetime) -> None:
        """Stand still at `now` until `now` is set."""
        self.now = now

    def __call__(self) -> datetime:
        """Return the set time."""
        return self.now


@pytest.fixture
def lamp(monkeypatch: pytest.MonkeyPatch) -> FakeLamp:
    fake = FakeLamp()

    async def find_lamp(_device_uuid: str) -> BLEDevice:
        return BLEDevice(fake.address, "WL75", details=None)

    monkeypatch.setattr(client, "find_lamp", find_lamp)
    monkeypatch.setattr(client, "make_transport", lambda _device: fake)
    monkeypatch.setattr(client, "RETRY_DELAYS_SECONDS", (0, 0))
    monkeypatch.setattr(client, "ANSWER_TIMEOUT_SECONDS", 0.05)
    return fake


@pytest.fixture
def config(lamp: FakeLamp) -> Config:
    return Config(device_uuid=lamp.address, verified_firmware="V1.39.0518", volume=6)


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "state.json"


@pytest.fixture
def messages() -> Generator[list[str]]:
    collected: list[str] = []
    handler = logger.add(lambda message: collected.append(str(message).strip()), format="{level} {message}")
    yield collected
    logger.remove(handler)


def _tick(config: Config, config_path: Path, state_path: Path, now: datetime) -> bool:
    return run_tick(config, config_path, state_path, Clock(now))


def _night(state_path: Path) -> Night:
    night = load_state(state_path).night
    assert night is not None
    return night


def test_outside_the_window_nothing_touches_bluetooth(
    config: Config, config_path: Path, state_path: Path, lamp: FakeLamp, messages: list[str]
) -> None:
    assert _tick(config, config_path, state_path, at(12))
    assert lamp.connects == 0
    assert load_state(state_path) == State(last_tick=at(12))
    assert "INFO tick: nothing, outside the window." in messages


def test_start_at_window_start_with_fade_on(
    config: Config, config_path: Path, state_path: Path, lamp: FakeLamp, messages: list[str]
) -> None:
    assert _tick(config, config_path, state_path, at(22))

    # The tick reads the status to choose the step, and `start_sleep` reads it again.
    assert lamp.written == [
        bytes.fromhex("00 75"),
        STATUS,
        STATUS,
        SWITCH_ON,
        FULL_SEGMENT,
        RAIN_AT_6_WITH_FADE,
        STATUS,
    ]
    assert lamp.disconnects == 1
    night = _night(state_path)
    assert (night.night, night.starts, night.expires_at) == (NIGHT, 1, at(23))
    assert any(message.startswith("INFO tick: start -> start, 60 min") for message in messages)
    assert any("found after" in message for message in messages)


def test_idle_ticks_between_top_ups_do_not_connect(
    config: Config, config_path: Path, state_path: Path, lamp: FakeLamp
) -> None:
    _tick(config, config_path, state_path, at(22))
    _tick(config, config_path, state_path, at(22, 5))
    _tick(config, config_path, state_path, at(22, 40))
    assert lamp.connects == 1


def test_top_up_after_renew_after(config: Config, config_path: Path, state_path: Path, lamp: FakeLamp) -> None:
    _tick(config, config_path, state_path, at(22))
    lamp.written.clear()
    assert _tick(config, config_path, state_path, at(22, 45))

    assert lamp.written == [bytes.fromhex("00 75"), STATUS, STATUS, FULL_SEGMENT, STATUS]
    night = _night(state_path)
    assert (night.starts, night.top_ups, night.expires_at) == (1, 1, at(23, 45))


def test_adopts_a_sleep_that_already_runs(
    config: Config, config_path: Path, state_path: Path, lamp: FakeLamp, messages: list[str]
) -> None:
    lamp.sleep[0] = 1
    assert _tick(config, config_path, state_path, at(22))

    assert SWITCH_ON not in lamp.written
    assert RAIN_AT_6_WITH_FADE not in lamp.written
    assert FULL_SEGMENT in lamp.written
    assert any("adopted the running Sleep, 60 min" in message for message in messages)


def test_a_stop_by_hand_ends_the_night(
    config: Config, config_path: Path, state_path: Path, lamp: FakeLamp, messages: list[str]
) -> None:
    _tick(config, config_path, state_path, at(22))
    lamp.sleep[0] = 0
    lamp.written.clear()
    assert _tick(config, config_path, state_path, at(22, 45))

    assert lamp.written == [bytes.fromhex("00 75"), STATUS]
    assert _night(state_path).stopped_by_hand
    assert any("stopped by hand" in message for message in messages)
    _tick(config, config_path, state_path, at(23, 30))
    assert lamp.connects == 2


def test_a_missed_top_up_starts_again_and_counts_a_gap(
    config: Config, config_path: Path, state_path: Path, lamp: FakeLamp, messages: list[str]
) -> None:
    _tick(config, config_path, state_path, at(22))
    lamp.sleep[0] = 0
    lamp.written.clear()
    assert _tick(config, config_path, state_path, at(23, 5))

    assert SWITCH_ON in lamp.written
    night = _night(state_path)
    assert (night.starts, night.gaps, night.expires_at) == (2, 1, at(0, 5, day=16))
    assert any(message.startswith("WARNING tick: gap.") for message in messages)


def test_the_last_segment_ends_at_the_window_end(
    config: Config, config_path: Path, state_path: Path, lamp: FakeLamp
) -> None:
    night = Night(NIGHT, last_success=at(5, 30, day=16), expires_at=at(6, 30, day=16), volume=6)
    save_state(state_path, State(night=night))
    lamp.sleep[0] = 1
    assert _tick(config, config_path, state_path, at(6, 15, day=16))

    assert bytes.fromhex("1F 01 2D 00 63") in lamp.written
    assert _night(state_path).expires_at == at(7, day=16)
    lamp.written.clear()
    _tick(config, config_path, state_path, at(6, 55, day=16))
    assert lamp.written == []


def test_a_volume_change_sends_the_sound_settings_with_a_top_up(
    config: Config, config_path: Path, state_path: Path, lamp: FakeLamp, messages: list[str]
) -> None:
    config = replace(config, later_volume=4, later_volume_from=time(23, 0))
    _tick(config, config_path, state_path, at(22, 45))
    lamp.written.clear()
    assert _tick(config, config_path, state_path, at(23))

    # Sleep runs, so no switch-on: only the light and the sound settings, with fade still on.
    assert lamp.written == [
        bytes.fromhex("00 75"),
        STATUS,
        STATUS,
        FULL_SEGMENT,
        bytes.fromhex("1F 02 05 04 01"),
        STATUS,
    ]
    night = _night(state_path)
    assert (night.starts, night.top_ups, night.volume, night.expires_at) == (1, 1, 4, at(0, day=16))
    assert any(
        "tick: top-up -> reconfigure, 60 min at volume 4, the volume changes to 4" in message for message in messages
    )
    lamp.written.clear()
    _tick(config, config_path, state_path, at(23, 5))
    assert lamp.written == []


def test_a_failed_start_is_counted_and_tried_again(
    config: Config, config_path: Path, state_path: Path, lamp: FakeLamp, messages: list[str]
) -> None:
    lamp.connect_error = OSError("Connection timed out.")
    assert not _tick(config, config_path, state_path, at(22))

    assert lamp.connects == 3
    night = _night(state_path)
    assert (night.failures, night.last_success) == (1, None)
    assert any(message.startswith("WARNING tick: start failed") for message in messages)

    lamp.connect_error = None
    assert _tick(config, config_path, state_path, at(22, 5))
    assert _night(state_path).starts == 1


def test_a_start_that_failed_halfway_is_configured_in_full(
    config: Config, config_path: Path, state_path: Path, lamp: FakeLamp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first attempt switches Sleep on and then loses the link. The second must still send the sound."""
    original = lamp.write_gatt_char
    failed = False

    async def write_once_failing(char_specifier: str, data: bytes, response: bool | None = None) -> None:
        nonlocal failed
        if not failed and data == protocol.build_frame(FULL_SEGMENT):
            failed = True
            message = "Link lost."
            raise BleakError(message)
        await original(char_specifier, data, response=response)

    monkeypatch.setattr(lamp, "write_gatt_char", write_once_failing)
    assert _tick(config, config_path, state_path, at(22))

    assert lamp.connects == 2
    assert lamp.written.count(SWITCH_ON) == 1
    assert lamp.written[-2:] == [RAIN_AT_6_WITH_FADE, STATUS]
    assert _night(state_path).starts == 1


def test_gives_up_after_the_tick_timeout(  # noqa: PLR0913, PLR0917
    config: Config,
    config_path: Path,
    state_path: Path,
    lamp: FakeLamp,
    monkeypatch: pytest.MonkeyPatch,
    messages: list[str],
) -> None:
    monkeypatch.setattr(tick, "TICK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(client, "ANSWER_TIMEOUT_SECONDS", 1)
    lamp.unanswered.add(protocol.CMD_HANDSHAKE)
    assert not _tick(config, config_path, state_path, at(22))

    assert lamp.disconnects == lamp.connects == 1
    assert any("no result within 0.01 s" in message for message in messages)


def test_saves_a_new_uuid(config: Config, config_path: Path, state_path: Path, lamp: FakeLamp) -> None:
    config = Config(device_uuid="old-uuid", verified_firmware="V1.39.0518")
    _tick(config, config_path, state_path, at(22))
    assert load_config(config_path).device_uuid == lamp.address


def test_a_held_lock_skips_the_tick(
    config: Config, config_path: Path, state_path: Path, lamp: FakeLamp, messages: list[str]
) -> None:
    with locked(state_path.with_suffix(".lock")) as acquired:
        assert acquired
        assert _tick(config, config_path, state_path, at(22))
    assert lamp.connects == 0
    assert any("another tick is still running" in message for message in messages)


def test_state_survives_a_round_trip() -> None:
    state = State(
        night=Night(
            NIGHT,
            last_success=at(22),
            expires_at=at(23),
            stopped_by_hand=True,
            volume=5,
            starts=1,
            top_ups=2,
            gaps=3,
            failures=4,
        ),
        skip_night=NIGHT,
        last_tick=at(22, 5),
    )
    assert state_from_json(state_to_json(state)) == state
    assert state_from_json(state_to_json(State())) == State()


def test_a_state_from_before_volume_changes_is_read() -> None:
    night = Night(NIGHT, last_success=at(22), expires_at=at(23), starts=1, volume=6)
    data = json.loads(state_to_json(State(night=night)))
    del data["night"]["volume"]
    assert state_from_json(json.dumps(data)) == State(night=replace(night, volume=None))


def test_an_unreadable_state_starts_fresh(state_path: Path, messages: list[str]) -> None:
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"night": {"night": "yesterday"}}', encoding="utf-8")
    assert load_state(state_path) == State()
    assert any("Starting from a fresh state" in message for message in messages)


runner = CliRunner()


def _invoke(config_path: Path, state_path: Path, *arguments: str) -> tuple[int, str]:
    result = runner.invoke(app, ["--config-path", str(config_path), "--state-path", str(state_path), *arguments])
    return result.exit_code, result.output


def _write_config(config_path: Path, lamp: FakeLamp, extra: str = "") -> None:
    config_path.write_text(
        f'device_uuid = "{lamp.address}"\nverified_firmware = "V1.39.0518"\n{extra}', encoding="utf-8"
    )


def test_cli_tick(config_path: Path, state_path: Path, lamp: FakeLamp, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(config_path, lamp)
    monkeypatch.setattr(tick, "local_now", Clock(at(22)))
    exit_code, _ = _invoke(config_path, state_path, "tick")
    assert exit_code == 0
    assert _night(state_path).starts == 1


def test_cli_tick_fails_when_the_lamp_fails(
    config_path: Path, state_path: Path, lamp: FakeLamp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(config_path, lamp)
    lamp.connect_error = OSError("Connection timed out.")
    monkeypatch.setattr(tick, "local_now", Clock(at(22)))
    exit_code, _ = _invoke(config_path, state_path, "tick")
    assert exit_code == 1


def test_cli_tick_refuses_to_run_before_the_firmware_gate(config_path: Path, state_path: Path, lamp: FakeLamp) -> None:
    config_path.write_text(f'device_uuid = "{lamp.address}"\n', encoding="utf-8")
    exit_code, output = _invoke(config_path, state_path, "tick")
    assert exit_code == 2
    assert "Nothing was sent" in output
    assert not state_path.exists()


def test_cli_skip_and_undo(
    config_path: Path, state_path: Path, lamp: FakeLamp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(config_path, lamp)
    monkeypatch.setattr(tick, "local_now", Clock(at(12)))

    exit_code, output = _invoke(config_path, state_path, "skip")
    assert exit_code == 0
    assert "Skipping the night of Sun 15 Nov, 22:00 to 07:00." in output
    assert "ends by itself" not in output
    assert load_state(state_path).skip_night == NIGHT

    monkeypatch.setattr(tick, "local_now", Clock(at(22)))
    assert _invoke(config_path, state_path, "tick")[0] == 0
    assert lamp.connects == 0

    exit_code, output = _invoke(config_path, state_path, "skip", "--undo")
    assert exit_code == 0
    assert "Skip cancelled" in output
    assert load_state(state_path).skip_night is None

    exit_code, output = _invoke(config_path, state_path, "skip", "--undo")
    assert "Nothing to undo" in output


def test_cli_skip_during_the_night_mentions_the_running_sleep(
    config_path: Path, state_path: Path, lamp: FakeLamp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(config_path, lamp)
    monkeypatch.setattr(tick, "local_now", Clock(at(23)))
    exit_code, output = _invoke(config_path, state_path, "skip")
    assert exit_code == 0
    assert "`wl75 stop` ends it at once" in output


def test_cli_skip_waits_for_a_running_tick(config_path: Path, state_path: Path, lamp: FakeLamp) -> None:
    _write_config(config_path, lamp)
    with locked(state_path.with_suffix(".lock")):
        exit_code, output = _invoke(config_path, state_path, "skip")
    assert exit_code == 1
    assert "A tick is running" in output


def test_default_window_is_used_by_skip(
    config_path: Path, state_path: Path, lamp: FakeLamp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(config_path, lamp, 'window_start = "13:00"\nwindow_end = "13:30"\n')
    monkeypatch.setattr(tick, "local_now", Clock(at(14)))
    _, output = _invoke(config_path, state_path, "skip")
    assert "Skipping the night of Mon 16 Nov, 13:00 to 13:30." in output
    assert load_config(config_path).window_start == time(13, 0)


def test_sleep_stopping_between_two_queries_is_tried_again(
    config: Config, config_path: Path, state_path: Path, lamp: FakeLamp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tick sees Sleep on, then `extend_sleep` sees it off: the next attempt decides afresh."""
    _tick(config, config_path, state_path, at(22))
    original = lamp.write_gatt_char
    queries = 0

    async def stop_after_first_query(char_specifier: str, data: bytes, response: bool | None = None) -> None:
        nonlocal queries
        await original(char_specifier, data, response=response)
        if data == protocol.build_frame(STATUS):
            queries += 1
            if queries == 1:
                lamp.sleep[0] = 0

    lamp.written.clear()
    monkeypatch.setattr(lamp, "write_gatt_char", stop_after_first_query)
    assert _tick(config, config_path, state_path, at(22, 45))

    assert lamp.connects == 3
    assert _night(state_path).stopped_by_hand


def test_local_now_carries_the_offset() -> None:
    assert tick.local_now().utcoffset() is not None
