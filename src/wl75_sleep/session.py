"""Decision logic of the scheduler: what one tick does, given the configuration, the saved state and the time.

Pure: no Bluetooth, no files and no clock. `tick.py` supplies all three.

A night is one window, from `window_start` to `window_end`, named after the date it starts on. Each tick decides
without touching the lamp whether to start, to top up or to do nothing. A start or top-up then reads the lamp's status
first, and `step_on_lamp` picks what to send from it.

The volume is sent at the start and again at `later_volume_from`, if set. That change is a top-up that also sends the
sound settings, so a volume the user sets in the app between those two moments stays.

Window boundaries take the UTC offset of `now`. On the two nights a year when the clock changes, the window is one
hour off at its far end.
"""

import enum
import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, tzinfo

lazy from wl75_sleep.config import Config


# The lamp ends a run within a second of its countdown. Found off earlier than this before our expected end, it was
# stopped by hand.
STOP_MARGIN = timedelta(seconds=30)
# Minutes sent are rounded down, so the last segment ends up to a minute before the window does.
LAST_SEGMENT_MARGIN = timedelta(minutes=1)
# Ticks come at fixed intervals, and a success is recorded seconds after its tick began. Without this slack, a top-up
# due at minute 45 would just miss the tick at minute 45 and wait for the next one, which costs it a chance.
DUE_SLACK = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class Window:
    """One night's window."""

    night: date
    start: datetime
    end: datetime


def window_of(config: Config, night: date, zone: tzinfo | None) -> Window:
    """Return the window that starts on `night`."""
    start = datetime.combine(night, config.window_start, tzinfo=zone)
    end_day = night if config.window_end > config.window_start else night + timedelta(days=1)
    return Window(night, start, datetime.combine(end_day, config.window_end, tzinfo=zone))


def window_at(config: Config, now: datetime) -> Window | None:
    """Return the window that contains `now`, or `None` outside every window."""
    for night in (now.date() - timedelta(days=1), now.date()):
        window = window_of(config, night, now.tzinfo)
        if window.start <= now < window.end:
            return window
    return None


def night_to_skip(config: Config, now: datetime) -> date:
    """Return the night `wl75 skip` suppresses: the one under way, or else the next one to start."""
    window = window_at(config, now)
    if window is not None:
        return window.night
    if now < window_of(config, now.date(), now.tzinfo).start:
        return now.date()
    return now.date() + timedelta(days=1)


def volume_change_at(config: Config, window: Window) -> datetime | None:
    """Return when `window`'s night switches to `later_volume`, or `None` if it never does."""
    change = config.later_volume_from
    if change is None:
        return None
    day = window.night if change >= config.window_start else window.night + timedelta(days=1)
    return datetime.combine(day, change, tzinfo=window.start.tzinfo)


def volume_at(config: Config, window: Window, now: datetime) -> int:
    """Return the volume the lamp should play at `now`."""
    change = volume_change_at(config, window)
    if change is None or config.later_volume is None or now < change:
        return config.volume
    return config.later_volume


def minutes_for(config: Config, window: Window, now: datetime) -> int:
    """Return the minutes to send at `now`: a full segment, or what is left of the window, rounded down."""
    left = math.floor((window.end - now).total_seconds() / 60)
    return min(config.segment_minutes, left)


@dataclass(frozen=True, slots=True)
class Night:
    """What happened in one night so far."""

    night: date
    last_success: datetime | None = None
    expires_at: datetime | None = None
    stopped_by_hand: bool = False
    # The volume of tonight's schedule at the last success. `None` means not known, as in a state from before volume
    # changes existed.
    volume: int | None = None
    starts: int = 0
    top_ups: int = 0
    gaps: int = 0
    failures: int = 0


@dataclass(frozen=True, slots=True)
class State:
    """Everything the scheduler keeps between ticks."""

    night: Night | None = None
    skip_night: date | None = None
    last_tick: datetime | None = None

    def night_of(self, window: Window) -> Night:
        """Return the record of `window`'s night, or a fresh one if the saved record is from another night."""
        if self.night is not None and self.night.night == window.night:
            return self.night
        return Night(window.night)


