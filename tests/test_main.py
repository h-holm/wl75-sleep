lazy from pathlib import Path

import pytest
import typer
from bleak.backends.device import BLEDevice
from typer.testing import CliRunner

from wl75_sleep import client, main, protocol
from wl75_sleep.client import Inspection, LampNotFoundError, Sighting
from wl75_sleep.config import load_config
from wl75_sleep.main import app

from .fake_lamp import FakeLamp


UUID = "11111111-2222-3333-4444-555555555555"

runner = CliRunner()


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


@pytest.fixture
def verified_config(config_path: Path) -> Path:
    config_path.write_text(f'device_uuid = "{UUID}"\nverified_firmware = "V1.39.0518"\n', encoding="utf-8")
    return config_path


@pytest.fixture
def lamp(monkeypatch: pytest.MonkeyPatch) -> FakeLamp:
    """Replace the hardware layer: `find_lamp` returns the UUID and `make_transport` the fake lamp."""
    fake = FakeLamp()

    async def find_lamp(_device_uuid: str) -> BLEDevice:
        return BLEDevice(fake.address, "WL75", details=None)

    monkeypatch.setattr(client, "find_lamp", find_lamp)
    monkeypatch.setattr(client, "make_transport", lambda _device: fake)
    monkeypatch.setattr(client, "RETRY_DELAYS_SECONDS", (0, 0))
    return fake


def _invoke(config_path: Path, *arguments: str) -> tuple[int, str]:
    result = runner.invoke(app, ["--config-path", str(config_path), *arguments])
    return result.exit_code, result.output


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "scan" in result.output
    assert "inspect" in result.output
    assert "status" in result.output


def test_there_is_no_raw_send_command() -> None:
    commands = typer.main.get_command(app).commands  # ty: ignore[unresolved-attribute]
    assert set(commands) == {"scan", "inspect", "status", "start", "extend", "stop", "tick", "skip", "doctor"}


@pytest.mark.parametrize("arguments", [["--log-level", "invalid", "status"], ["scan", "--seconds", "0"], ["nonsense"]])
def test_invalid_usage(arguments: list[str]) -> None:
    assert runner.invoke(app, arguments).exit_code == 2


def test_status_prints_the_sleep_state(verified_config: Path, lamp: FakeLamp) -> None:
    exit_code, output = _invoke(verified_config, "status")

    assert exit_code == 0
    assert "sleep off | light sunset, brightness 99 | sound off, volume 6, fade on" in output
    assert lamp.written == [bytes.fromhex("00 75"), bytes.fromhex("1D")]
    assert lamp.disconnects == 1


def test_status_refuses_to_run_before_the_firmware_gate(config_path: Path, lamp: FakeLamp) -> None:
    config_path.write_text(f'device_uuid = "{UUID}"\n', encoding="utf-8")
    exit_code, output = _invoke(config_path, "status")

    assert exit_code == 2
    assert "verified_firmware" in output
    assert "Nothing was sent" in output
    assert lamp.connects == 0


def test_status_reports_an_invalid_configuration(config_path: Path, lamp: FakeLamp) -> None:
    config_path.write_text("volume = 99\n", encoding="utf-8")
    exit_code, output = _invoke(config_path, "status")

    assert exit_code == 2
    assert "volume must be an integer from 1 to 10" in output
    assert lamp.connects == 0


def test_status_fails_cleanly_when_the_lamp_does_not_answer(
    verified_config: Path, lamp: FakeLamp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "ANSWER_TIMEOUT_SECONDS", 0.05)
    lamp.unanswered.add(protocol.CMD_HANDSHAKE)
    exit_code, output = _invoke(verified_config, "status")

    assert exit_code == 1
    assert "No answer to 00 75" in output
    assert "Attempt 2 of 3" in output
    assert (lamp.connects, lamp.disconnects) == (3, 3)


