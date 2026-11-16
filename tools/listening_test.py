"""Listening tests: one held connection from the start of a Sleep run to its end.

The link is too slow for timed tests on fresh connections, so this tool finds the lamp once, starts a run, optionally
tops it up after a set time, optionally polls the status, and waits for the lamp's unprompted `B0 04` at the end of the
run. Every line carries the wall-clock time and the seconds since the start, so what is heard can be matched to what
was sent.

It uses the same allowlisted sequences as `wl75 start` and `wl75 extend`, refuses to run without `verified_firmware`,
and always disconnects. If the link drops after the start, it says so and stops: the run on the lamp ends by itself.

    uv run tools/listening_test.py --minutes 10 --top-up-after 180
"""

import asyncio
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated
lazy from collections.abc import Callable

import typer
from bleak import BleakClient
from loguru import logger
lazy from bleak.backends.device import BLEDevice


if not __package__:  # pragma: no cover
    # Run as a script: make `wl75_sleep` importable from `src`.
    sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from wl75_sleep import client, protocol
from wl75_sleep.client import LampError, LampRefusedError, LampSession, Transport, open_session
from wl75_sleep.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from wl75_sleep.protocol import (
    MAX_MINUTES,
    MAX_VOLUME,
    MIN_MINUTES,
    MIN_VOLUME,
    SleepLight,
    SleepSound,
    SleepState,
)


SECONDS_PER_MINUTE = 60.0
# How long after the expected end to wait for `B0 04` before giving up. The lamp ends a run within a second.
END_MARGIN_SECONDS = 60.0
# How often the wait checks for `B0 04` and for a dropped link. Sets the precision of the `B0 04` timestamp.
CHECK_INTERVAL_SECONDS = 0.05
# Attempts to find and connect to the lamp before the start. After the start there is no second attempt.
START_ATTEMPTS = 3
START_RETRY_DELAY_SECONDS = 5.0


class LinkLostError(LampError):
    """The connection failed after the run had started."""


@dataclass(frozen=True, slots=True)
class ListeningPlan:
    """What one listening test sends, and when."""

    minutes: int
    light: SleepLight
    sound: SleepSound
    volume: int
    fade_out: bool
    top_up_after_seconds: float | None = None
    top_up_minutes: int | None = None
    top_up_light: SleepLight = SleepLight.OFF
    resend_sound: bool = False
    poll_every_seconds: float | None = None

    def problems(self) -> list[str]:
        """Return why the plan cannot run as given. An empty list means it can."""
        problems = []
        if (
            self.top_up_after_seconds is not None
            and not 0 < self.top_up_after_seconds < self.minutes * SECONDS_PER_MINUTE
        ):
            problems.append(f"The top-up must come during the {self.minutes}-minute run.")
        if self.top_up_after_seconds is None and (self.resend_sound or self.top_up_minutes is not None):
            problems.append("Re-sending the sound and the top-up duration need a top-up time.")
        if self.poll_every_seconds is not None and self.poll_every_seconds <= 0:
            problems.append("The polling interval must be positive.")
        return problems


@dataclass(slots=True)
class Timeline:
    """Prints one timestamped line per event and remembers when the run started, was topped up and ended."""

    clock: Callable[[], float] = time.monotonic
    started_at: float | None = None
    topped_up_at: float | None = None
    finished_at: float | None = None

    def line(self, event: str, detail: str = "") -> None:
        """Print one event with the wall-clock time and, once the run has started, the seconds since the start."""
        wall = datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-5]
        elapsed = "" if self.started_at is None else f"+{self.clock() - self.started_at:.1f} s"
        typer.echo(f"{wall}  {elapsed:>9}  {event:<16}  {detail}".rstrip())

    def summary(self) -> str:
        """Return the measured durations of the run."""
        if self.started_at is None:
            return "The run did not start."
        parts = []
        if self.topped_up_at is not None:
            parts.append(f"topped up {self.topped_up_at - self.started_at:.1f} s after the start")
        if self.finished_at is None:
            parts.append("no B0 04 seen")
        else:
            parts.append(f"B0 04 {self.finished_at - self.started_at:.1f} s after the start")
            if self.topped_up_at is not None:
                parts.append(f"{self.finished_at - self.topped_up_at:.1f} s after the top-up")
        return f"Summary: {', '.join(parts)}."


