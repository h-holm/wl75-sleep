"""One scheduler tick: lock, decide, act on the lamp if needed, save the state, and log one line.

launchd runs a tick every few minutes. A tick with nothing to do exits without touching Bluetooth. A start or top-up
gets one run against the lamp, capped at `TICK_TIMEOUT_SECONDS`. If it fails, the next tick tries again.
"""

import asyncio
import contextlib
import fcntl
import json
import os
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any
lazy from collections.abc import Callable, Generator

from loguru import logger

from wl75_sleep import client, session
from wl75_sleep.client import LampError, LampSession
from wl75_sleep.config import Config, save_value
from wl75_sleep.session import Action, Decision, Night, State, Step
lazy from wl75_sleep.protocol import SleepState


STATE_PATH = Path.home() / "Library" / "Application Support" / "wl75-sleep" / "state.json"
LOG_PATH = Path.home() / "Library" / "Logs" / "wl75-sleep" / "wl75-sleep.log"
# One run against the lamp, including all its attempts. Ticks are minutes apart, so they never overlap.
TICK_TIMEOUT_SECONDS = 90.0


def local_now() -> datetime:
    """Return the current local time with its UTC offset."""
    return datetime.now().astimezone()


class StateError(Exception):
    """The state file cannot be read."""


def _encode(value: object) -> object:
    return value.isoformat() if isinstance(value, date) else value


def state_to_json(state: State) -> str:
    """Serialise `state`."""
    night = None
    if state.night is not None:
        night = {name: _encode(getattr(state.night, name)) for name in Night.__dataclass_fields__}
    data = {"night": night, "skip_night": _encode(state.skip_night), "last_tick": _encode(state.last_tick)}
    return json.dumps(data, indent=2) + "\n"


def state_from_json(text: str) -> State:
    """Deserialise a state written by `state_to_json`.

    Raises:
        StateError: If `text` is not such a state.
    """
    try:
        data: dict[str, Any] = json.loads(text)
        night = data["night"]
        return State(
            night=None
            if night is None
            else Night(
                night=date.fromisoformat(night["night"]),
                last_success=_optional_datetime(night["last_success"]),
                expires_at=_optional_datetime(night["expires_at"]),
                stopped_by_hand=bool(night["stopped_by_hand"]),
                volume=night.get("volume"),
                starts=int(night["starts"]),
                top_ups=int(night["top_ups"]),
                gaps=int(night["gaps"]),
                failures=int(night["failures"]),
            ),
            skip_night=None if data["skip_night"] is None else date.fromisoformat(data["skip_night"]),
            last_tick=_optional_datetime(data["last_tick"]),
        )
    except (ValueError, KeyError, TypeError) as error:
        message = f"Unreadable state: {error!r}"
        raise StateError(message) from error


