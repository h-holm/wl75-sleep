from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

import pytest

from wl75_sleep.config import Config
from wl75_sleep.session import (
    Action,
    Decision,
    Night,
    State,
    Step,
    decide,
    minutes_for,
    night_to_skip,
    record_failure,
    record_success,
    step_on_lamp,
    volume_at,
    window_at,
)


ZONE = timezone(timedelta(hours=1))
CONFIG = Config(window_start=time(22, 0), window_end=time(7, 0), renew_after_minutes=45, volume=6)
# The same window, with the volume lowered at 01:00 after midnight.
LATER = replace(CONFIG, later_volume=4, later_volume_from=time(1, 0))
NIGHT = date(2026, 11, 15)


def at(hour: int, minute: int = 0, second: int = 0, *, day: int = 15) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=ZONE)


def running(last_success: datetime, minutes: int = 60, **changes: object) -> State:
    night = Night(NIGHT, last_success=last_success, expires_at=last_success + timedelta(minutes=minutes), volume=6)
    return State(night=replace(night, **changes))


@pytest.mark.parametrize(
    ("state", "now", "action", "reason"),
    [
        (State(), at(12), Action.NOTHING, "outside the window"),
        (State(), at(21, 59), Action.NOTHING, "outside the window"),
        (State(), at(7, day=16), Action.NOTHING, "outside the window"),
        (State(), at(22), Action.START, "no session tonight yet"),
        (State(), at(2, day=16), Action.START, "no session tonight yet"),
        (State(skip_night=NIGHT), at(22), Action.NOTHING, "tonight is skipped"),
        (State(skip_night=NIGHT - timedelta(days=1)), at(22), Action.START, "no session tonight yet"),
        (State(), at(6, 59, 30, day=16), Action.NOTHING, "less than a minute"),
        # Failed starts leave no success behind, so every tick tries again.
        (State(night=Night(NIGHT, failures=3)), at(22, 15), Action.START, "no session tonight yet"),
        # A record from the night before does not count tonight.
        (
            State(night=Night(date(2026, 11, 14), last_success=at(6, day=15), expires_at=at(7, day=15))),
            at(22),
            Action.START,
            "no session tonight yet",
        ),
        (running(at(22)), at(22, 40), Action.NOTHING, "next top-up at 22:45"),
        (running(at(22)), at(22, 45), Action.TOP_UP, "due at 22:45"),
        # The success of the 22:00 tick was recorded seconds later. The 22:45 tick still tops up.
        (running(at(22, 0, 20)), at(22, 45), Action.TOP_UP, "due at 22:45"),
        (running(at(22, 0, 20)), at(22, 44, 19), Action.NOTHING, "next top-up at 22:45"),
        # Failed top-ups are tried again, also after the lamp's timer must have run out.
        (running(at(22), failures=2), at(22, 55), Action.TOP_UP, "due at 22:45"),
        (running(at(22)), at(23, 10), Action.TOP_UP, "due at 22:45"),
        (running(at(22), stopped_by_hand=True), at(22, 50), Action.NOTHING, "stopped by hand"),
        # The last segment was capped at the window end. No more top-ups.
        (running(at(6, 5, day=16), minutes=55), at(6, 55, day=16), Action.NOTHING, "last segment runs until 07:00"),
        (running(at(6, 5, 20, day=16), minutes=54), at(6, 55, day=16), Action.NOTHING, "last segment"),
        # The segment before the last one is a full one and still gets a top-up.
        (running(at(5, 58, day=16)), at(6, 43, day=16), Action.TOP_UP, "due at 06:43"),
    ],
)
def test_decide(state: State, now: datetime, action: Action, reason: str) -> None:
    decision = decide(CONFIG, state, now)
    assert decision.action is action
    assert reason in decision.reason


@pytest.mark.parametrize(
    ("state", "now", "action", "reason"),
    [
        (running(at(0, 45, day=16)), at(0, 55, day=16), Action.NOTHING, "next top-up at 01:30"),
        (running(at(0, 45, day=16)), at(1, 0, day=16), Action.TOP_UP, "the volume changes to 4"),
        (running(at(1, 0, day=16), volume=4), at(1, 5, day=16), Action.NOTHING, "next top-up at 01:45"),
        # The change also comes during the last segment.
        (running(at(6, 5, day=16), minutes=55), at(6, 55, day=16), Action.TOP_UP, "the volume changes to 4"),
        (running(at(22), stopped_by_hand=True), at(1, day=16), Action.NOTHING, "stopped by hand"),
        # A state saved before volume changes existed gets the volume sent once.
        (running(at(22), volume=None), at(22, 5), Action.TOP_UP, "the volume changes to 6"),
    ],
)
def test_decide_a_volume_change(state: State, now: datetime, action: Action, reason: str) -> None:
    decision = decide(LATER, state, now)
    assert decision.action is action
    assert reason in decision.reason