def test_status_fails_cleanly_when_the_lamp_is_not_advertising(
    verified_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def find_lamp(_device_uuid: str) -> BLEDevice:
        message = "No WL75 is advertising."
        raise LampNotFoundError(message)

    monkeypatch.setattr(client, "find_lamp", find_lamp)
    monkeypatch.setattr(client, "RETRY_DELAYS_SECONDS", (0, 0))
    exit_code, output = _invoke(verified_config, "status")

    assert exit_code == 1
    assert "No WL75 is advertising." in output


def test_run_gives_up_after_the_overall_timeout(
    verified_config: Path, lamp: FakeLamp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "RUN_TIMEOUT_SECONDS", 0.05)
    lamp.unanswered.add(protocol.CMD_HANDSHAKE)
    exit_code, output = _invoke(verified_config, "status")

    assert exit_code == 1
    assert "Gave up after 0.05 s" in output
    assert lamp.disconnects == 1


def test_status_saves_a_changed_uuid(verified_config: Path, lamp: FakeLamp) -> None:
    lamp.address = "99999999-2222-3333-4444-555555555555"
    exit_code, output = _invoke(verified_config, "status")

    assert exit_code == 0
    assert "Found the lamp under a new UUID" in output
    assert load_config(verified_config).device_uuid == lamp.address


def test_start_uses_the_configuration_defaults(verified_config: Path, lamp: FakeLamp) -> None:
    exit_code, output = _invoke(verified_config, "start")

    assert exit_code == 0
    assert "sleep on, 60 min left | light off, brightness 99 | sound rain, volume 5, fade off" in output
    assert [payload.hex(" ") for payload in lamp.written] == [
        "00 75",
        "1d",
        "1e 01",
        "1f 01 3c 00 63",
        "1f 02 05 05 00",
        "1d",
    ]


def test_start_takes_options(verified_config: Path, lamp: FakeLamp) -> None:
    arguments = ["start", "--sound", "harmony", "--minutes", "3", "--volume", "2", "--light", "sunset", "--fade"]
    exit_code, output = _invoke(verified_config, *arguments)

    assert exit_code == 0
    assert "sleep on, 3 min left | light sunset, brightness 99 | sound harmony, volume 2, fade on" in output
    assert bytes.fromhex("1F 01 03 01 63") in lamp.written
    assert bytes.fromhex("1F 02 02 02 01") in lamp.written


@pytest.mark.parametrize(
    "arguments",
    [
        ["start", "--minutes", "61"],
        ["start", "--minutes", "0"],
        ["start", "--volume", "11"],
        ["start", "--sound", "waves"],
        ["start", "--light", "disco"],
        ["extend", "--minutes", "120"],
    ],
)
def test_out_of_range_options_are_refused_before_connecting(
    verified_config: Path, lamp: FakeLamp, arguments: list[str]
) -> None:
    exit_code, _ = _invoke(verified_config, *arguments)

    assert exit_code == 2
    assert lamp.connects == 0


@pytest.mark.parametrize("command", ["start", "extend", "stop"])
def test_writing_commands_refuse_to_run_before_the_firmware_gate(
    config_path: Path, lamp: FakeLamp, command: str
) -> None:
    config_path.write_text(f'device_uuid = "{UUID}"\n', encoding="utf-8")
    exit_code, output = _invoke(config_path, command)

    assert exit_code == 2
    assert "Nothing was sent" in output
    assert lamp.connects == 0


def test_extend_tops_up_a_running_sleep(verified_config: Path, lamp: FakeLamp) -> None:
    lamp.sleep = bytearray.fromhex("01 0E 00 63 05 05 00")
    exit_code, output = _invoke(verified_config, "extend", "--minutes", "45")

    assert exit_code == 0
    assert "sleep on, 45 min left" in output
    assert [payload.hex(" ") for payload in lamp.written] == ["00 75", "1d", "1f 01 2d 00 63", "1d"]


def test_extend_does_not_start_a_stopped_sleep(verified_config: Path, lamp: FakeLamp) -> None:
    exit_code, output = _invoke(verified_config, "extend")

    assert exit_code == 1
    assert "Sleep is not running" in output
    assert lamp.written == [bytes.fromhex("00 75"), bytes.fromhex("1D")]


def test_stop(verified_config: Path, lamp: FakeLamp) -> None:
    lamp.sleep[:2] = bytes([1, 30])
    exit_code, output = _invoke(verified_config, "stop")

    assert exit_code == 0
    assert "sleep off" in output
    assert [payload.hex(" ") for payload in lamp.written] == ["00 75", "1e 00", "1d"]


def test_a_failed_verification_is_reported_without_retrying(verified_config: Path, lamp: FakeLamp) -> None:
    lamp.canned_answers[protocol.CMD_SLEEP_STATUS] = bytes.fromhex("ED 01 1E 00 63 05 05 00")
    exit_code, output = _invoke(verified_config, "stop")

    assert exit_code == 1
    assert "The lamp is not in the expected state" in output
    assert lamp.connects == 1


def _patch_scan(monkeypatch: pytest.MonkeyPatch, sightings: list[Sighting]) -> None:
    async def scan(_seconds: float) -> list[Sighting]:
        return sightings

    monkeypatch.setattr(client, "scan", scan)


def test_scan_lists_the_lamp_and_saves_its_uuid(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_scan(monkeypatch, [Sighting(UUID, "WL75", rssi_samples=[-61, -63, -70])])
    exit_code, output = _invoke(config_path, "scan", "--seconds", "1")

    assert exit_code == 0
    assert f"{UUID}  name='WL75'" in output
    assert "3 samples, min -70, median -63, max -61 dBm" in output
    assert load_config(config_path).device_uuid == UUID


def test_scan_does_not_save_with_no_save(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_scan(monkeypatch, [Sighting(UUID, "WL75", rssi_samples=[-61])])
    exit_code, _ = _invoke(config_path, "scan", "--seconds", "1", "--no-save")

    assert exit_code == 0
    assert not config_path.exists()


def test_scan_does_not_guess_between_two_lamps(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_scan(
        monkeypatch, [Sighting(UUID, "WL75", rssi_samples=[-61]), Sighting("other", "WL75", rssi_samples=[-80])]
    )
    exit_code, output = _invoke(config_path, "scan", "--seconds", "1")

    assert exit_code == 0
    assert "Found 2 lamps" in output
    assert not config_path.exists()


def test_scan_fails_when_nothing_is_found(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_scan(monkeypatch, [])
    exit_code, output = _invoke(config_path, "scan", "--seconds", "1")

    assert exit_code == 1
    assert "No WL75 found" in output
    assert not config_path.exists()


def test_inspect_prints_the_report_without_needing_the_firmware_gate(
    config_path: Path, lamp: FakeLamp, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def inspect(device: BLEDevice, _listen_seconds: float) -> Inspection:
        return Inspection(device.address, {}, "V1.39.0518", "V2.0.0518", "V4", notifications=[])

    monkeypatch.setattr(client, "inspect", inspect)
    config_path.write_text(f'device_uuid = "{UUID}"\n', encoding="utf-8")
    exit_code, output = _invoke(config_path, "inspect", "--listen", "0")

    assert exit_code == 0
    assert f"Lamp {UUID}" in output
    assert "Firmware gate:" in output
    assert lamp.written == []


def test_log_file(verified_config: Path, lamp: FakeLamp, tmp_path: Path) -> None:
    log_file_path = tmp_path / "wl75-sleep.log"
    lamp.connect_error = OSError("no bluetooth")
    result = runner.invoke(
        app, ["--config-path", str(verified_config), "--log-file-path", str(log_file_path), "status"]
    )

    assert result.exit_code == 1
    assert "no bluetooth" in log_file_path.read_text(encoding="utf-8")


@pytest.mark.usefixtures("lamp")
def test_stderr_level_can_differ_from_the_log_file(verified_config: Path, tmp_path: Path) -> None:
    log_file_path = tmp_path / "wl75-sleep.log"
    arguments = ["--config-path", str(verified_config), "--log-file-path", str(log_file_path), "--stderr-level"]
    result = runner.invoke(app, [*arguments, "warning", "--log-level", "debug", "status"])

    assert result.exit_code == 0
    assert "-> fe ef 02 1d 1f" in log_file_path.read_text(encoding="utf-8")
    assert "-> fe ef" not in result.output
