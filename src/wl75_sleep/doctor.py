"""`wl75 doctor`: check that everything the scheduler needs is in place, without touching Bluetooth.

The checks are a pure function of `Facts`. Gathering the facts from `launchctl` and `pmset` is the only part that needs
the real Mac.
"""

import enum
import os
import plistlib
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from wl75_sleep import protocol, tick
lazy from wl75_sleep.config import Config
lazy from wl75_sleep.session import State


LABEL = "local.wl75-sleep"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
# A tick may be late by its run time, and launchd may delay a tick a little.
TICK_SLACK = timedelta(seconds=tick.TICK_TIMEOUT_SECONDS + 30)


class Level(enum.StrEnum):
    """How serious a finding is."""

    OK = "ok"
    WARN = "warn"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class Finding:
    """One line of the report."""

    level: Level
    topic: str
    detail: str

    def describe(self) -> str:
        """Return the line as printed."""
        return f"{self.level:<5} {self.topic:<12} {self.detail}"


@dataclass(frozen=True, slots=True)
class Facts:
    """Everything the checks look at."""

    config: Config | None
    config_problem: str | None
    state: State
    now: datetime
    agent_loaded: bool
    tick_seconds: int | None
    mac_sleep_minutes: int | None


def examine(facts: Facts) -> list[Finding]:
    """Return one finding per check."""
    return [
        _check_config(facts),
        _check_agent(facts),
        _check_last_tick(facts),
        _check_mac_sleep(facts),
        _check_latest_night(facts),
    ]


def _check_config(facts: Facts) -> Finding:
    config = facts.config
    if config is None:
        return Finding(Level.FAIL, "config", facts.config_problem or "The configuration cannot be read.")
    if not config.device_uuid:
        return Finding(Level.FAIL, "config", "`device_uuid` is not set. Run `wl75 scan`.")
    if config.verified_firmware != protocol.DOCUMENTED_SOFTWARE_REVISION:
        return Finding(Level.FAIL, "config", "`verified_firmware` is not the documented firmware. Run `wl75 inspect`.")
    volume = f"volume {config.volume}"
    if config.later_volume is not None and config.later_volume_from is not None:
        volume += f", {config.later_volume} from {config.later_volume_from:%H:%M}"
    return Finding(
        Level.OK,
        "config",
        f"window {config.window_start:%H:%M} to {config.window_end:%H:%M}, {config.sound.name.lower()} at {volume}, "
        f"segments of {config.segment_minutes} min, top-up after {config.renew_after_minutes} min",
    )


def _check_agent(facts: Facts) -> Finding:
    if not facts.agent_loaded:
        return Finding(Level.FAIL, "agent", f"{LABEL} is not loaded. Run `make install`.")
    interval = "unknown interval" if facts.tick_seconds is None else f"a tick every {facts.tick_seconds} s"
    return Finding(Level.OK, "agent", f"{LABEL} is loaded, {interval}")


def _check_last_tick(facts: Facts) -> Finding:
    last_tick = facts.state.last_tick
    if last_tick is None:
        return Finding(Level.WARN, "last tick", "No tick has run yet.")
    age = facts.now - last_tick
    detail = f"{last_tick:%a %H:%M:%S}, {int(age.total_seconds())} s ago"
    if facts.tick_seconds is not None and age > timedelta(seconds=facts.tick_seconds) + TICK_SLACK:
        return Finding(Level.WARN, "last tick", f"{detail}: later than the interval allows.")
    return Finding(Level.OK, "last tick", detail)


def _check_mac_sleep(facts: Facts) -> Finding:
    minutes = facts.mac_sleep_minutes
    if minutes is None:
        return Finding(Level.WARN, "mac sleep", "Could not read the sleep setting from `pmset -g`.")
    if minutes:
        return Finding(Level.FAIL, "mac sleep", f"The Mac sleeps after {minutes} min idle. Ticks stop while it sleeps.")
    return Finding(Level.OK, "mac sleep", "The Mac never sleeps.")


def _check_latest_night(facts: Facts) -> Finding:
    night = facts.state.night
    skipped = "" if facts.state.skip_night is None else f" Skipped: the night of {facts.state.skip_night:%a %d %b}."
    if night is None:
        return Finding(Level.OK, "latest night", f"No night recorded yet.{skipped}")
    detail = (
        f"night of {night.night:%a %d %b}: {night.starts} start(s), {night.top_ups} top-up(s), {night.gaps} gap(s), "
        f"{night.failures} failed attempt(s)"
    )
    if night.stopped_by_hand:
        detail += ", stopped by hand"
    level = Level.WARN if night.gaps or night.failures else Level.OK
    return Finding(level, "latest night", detail + "." + skipped)


def installed_tick_seconds(plist_path: Path = PLIST_PATH) -> int | None:
    """Return the tick interval of the installed LaunchAgent, or `None` if it is not installed."""
    try:
        with plist_path.open("rb") as handle:
            interval = plistlib.load(handle).get("StartInterval")
    except OSError, plistlib.InvalidFileException:
        return None
    return interval if isinstance(interval, int) else None


def parse_mac_sleep_minutes(pmset_output: str) -> int | None:
    """Return the system sleep timer from the output of `pmset -g`, in minutes. 0 means never."""
    match = re.search(r"^\s*sleep\s+(\d+)", pmset_output, flags=re.MULTILINE)
    return None if match is None else int(match.group(1))


def agent_loaded() -> bool:  # pragma: no cover
    """Return `True` if launchd has the LaunchAgent loaded in this user's GUI domain."""
    result = subprocess.run(  # noqa: S603
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, check=False
    )
    return result.returncode == 0


def mac_sleep_minutes() -> int | None:  # pragma: no cover
    """Return the Mac's system sleep timer in minutes, or `None` if it cannot be read."""
    result = subprocess.run(["/usr/bin/pmset", "-g"], capture_output=True, text=True, check=False)
    return parse_mac_sleep_minutes(result.stdout)
