import plistlib
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
lazy from pathlib import Path

import pytest
from typer.testing import CliRunner

from wl75_sleep import doctor, tick
from wl75_sleep.config import Config
from wl75_sleep.doctor import Facts, Level, examine, installed_tick_seconds, parse_mac_sleep_minutes
from wl75_sleep.main import app
from wl75_sleep.session import Night, State


ZONE = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 16, 8, 0, tzinfo=ZONE)
NIGHT = date(2026, 11, 15)
CONFIG = Config(device_uuid="uuid", verified_firmware="V1.39.0518", volume=6)
GOOD = Facts(
    config=CONFIG,
    config_problem=None,
    state=State(night=Night(NIGHT, starts=1, top_ups=11), last_tick=NOW - timedelta(minutes=3)),
    now=NOW,
    agent_loaded=True,
    tick_seconds=300,
    mac_sleep_minutes=0,
)


def test_everything_in_place() -> None:
    findings = examine(GOOD)
    assert all(finding.level is Level.OK for finding in findings)
    assert [finding.describe() for finding in findings] == [
        "ok    config       window 22:00 to 07:00, rain at volume 6, segments of 60 min, top-up after 45 min",
        "ok    agent        local.wl75-sleep is loaded, a tick every 300 s",
        "ok    last tick    Mon 07:57:00, 180 s ago",
        "ok    mac sleep    The Mac never sleeps.",
        "ok    latest night night of Sun 15 Nov: 1 start(s), 11 top-up(s), 0 gap(s), 0 failed attempt(s).",
    ]


@pytest.mark.parametrize(
    ("changes", "topic", "level", "text"),
    [
        ({"config": None, "config_problem": "volume must be an integer"}, "config", Level.FAIL, "volume must be"),
        ({"config": None}, "config", Level.FAIL, "cannot be read"),
        ({"config": Config(verified_firmware="V1.39.0518")}, "config", Level.FAIL, "wl75 scan"),
        ({"config": Config(device_uuid="uuid")}, "config", Level.FAIL, "wl75 inspect"),
        (
            {"config": replace(CONFIG, later_volume=4, later_volume_from=time(2, 0))},
            "config",
            Level.OK,
            "rain at volume 6, 4 from 02:00, segments",
        ),
        ({"agent_loaded": False}, "agent", Level.FAIL, "make install"),
        ({"tick_seconds": None}, "agent", Level.OK, "unknown interval"),
        ({"state": State()}, "last tick", Level.WARN, "No tick has run yet"),
        ({"state": State(last_tick=NOW - timedelta(minutes=10))}, "last tick", Level.WARN, "later than the interval"),
        (
            {"state": State(last_tick=NOW - timedelta(minutes=10)), "tick_seconds": None},
            "last tick",
            Level.OK,
            "600 s ago",
        ),
        ({"mac_sleep_minutes": 10}, "mac sleep", Level.FAIL, "sleeps after 10 min"),
        ({"mac_sleep_minutes": None}, "mac sleep", Level.WARN, "Could not read"),
        ({"state": State(night=Night(NIGHT, gaps=1))}, "latest night", Level.WARN, "1 gap(s)"),
        ({"state": State(night=Night(NIGHT, failures=4))}, "latest night", Level.WARN, "4 failed attempt(s)"),
        ({"state": State(night=Night(NIGHT, stopped_by_hand=True))}, "latest night", Level.OK, "stopped by hand"),
        (
            {"state": State(skip_night=NIGHT)},
            "latest night",
            Level.OK,
            "No night recorded yet. Skipped: the night of Sun",
        ),
    ],
)
def test_findings(changes: dict[str, object], topic: str, level: Level, text: str) -> None:
    findings = {finding.topic: finding for finding in examine(replace(GOOD, **changes))}
    assert findings[topic].level is level
    assert text in findings[topic].detail


def test_installed_tick_seconds(tmp_path: Path) -> None:
    plist = tmp_path / "agent.plist"
    assert installed_tick_seconds(plist) is None
    plist.write_bytes(plistlib.dumps({"Label": doctor.LABEL, "StartInterval": 300}))
    assert installed_tick_seconds(plist) == 300
    plist.write_bytes(plistlib.dumps({"Label": doctor.LABEL}))
    assert installed_tick_seconds(plist) is None
    plist.write_text("not a plist", encoding="utf-8")
    assert installed_tick_seconds(plist) is None


def test_parse_mac_sleep_minutes() -> None:
    output = "Currently in use:\n standby              0\n sleep                0 (sleep prevented by coreaudiod)\n"
    assert parse_mac_sleep_minutes(output) == 0
    assert parse_mac_sleep_minutes(" displaysleep 10\n sleep 15\n") == 15
    assert parse_mac_sleep_minutes("nothing here") is None


def _cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_text: str) -> tuple[int, str]:
    config_path = tmp_path / "config.toml"
    config_path.write_text(config_text, encoding="utf-8")
    state_path = tmp_path / "state.json"
    tick.save_state(state_path, State(last_tick=NOW))
    monkeypatch.setattr(tick, "local_now", lambda: NOW)
    monkeypatch.setattr(doctor, "agent_loaded", lambda: True)
    monkeypatch.setattr(doctor, "installed_tick_seconds", lambda: 300)
    monkeypatch.setattr(doctor, "mac_sleep_minutes", lambda: 0)
    result = CliRunner().invoke(app, ["--config-path", str(config_path), "--state-path", str(state_path), "doctor"])
    return result.exit_code, result.output


def test_cli_doctor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exit_code, output = _cli(monkeypatch, tmp_path, 'device_uuid = "uuid"\nverified_firmware = "V1.39.0518"\n')
    assert exit_code == 0
    assert "ok    agent" in output
    assert "warn" not in output.split("last tick")[1].split("\n")[0]


def test_cli_doctor_fails_on_a_broken_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exit_code, output = _cli(monkeypatch, tmp_path, "volume = 99\n")
    assert exit_code == 1
    assert "FAIL  config" in output
    assert "volume must be an integer from 1 to 10" in output