@pytest.mark.parametrize(
    ("config", "now", "volume"),
    [
        (CONFIG, at(23), 6),
        (LATER, at(23), 6),
        (LATER, at(0, 59, 59, day=16), 6),
        (LATER, at(1, day=16), 4),
        (LATER, at(6, 59, day=16), 4),
        (replace(LATER, later_volume_from=time(23, 0)), at(23), 4),
        (replace(LATER, later_volume_from=time(23, 0)), at(22, 59), 6),
    ],
)
def test_volume_at(config: Config, now: datetime, volume: int) -> None:
    window = window_at(config, now)
    assert window is not None
    assert volume_at(config, window, now) == volume


def test_a_window_after_midnight_belongs_to_the_night_before() -> None:
    window = window_at(CONFIG, at(3, day=16))
    assert window is not None
    assert (window.night, window.start, window.end) == (NIGHT, at(22), at(7, day=16))


def test_a_window_within_one_day() -> None:
    config = Config(window_start=time(13, 0), window_end=time(13, 30))
    assert window_at(config, at(12, 59)) is None
    assert window_at(config, at(13, 30)) is None
    window = window_at(config, at(13, 10))
    assert window is not None
    assert (window.night, window.end) == (NIGHT, at(13, 30))
    assert decide(config, State(), at(13, 10)).action is Action.START


@pytest.mark.parametrize(
    ("now", "minutes"),
    [
        (at(22), 60),
        (at(5, 58, day=16), 60),
        (at(6, 5, day=16), 55),
        (at(6, 5, 20, day=16), 54),
        (at(6, 59, 59, day=16), 0),
    ],
)
def test_minutes_for(now: datetime, minutes: int) -> None:
    window = window_at(CONFIG, now)
    assert window is not None
    assert minutes_for(CONFIG, window, now) == minutes


def test_minutes_for_uses_the_segment_length() -> None:
    config = Config(window_start=time(13, 0), window_end=time(13, 30), segment_minutes=3, renew_after_minutes=2)
    window = window_at(config, at(13))
    assert window is not None
    assert minutes_for(config, window, at(13)) == 3
    assert minutes_for(config, window, at(13, 28)) == 2


@pytest.mark.parametrize(
    ("config", "now", "night"),
    [
        (CONFIG, at(12), NIGHT),
        (CONFIG, at(23), NIGHT),
        (CONFIG, at(2, day=16), NIGHT),
        (CONFIG, at(8, day=16), date(2026, 11, 16)),
        (Config(window_start=time(13, 0), window_end=time(13, 30)), at(14), date(2026, 11, 16)),
    ],
)
def test_night_to_skip(config: Config, now: datetime, night: date) -> None:
    assert night_to_skip(config, now) == night


@pytest.mark.parametrize(
    ("action", "is_on", "now", "attempted_start", "volume", "step"),
    [
        (Action.START, False, at(22), False, 6, Step.START),
        # Sleep already running at window start: adopt it and only top up.
        (Action.START, True, at(22), False, 6, Step.EXTEND),
        # An earlier attempt of this tick began a start: configure the run in full.
        (Action.START, True, at(22), True, 6, Step.START),
        (Action.TOP_UP, True, at(22, 45), False, 6, Step.EXTEND),
        # Off while our last top-up should still be running: stopped on the clock.
        (Action.TOP_UP, False, at(22, 45), False, 6, Step.STOPPED_BY_HAND),
        (Action.TOP_UP, False, at(22, 59, 29), False, 6, Step.STOPPED_BY_HAND),
        # Off after our last top-up has expired: we missed it. Start again.
        (Action.TOP_UP, False, at(22, 59, 30), False, 6, Step.START),
        (Action.TOP_UP, False, at(23, 5), False, 6, Step.START),
        # A volume change on a running lamp sends the sound settings with the top-up.
        (Action.TOP_UP, True, at(22, 45), False, 4, Step.RECONFIGURE),
        (Action.TOP_UP, True, at(22, 45), True, 4, Step.START),
        # Off while a volume change is due: the change may have stopped it, so start again.
        (Action.TOP_UP, False, at(22, 45), False, 4, Step.START),
        # An adopted run keeps its own volume.
        (Action.START, True, at(22), False, 4, Step.EXTEND),
    ],
)
def test_step_on_lamp(  # noqa: PLR0913, PLR0917
    action: Action, is_on: bool, now: datetime, attempted_start: bool, volume: int, step: Step
) -> None:
    night = Night(NIGHT, last_success=at(22), expires_at=at(23), volume=6)
    assert step_on_lamp(action, night, is_on=is_on, now=now, attempted_start=attempted_start, volume=volume) is step