async def run_listening_test(
    session: LampSession, plan: ListeningPlan, timeline: Timeline, disconnected: asyncio.Event
) -> None:
    """Start the run, top up and poll as planned, and return once `B0 04` arrives or the wait gives up.

    Raises:
        LinkLostError: If the lamp drops the link while waiting.
        LampError: If a request fails.
    """
    state = await client.start_sleep(
        session,
        minutes=plan.minutes,
        light=plan.light,
        sound=plan.sound,
        volume=plan.volume,
        fade_out=plan.fade_out,
    )
    start = timeline.started_at = timeline.clock()
    timeline.line(f"start, {plan.minutes} min", state.describe())
    # `math.inf` stands for "never".
    top_up_at = start + (math.inf if plan.top_up_after_seconds is None else plan.top_up_after_seconds)
    poll_every = math.inf if plan.poll_every_seconds is None else plan.poll_every_seconds
    next_poll = start + poll_every
    end_expected = start + plan.minutes * SECONDS_PER_MINUTE

    while True:
        wake = min(top_up_at, next_poll, end_expected + END_MARGIN_SECONDS)
        if await _wait_for_end(session, disconnected, wake, timeline.clock):
            timeline.finished_at = timeline.clock()
            timeline.line("b0 04", "unprompted: the Sleep timer ended")
            await _final_status(session, timeline)
            return
        now = timeline.clock()
        if now >= top_up_at:
            top_up_at = math.inf
            topped_up_at = await _top_up(session, plan, timeline)
            if topped_up_at is None:
                return
            end_expected = topped_up_at + (plan.top_up_minutes or plan.minutes) * SECONDS_PER_MINUTE
        elif now >= next_poll:
            timeline.line("status", (await session.status()).describe())
            while next_poll <= timeline.clock():
                next_poll += poll_every
        elif now >= end_expected + END_MARGIN_SECONDS:
            timeline.line("no b0 04", f"nothing within {END_MARGIN_SECONDS:g} s of the expected end")
            await _final_status(session, timeline)
            return


async def _wait_for_end(
    session: LampSession, disconnected: asyncio.Event, until: float, clock: Callable[[], float]
) -> bool:
    """Wait until `until`. Return `True` as soon as `B0 04` has arrived.

    Raises:
        LinkLostError: If the lamp drops the link while waiting.
    """
    while True:
        if protocol.SLEEP_TIMER_FINISHED in session.unprompted:
            return True
        if disconnected.is_set():
            message = "The lamp dropped the connection."
            raise LinkLostError(message)
        remaining = until - clock()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(CHECK_INTERVAL_SECONDS, remaining))


async def _top_up(session: LampSession, plan: ListeningPlan, timeline: Timeline) -> float | None:
    """Restart the countdown, and re-send the sound settings if planned.

    Return when the countdown restarted, or `None` if Sleep had already ended and nothing was sent.
    """
    minutes = plan.top_up_minutes or plan.minutes
    state = await client.extend_sleep(session, minutes=minutes, light=plan.top_up_light)
    if not state.is_on:
        timeline.line("top-up skipped", f"Sleep is not running: {state.describe()}")
        return None
    topped_up_at = timeline.topped_up_at = timeline.clock()
    timeline.line(f"top-up, {minutes} min", state.describe())
    if plan.resend_sound:
        await session.command(protocol.set_sleep_sound(plan.sound, plan.volume, fade_out=plan.fade_out))
        timeline.line("re-sent sound", (await session.status()).describe())
    return topped_up_at


async def _final_status(session: LampSession, timeline: Timeline) -> SleepState | None:
    try:
        state = await session.status()
    except LampError as error:
        timeline.line("status failed", str(error))
        return None
    timeline.line("status", state.describe())
    return state


def make_transport(device: BLEDevice, on_disconnect: Callable[[], None]) -> Transport:  # pragma: no cover
    """Return a `BleakClient` for `device` that calls `on_disconnect` when the link drops."""
    return BleakClient(
        device, timeout=client.CONNECT_TIMEOUT_SECONDS, disconnected_callback=lambda _client: on_disconnect()
    )


async def run_on_lamp(device_uuid: str, plan: ListeningPlan, timeline: Timeline) -> None:
    """Find the lamp and run the test on one connection. Retries only until the run has started.

    Raises:
        LinkLostError: If the link drops after the start.
        LampError: If the lamp cannot be reached before the start, or refuses a command.
    """
    for attempt in range(1, START_ATTEMPTS + 1):
        try:
            timeline.line("searching", f"attempt {attempt} of {START_ATTEMPTS}")
            device = await client.find_lamp(device_uuid)
            disconnected = asyncio.Event()
            transport = make_transport(device, disconnected.set)
            async with open_session(transport) as session:
                timeline.line("connected", "handshake answered")
                await run_listening_test(session, plan, timeline, disconnected)
        except LinkLostError, LampRefusedError:
            raise
        except LampError as error:
            if timeline.started_at is not None:
                message = f"Lost the link after the start: {error}"
                raise LinkLostError(message) from error
            if attempt == START_ATTEMPTS:
                raise
            timeline.line("attempt failed", f"before the start, nothing is running: {error}")
            await asyncio.sleep(START_RETRY_DELAY_SECONDS)
        else:
            return