def _optional_datetime(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def load_state(path: Path) -> State:
    """Load the state from `path`. A missing or unreadable file yields a fresh state; the latter is logged."""
    if not path.is_file():
        return State()
    try:
        return state_from_json(path.read_text(encoding="utf-8"))
    except StateError as error:
        logger.warning(f"{path}: {error}. Starting from a fresh state.")
        return State()


def save_state(path: Path, state: State) -> None:
    """Write `state` to `path` atomically, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(state_to_json(state), encoding="utf-8")
    temporary.replace(path)


@contextlib.contextmanager
def locked(path: Path) -> Generator[bool]:
    """Hold an exclusive lock on `path` for the block. Yield `False` at once if another process holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(descriptor)


@dataclass(slots=True)
class Stopwatch:
    """How long a tick took, and how long until the lamp was found."""

    started: float = field(default_factory=time.monotonic)
    found_after: float | None = None

    def found(self) -> None:
        """Note that the lamp was found. On a later attempt, this overwrites the earlier time."""
        self.found_after = time.monotonic() - self.started

    def describe(self) -> str:
        """Return the times for the log line."""
        found = "lamp not found" if self.found_after is None else f"found after {self.found_after:.1f} s"
        return f"{found}, took {time.monotonic() - self.started:.1f} s"


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a start or top-up did on the lamp."""

    step: Step
    minutes: int
    volume: int
    status: SleepState
    finished_at: datetime


async def act(  # noqa: PLR0913
    config: Config,
    config_path: Path,
    decision: Decision,
    *,
    night: Night,
    now: Callable[[], datetime],
    stopwatch: Stopwatch,
) -> Outcome:
    """Run a start or top-up on the lamp: read the status, choose the step, send it.

    Raises:
        LampError: If the lamp cannot be reached, refuses a command or ends in an unexpected state.
    """
    if decision.window is None:  # pragma: no cover
        message = "A start or top-up needs a window."
        raise ValueError(message)
    window = decision.window
    attempted_start = False

    async def operation(lamp: LampSession) -> Outcome:
        nonlocal attempted_start
        before = await lamp.status()
        moment = now()
        volume = session.volume_at(config, window, moment)
        step = session.step_on_lamp(
            decision.action, night, is_on=before.is_on, now=moment, attempted_start=attempted_start, volume=volume
        )
        # Rounded down; at least 1, since a run cannot be shorter. The decision already refused a window that ends
        # in less than a minute, so this overshoots the window end by seconds at most.
        minutes = max(1, session.minutes_for(config, window, moment))
        if step is Step.STOPPED_BY_HAND:
            after = before
        elif step is Step.EXTEND:
            after = await client.extend_sleep(lamp, minutes=minutes, light=config.renew_light)
            if not after.is_on:
                message = "Sleep stopped between two status queries."
                raise LampError(message)
        else:
            # A reconfigure finds Sleep running, so `start_sleep` only sends the light and sound settings.
            starting = step is Step.START
            attempted_start = attempted_start or starting
            after = await client.start_sleep(
                lamp,
                minutes=minutes,
                light=config.start_light if starting else config.renew_light,
                sound=config.sound,
                volume=volume,
                fade_out=config.fade_last_segment,
            )
        return Outcome(step, minutes, volume, after, now())

    def on_found(address: str) -> None:
        stopwatch.found()
        if address.casefold() != config.device_uuid.casefold():
            logger.warning(f"Found the lamp under a new UUID. Saving {address} to {config_path}.")
            save_value("device_uuid", address, config_path)

    async with asyncio.timeout(TICK_TIMEOUT_SECONDS):
        return await client.run_on_lamp(config.device_uuid, operation, on_found)


def run_tick(
    config: Config, config_path: Path, state_path: Path = STATE_PATH, now: Callable[[], datetime] | None = None
) -> bool:
    """Run one tick. Return `False` if a start or top-up failed, `True` otherwise. `now` defaults to `local_now`."""
    now = now or local_now
    with locked(state_path.with_suffix(".lock")) as acquired:
        if not acquired:
            logger.warning("tick: another tick is still running. Nothing done.")
            return True
        state = load_state(state_path)
        moment = now()
        decision = session.decide(config, state, moment)
        state = replace(state, last_tick=moment)
        if decision.action is Action.NOTHING or decision.window is None:
            save_state(state_path, state)
            logger.info(f"tick: nothing, {decision.reason}.")
            return True

        night = state.night_of(decision.window)
        stopwatch = Stopwatch()
        try:
            outcome = asyncio.run(act(config, config_path, decision, night=night, now=now, stopwatch=stopwatch))
        except (LampError, TimeoutError) as error:
            save_state(state_path, session.record_failure(state, night))
            reason = str(error) or f"no result within {TICK_TIMEOUT_SECONDS:g} s"
            logger.warning(f"tick: {decision.action} failed, {decision.reason}: {reason} ({stopwatch.describe()}).")
            return False

        state = session.record_success(
            state,
            night,
            decision.action,
            outcome.step,
            minutes=outcome.minutes,
            volume=outcome.volume,
            now=outcome.finished_at,
        )
        save_state(state_path, state)
        logger.info(
            f"tick: {decision.action} -> {_describe(decision, outcome)}, {decision.reason} ({stopwatch.describe()}). "
            f"Lamp: {outcome.status.describe()}"
        )
        if decision.action is Action.TOP_UP and outcome.step is Step.START:
            logger.warning("tick: gap. The lamp's timer had run out before the top-up got through.")
        return True


def _describe(decision: Decision, outcome: Outcome) -> str:
    if outcome.step is Step.STOPPED_BY_HAND:
        return "Sleep was stopped by hand, nothing more tonight"
    if decision.action is Action.START and outcome.step is Step.EXTEND:
        return f"adopted the running Sleep, {outcome.minutes} min"
    if outcome.step is Step.RECONFIGURE:
        return f"reconfigure, {outcome.minutes} min at volume {outcome.volume}"
    return f"{outcome.step}, {outcome.minutes} min"