class Action(enum.StrEnum):
    """What a tick does, decided without the lamp."""

    NOTHING = "nothing"
    START = "start"
    TOP_UP = "top-up"


@dataclass(frozen=True, slots=True)
class Decision:
    """The action of one tick, why, and the window it belongs to."""

    action: Action
    reason: str
    window: Window | None = None


def decide(config: Config, state: State, now: datetime) -> Decision:  # noqa: PLR0911
    """Decide what the tick at `now` does."""
    window = window_at(config, now)
    if window is None:
        return Decision(Action.NOTHING, "outside the window")
    if state.skip_night == window.night:
        return Decision(Action.NOTHING, "tonight is skipped", window)
    if minutes_for(config, window, now) < 1:
        return Decision(Action.NOTHING, "the window ends in less than a minute", window)
    night = state.night_of(window)
    if night.stopped_by_hand:
        return Decision(Action.NOTHING, "Sleep was stopped by hand tonight", window)
    if night.last_success is None or night.expires_at is None:
        return Decision(Action.START, "no session tonight yet", window)
    volume = volume_at(config, window, now)
    if night.volume != volume:
        return Decision(Action.TOP_UP, f"the volume changes to {volume}", window)
    if night.expires_at >= window.end - LAST_SEGMENT_MARGIN:
        return Decision(Action.NOTHING, f"the last segment runs until {night.expires_at:%H:%M}", window)
    due = night.last_success + timedelta(minutes=config.renew_after_minutes)
    if now >= due - DUE_SLACK:
        return Decision(Action.TOP_UP, f"the top-up was due at {due:%H:%M}", window)
    return Decision(Action.NOTHING, f"next top-up at {due:%H:%M}", window)


class Step(enum.StrEnum):
    """What a start or top-up sends, chosen after reading the lamp's status."""

    START = "start"
    EXTEND = "extend"
    # A top-up that also sends the sound settings, for a volume change.
    RECONFIGURE = "reconfigure"
    STOPPED_BY_HAND = "stopped by hand"


def step_on_lamp(  # noqa: PLR0913
    action: Action, night: Night, *, is_on: bool, now: datetime, attempted_start: bool, volume: int
) -> Step:
    """Choose what to send, given what the lamp reports. `volume` is the one `volume_at` gives for `now`.

    `attempted_start` is `True` if an earlier attempt of the same tick began a start sequence. A run found on after that
    may still lack our sound settings, so it is started again in full rather than only extended.

    A lamp found off while a volume change is due is started again rather than taken as stopped by hand: a failed
    change may have been what stopped it.
    """
    volume_changes = action is Action.TOP_UP and night.volume != volume
    if is_on:
        if attempted_start:
            return Step.START
        return Step.RECONFIGURE if volume_changes else Step.EXTEND
    stopped_early = night.expires_at is not None and now < night.expires_at - STOP_MARGIN
    if action is Action.TOP_UP and stopped_early and not volume_changes:
        return Step.STOPPED_BY_HAND
    return Step.START


def record_success(  # noqa: PLR0913
    state: State, night: Night, action: Action, step: Step, *, minutes: int, volume: int, now: datetime
) -> State:
    """Return the state after `step`, chosen for `action`, succeeded at `now` with a run of `minutes`.

    `volume` is the one `volume_at` gave. An adopted run keeps its own volume until the next scheduled change.
    """
    if step is Step.STOPPED_BY_HAND:
        return replace(state, night=replace(night, stopped_by_hand=True))
    started = step is Step.START
    updated = replace(
        night,
        last_success=now,
        expires_at=now + timedelta(minutes=minutes),
        volume=volume,
        starts=night.starts + started,
        top_ups=night.top_ups + (not started),
        # A top-up that finds the lamp off after its timer ran out restarts the rain: there was a gap.
        gaps=night.gaps + (started and action is Action.TOP_UP),
    )
    return replace(state, night=updated)


def record_failure(state: State, night: Night) -> State:
    """Return the state after a start or top-up failed."""
    return replace(state, night=replace(night, failures=night.failures + 1))