def _parse_sound(value: str) -> SleepSound:
    try:
        return SleepSound[value.upper()]
    except KeyError as error:
        message = f"must be one of {', '.join(member.name.lower() for member in SleepSound)}"
        raise typer.BadParameter(message) from error


def _parse_light(value: str) -> SleepLight:
    try:
        return SleepLight[value.upper()]
    except KeyError as error:
        message = f"must be one of {', '.join(member.name.lower() for member in SleepLight)}"
        raise typer.BadParameter(message) from error


_SOUNDS = f"[{'|'.join(member.name.lower() for member in SleepSound)}]"
_LIGHTS = f"[{'|'.join(member.name.lower() for member in SleepLight)}]"

app = typer.Typer()


@app.command()
def main(  # noqa: PLR0913
    *,
    minutes: Annotated[int, typer.Option(help="Duration of the run.", min=MIN_MINUTES, max=MAX_MINUTES)] = 3,
    sound: Annotated[SleepSound | None, typer.Option(parser=_parse_sound, metavar=_SOUNDS, help="Sound.")] = None,
    volume: Annotated[int | None, typer.Option(help="Volume.", min=MIN_VOLUME, max=MAX_VOLUME)] = None,
    light: Annotated[SleepLight | None, typer.Option(parser=_parse_light, metavar=_LIGHTS, help="Light.")] = None,
    top_up_after: Annotated[float | None, typer.Option(help="Seconds after the start to top up.")] = None,
    top_up_minutes: Annotated[
        int | None, typer.Option(help="Duration sent at the top-up.", min=MIN_MINUTES, max=MAX_MINUTES)
    ] = None,
    top_up_light: Annotated[
        SleepLight | None, typer.Option(parser=_parse_light, metavar=_LIGHTS, help="Light sent at the top-up.")
    ] = None,
    poll_every: Annotated[float | None, typer.Option(help="Seconds between status queries.")] = None,
    config_path: Annotated[Path, typer.Option(help="Configuration file.", envvar="WL75_CONFIG")] = DEFAULT_CONFIG_PATH,
    fade: Annotated[bool, typer.Option(help="Fade the sound out towards the end.")] = False,
    resend_sound: Annotated[bool, typer.Option(help="Re-send the sound settings right after the top-up.")] = False,
    debug: Annotated[bool, typer.Option(help="Log every frame sent and received.")] = False,
) -> None:
    """Hold one connection for a whole Sleep run. Sound, volume and lights default to the configuration."""
    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if debug else "INFO")
    try:
        config = load_config(config_path)
    except ConfigError as error:
        logger.error(error)
        raise typer.Exit(code=2) from error
    if not config.verified_firmware:
        logger.error(f"`verified_firmware` is not set in {config_path}. Nothing was sent.")
        raise typer.Exit(code=2)
    plan = ListeningPlan(
        minutes=minutes,
        light=config.start_light if light is None else light,
        sound=config.sound if sound is None else sound,
        volume=config.volume if volume is None else volume,
        fade_out=fade,
        top_up_after_seconds=top_up_after,
        top_up_minutes=top_up_minutes,
        top_up_light=config.renew_light if top_up_light is None else top_up_light,
        resend_sound=resend_sound,
        poll_every_seconds=poll_every,
    )
    if problems := plan.problems():
        for problem in problems:
            logger.error(problem)
        raise typer.Exit(code=2)

    timeline = Timeline()
    try:
        asyncio.run(run_on_lamp(config.device_uuid, plan, timeline))
    except LinkLostError as error:
        timeline.line("link lost", f"{error} The run on the lamp ends by itself.")
        raise typer.Exit(code=1) from error
    except LampError as error:
        timeline.line("failed", str(error))
        raise typer.Exit(code=1) from error
    except KeyboardInterrupt:
        timeline.line("interrupted", "disconnected. A run that has started ends by itself.")
        raise typer.Exit(code=130) from None
    finally:
        typer.echo(timeline.summary())


if __name__ == "__main__":
    app()
