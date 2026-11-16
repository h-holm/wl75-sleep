"""Tests for `tools/listening_test.py`. One minute of a run lasts 0.1 s here."""

import asyncio
lazy from collections.abc import Callable
lazy from pathlib import Path

import pytest
from bleak.backends.device import BLEDevice
from typer.testing import CliRunner

from tools import listening_test
from tools.listening_test import LinkLostError, ListeningPlan, Timeline, app, run_on_lamp
from wl75_sleep import client, protocol
from wl75_sleep.client import LampNotFoundError
from wl75_sleep.protocol import SleepLight, SleepSound

from .fake_lamp import FakeLamp, frame


START = [
    bytes.fromhex("00 75"),
    bytes.fromhex("1D"),
    bytes.fromhex("1E 01"),
    bytes.fromhex("1F 01 03 00 63"),
    bytes.fromhex("1F 02 05 06 00"),
    bytes.fromhex("1D"),
]
TOP_UP = [bytes.fromhex("1D"), bytes.fromhex("1F 01 03 00 63"), bytes.fromhex("1D")]
STATUS = bytes.fromhex("1D")


@pytest.fixture(autouse=True)
def fast_timings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(listening_test, "SECONDS_PER_MINUTE", 0.1)
    monkeypatch.setattr(listening_test, "END_MARGIN_SECONDS", 0.1)
    monkeypatch.setattr(listening_test, "CHECK_INTERVAL_SECONDS", 0.005)
    monkeypatch.setattr(listening_test, "START_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(client, "ANSWER_TIMEOUT_SECONDS", 0.05)


@pytest.fixture
def lamp(monkeypatch: pytest.MonkeyPatch) -> FakeLamp:
    """Replace the hardware layer. `lamp.drop_link` simulates the lamp closing the connection."""
    fake = FakeLamp()
    disconnect_callbacks: list[Callable[[], None]] = []

    def drop_link() -> None:
        disconnect_callbacks[-1]()

    fake.drop_link = drop_link  # ty: ignore[unresolved-attribute]

    async def find_lamp(_device_uuid: str) -> BLEDevice:
        return BLEDevice(fake.address, "WL75", details=None)

    def make_transport(_device: BLEDevice, on_disconnect: Callable[[], None]) -> FakeLamp:
        disconnect_callbacks.append(on_disconnect)
        return fake

    monkeypatch.setattr(client, "find_lamp", find_lamp)
    monkeypatch.setattr(listening_test, "make_transport", make_transport)
    return fake


def _plan(**changes: object) -> ListeningPlan:
    values: dict[str, object] = {
        "minutes": 3,
        "light": SleepLight.OFF,
        "sound": SleepSound.RAIN,
        "volume": 6,
        "fade_out": False,
    }
    return ListeningPlan(**(values | changes))  # ty: ignore[invalid-argument-type]


def _end_run_after(lamp: FakeLamp, seconds: float) -> None:
    """Let the fake lamp's timer run out: Sleep goes off and `B0 04` arrives."""

    def end() -> None:
        lamp.sleep[0] = 0
        lamp.push(frame(protocol.SLEEP_TIMER_FINISHED))

    asyncio.get_running_loop().call_later(seconds, end)


async def test_start_top_up_and_end(lamp: FakeLamp, capsys: pytest.CaptureFixture[str]) -> None:
    timeline = Timeline()
    _end_run_after(lamp, 0.25)
    await run_on_lamp("uuid", _plan(top_up_after_seconds=0.1), timeline)

    assert lamp.written == [*START, *TOP_UP, STATUS]
    for payload in lamp.written:
        protocol.validate_request(payload)
    assert (lamp.connects, lamp.disconnects) == (1, 1)
    output = capsys.readouterr().out
    assert "start, 3 min" in output
    assert "top-up, 3 min" in output
    assert "b0 04" in output
    assert "sleep off" in output.splitlines()[-1]
    assert timeline.started_at is not None
    assert timeline.topped_up_at is not None
    assert timeline.finished_at is not None
    assert timeline.finished_at - timeline.topped_up_at == pytest.approx(0.15, abs=0.05)
    assert "s after the top-up" in timeline.summary()


async def test_top_up_with_another_duration_and_light(lamp: FakeLamp) -> None:
    _end_run_after(lamp, 0.2)
    plan = _plan(light=SleepLight.SUNSET, top_up_after_seconds=0.1, top_up_minutes=10, top_up_light=SleepLight.OFF)
    await run_on_lamp("uuid", plan, Timeline())

    assert bytes.fromhex("1F 01 03 01 63") in lamp.written
    assert bytes.fromhex("1F 01 0A 00 63") in lamp.written


async def test_polls_the_status_until_the_end(lamp: FakeLamp, capsys: pytest.CaptureFixture[str]) -> None:
    _end_run_after(lamp, 0.18)
    await run_on_lamp("uuid", _plan(poll_every_seconds=0.05), Timeline())

    polls = lamp.written[len(START) : -1]
    assert polls == [STATUS] * len(polls)
    assert 2 <= len(polls) <= 4
    assert capsys.readouterr().out.count("status ") >= len(polls)


async def test_resends_the_sound_after_the_top_up(lamp: FakeLamp, capsys: pytest.CaptureFixture[str]) -> None:
    _end_run_after(lamp, 0.2)
    await run_on_lamp("uuid", _plan(top_up_after_seconds=0.1, resend_sound=True, fade_out=True), Timeline())

    resend = [bytes.fromhex("1F 02 05 06 01"), STATUS]
    assert lamp.written[len(START) :] == [*TOP_UP, *resend, STATUS]
    assert "re-sent sound" in capsys.readouterr().out


async def test_gives_up_waiting_when_no_end_arrives(lamp: FakeLamp, capsys: pytest.CaptureFixture[str]) -> None:
    timeline = Timeline()
    await run_on_lamp("uuid", _plan(), timeline)

    assert lamp.written == [*START, STATUS]
    assert "no b0 04" in capsys.readouterr().out
    assert "no B0 04 seen" in timeline.summary()
    assert lamp.disconnects == 1


async def test_skips_the_top_up_when_sleep_has_ended(lamp: FakeLamp, capsys: pytest.CaptureFixture[str]) -> None:
    asyncio.get_running_loop().call_later(0.05, lambda: lamp.sleep.__setitem__(0, 0))
    await run_on_lamp("uuid", _plan(top_up_after_seconds=0.1), Timeline())

    assert lamp.written == [*START, STATUS]
    assert "top-up skipped" in capsys.readouterr().out


async def test_reports_a_dropped_link_and_does_not_retry(lamp: FakeLamp) -> None:
    asyncio.get_running_loop().call_later(0.05, lamp.drop_link)  # ty: ignore[unresolved-attribute]
    with pytest.raises(LinkLostError, match="dropped the connection"):
        await run_on_lamp("uuid", _plan(), Timeline())

    assert lamp.written == START
    assert (lamp.connects, lamp.disconnects) == (1, 1)


async def test_a_failed_request_after_the_start_is_not_retried(lamp: FakeLamp) -> None:
    asyncio.get_running_loop().call_later(0.05, lambda: lamp.unanswered.add(protocol.CMD_SLEEP_STATUS))
    with pytest.raises(LinkLostError, match="after the start: No answer to 1d"):
        await run_on_lamp("uuid", _plan(poll_every_seconds=0.1), Timeline())

    assert (lamp.connects, lamp.disconnects) == (1, 1)


async def test_a_failure_before_the_start_is_retried(
    lamp: FakeLamp, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lamp.connect_error = OSError("Connection timed out.")

    async def find_lamp(_device_uuid: str) -> BLEDevice:
        if lamp.connects == 1:
            lamp.connect_error = None
            _end_run_after(lamp, 0.35)
        return BLEDevice(lamp.address, "WL75", details=None)

    monkeypatch.setattr(client, "find_lamp", find_lamp)
    await run_on_lamp("uuid", _plan(), Timeline())

    assert (lamp.connects, lamp.disconnects) == (2, 2)
    assert lamp.written == [*START, STATUS]
    assert "attempt failed" in capsys.readouterr().out


async def test_gives_up_when_the_lamp_is_never_found(monkeypatch: pytest.MonkeyPatch) -> None:
    async def find_lamp(_device_uuid: str) -> BLEDevice:
        message = "No WL75 advertised."
        raise LampNotFoundError(message)

    monkeypatch.setattr(client, "find_lamp", find_lamp)
    timeline = Timeline()
    with pytest.raises(LampNotFoundError):
        await run_on_lamp("uuid", _plan(), timeline)
    assert timeline.summary() == "The run did not start."


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"top_up_after_seconds": 180}, "during the 3-minute run"),
        ({"top_up_after_seconds": 0}, "during the 3-minute run"),
        ({"resend_sound": True}, "need a top-up time"),
        ({"top_up_minutes": 10}, "need a top-up time"),
        ({"poll_every_seconds": 0}, "polling interval must be positive"),
    ],
)
def test_plan_problems(changes: dict[str, object], problem: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(listening_test, "SECONDS_PER_MINUTE", 60.0)
    (message,) = _plan(**changes).problems()
    assert problem in message


def test_a_valid_plan_has_no_problems() -> None:
    assert _plan(top_up_after_seconds=0.1, top_up_minutes=10, resend_sound=True, poll_every_seconds=1).problems() == []


runner = CliRunner()


def _config(tmp_path: Path, *, verified: bool = True) -> Path:
    path = tmp_path / "config.toml"
    firmware = 'verified_firmware = "V1.39.0518"\n' if verified else ""
    path.write_text(f'device_uuid = "uuid"\n{firmware}volume = 6\n', encoding="utf-8")
    return path


def test_cli_runs_a_test_with_the_configured_volume(tmp_path: Path, lamp: FakeLamp) -> None:
    def end_soon() -> None:
        lamp.sleep[0] = 0
        lamp.push(frame(protocol.SLEEP_TIMER_FINISHED))

    original_connect = lamp.connect

    async def connect() -> None:
        await original_connect()
        asyncio.get_running_loop().call_later(0.1, end_soon)

    lamp.connect = connect
    result = runner.invoke(app, ["--config-path", str(_config(tmp_path)), "--minutes", "3", "--top-up-after", "0.05"])

    assert result.exit_code == 0, result.output
    assert bytes.fromhex("1F 02 05 06 00") in lamp.written
    assert "Summary:" in result.output
    assert lamp.disconnects == 1


def test_cli_refuses_to_run_before_the_firmware_gate(tmp_path: Path, lamp: FakeLamp) -> None:
    result = runner.invoke(app, ["--config-path", str(_config(tmp_path, verified=False))])

    assert result.exit_code == 2
    assert "Nothing was sent" in result.output
    assert lamp.connects == 0


def test_cli_rejects_an_impossible_plan(tmp_path: Path, lamp: FakeLamp) -> None:
    result = runner.invoke(app, ["--config-path", str(_config(tmp_path)), "--minutes", "3", "--top-up-after", "999"])

    assert result.exit_code == 2
    assert "during the 3-minute run" in result.output
    assert lamp.connects == 0


def test_cli_reports_an_invalid_configuration(tmp_path: Path, lamp: FakeLamp) -> None:
    path = tmp_path / "config.toml"
    path.write_text("volume = 99\n", encoding="utf-8")
    result = runner.invoke(app, ["--config-path", str(path)])

    assert result.exit_code == 2
    assert lamp.connects == 0


@pytest.mark.parametrize("arguments", [["--volume", "11"], ["--minutes", "61"], ["--sound", "nonsense"]])
def test_cli_keeps_values_in_the_app_ranges(tmp_path: Path, lamp: FakeLamp, arguments: list[str]) -> None:
    result = runner.invoke(app, ["--config-path", str(_config(tmp_path)), *arguments])

    assert result.exit_code == 2
    assert lamp.connects == 0


def test_cli_reports_a_lost_link(tmp_path: Path, lamp: FakeLamp) -> None:
    original_connect = lamp.connect

    async def connect() -> None:
        await original_connect()
        asyncio.get_running_loop().call_later(0.05, lamp.drop_link)  # ty: ignore[unresolved-attribute]

    lamp.connect = connect
    result = runner.invoke(app, ["--config-path", str(_config(tmp_path))])

    assert result.exit_code == 1
    assert "ends by itself" in result.output
    assert "Summary:" in result.output


def test_cli_reports_a_failure_before_the_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def find_lamp(_device_uuid: str) -> BLEDevice:
        message = "No WL75 advertised."
        raise LampNotFoundError(message)

    monkeypatch.setattr(client, "find_lamp", find_lamp)
    result = runner.invoke(app, ["--config-path", str(_config(tmp_path))])

    assert result.exit_code == 1
    assert "No WL75 advertised." in result.output
    assert "The run did not start." in result.output