def test_record_a_start() -> None:
    state = record_success(State(), Night(NIGHT), Action.START, Step.START, minutes=60, volume=6, now=at(22))
    assert state.night == Night(NIGHT, last_success=at(22), expires_at=at(23), starts=1, volume=6)


def test_record_a_top_up() -> None:
    night = Night(NIGHT, last_success=at(22), expires_at=at(23), starts=1, volume=6)
    state = record_success(State(night=night), night, Action.TOP_UP, Step.EXTEND, minutes=60, volume=6, now=at(22, 45))
    assert state.night == replace(night, last_success=at(22, 45), expires_at=at(23, 45), top_ups=1)


def test_record_a_volume_change_as_a_top_up() -> None:
    night = Night(NIGHT, last_success=at(22), expires_at=at(23), starts=1, volume=6)
    state = record_success(
        State(night=night), night, Action.TOP_UP, Step.RECONFIGURE, minutes=60, volume=4, now=at(22, 30)
    )
    assert state.night == replace(night, last_success=at(22, 30), expires_at=at(23, 30), top_ups=1, volume=4)


def test_record_a_missed_top_up_as_a_gap() -> None:
    night = Night(NIGHT, last_success=at(22), expires_at=at(23), starts=1)
    state = record_success(State(night=night), night, Action.TOP_UP, Step.START, minutes=60, volume=6, now=at(23, 5))
    assert state.night is not None
    assert (state.night.starts, state.night.top_ups, state.night.gaps) == (2, 0, 1)
    assert state.night.expires_at == at(0, 5, day=16)


def test_record_a_stop_by_hand() -> None:
    night = Night(NIGHT, last_success=at(22), expires_at=at(23))
    state = record_success(
        State(night=night), night, Action.TOP_UP, Step.STOPPED_BY_HAND, minutes=60, volume=6, now=at(22, 45)
    )
    assert state.night == Night(NIGHT, last_success=at(22), expires_at=at(23), stopped_by_hand=True)
    assert decide(CONFIG, state, at(22, 50)).action is Action.NOTHING


def test_record_a_failure() -> None:
    state = record_failure(State(skip_night=NIGHT), Night(NIGHT, failures=1))
    assert state == State(night=Night(NIGHT, failures=2), skip_night=NIGHT)


def walk_a_night(config: Config) -> tuple[list[tuple[str, int]], State]:
    """Walk a night with five-minute ticks and a lamp that is always on after the start. Return what was sent."""
    state = State()
    now = at(21, 55)
    sent: list[tuple[str, int]] = []
    while now <= at(7, 5, day=16):
        decision = decide(config, state, now)
        if decision.action is not Action.NOTHING:
            assert decision.window is not None
            night = state.night_of(decision.window)
            volume = volume_at(config, decision.window, now)
            step = step_on_lamp(
                decision.action, night, is_on=night.starts > 0, now=now, attempted_start=False, volume=volume
            )
            minutes = minutes_for(config, decision.window, now)
            sent.append((f"{now:%H:%M} {step}", minutes))
            state = record_success(state, night, decision.action, step, minutes=minutes, volume=volume, now=now)
        now += timedelta(minutes=5)
    return sent, state


def test_a_whole_night() -> None:
    sent, state = walk_a_night(CONFIG)
    assert sent[0] == ("22:00 start", 60)
    assert sent[1] == ("22:45 extend", 60)
    assert sent[-1] == ("06:15 extend", 45)
    assert all(minutes == 60 for _, minutes in sent[:-1])
    assert len(sent) == 12
    assert state.night is not None
    assert state.night.expires_at == at(7, day=16)


def test_a_whole_night_with_a_volume_change() -> None:
    sent, state = walk_a_night(LATER)
    assert sent[:3] == [("22:00 start", 60), ("22:45 extend", 60), ("23:30 extend", 60)]
    # 00:15 and then the change at 01:00, which restarts the top-up rhythm.
    assert sent[3:6] == [("00:15 extend", 60), ("01:00 reconfigure", 60), ("01:45 extend", 60)]
    assert sent[-1] == ("06:15 extend", 45)
    assert state.night is not None
    assert (state.night.volume, state.night.starts, state.night.top_ups) == (4, 1, len(sent) - 1)


def test_decision_carries_no_window_outside() -> None:
    assert decide(CONFIG, State(), at(12)) == Decision(Action.NOTHING, "outside the window")
