"""Command-line entrypoint that keeps the Sleep sounds of a Beurer WL75 playing over Bluetooth Low Energy."""

import asyncio
import enum
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated
lazy from collections.abc import Awaitable, Callable, Coroutine

import typer
from loguru import logger


# https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/#running-a-command-line-interface-from-source-with-src-layout
if not __package__:  # pragma: no cover
    # Add the grandparent directory to `sys.path` to enable running all of the following:
    # - `python ${PATH_TO_THIS_REPO}/src/wl75_sleep/main.py`  from anywhere,
    # - `uv run python src/wl75_sleep/main.py`                from the root of this repo.
    sys.path.insert(0, str(Path(__file__).parents[1]))


from wl75_sleep import client, session
from wl75_sleep import doctor as checks
from wl75_sleep import tick as scheduler
from wl75_sleep.client import LampError, LampSession
from wl75_sleep.config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config, save_value
from wl75_sleep.protocol import (
    MAX_MINUTES,
    MAX_VOLUME,
    MIN_MINUTES,
    MIN_VOLUME,
    SleepLight,
    SleepSound,
    SleepState,
)


# Hard limit for one run against the lamp, so a stuck connection can never hold Bluetooth for long.
RUN_TIMEOUT_SECONDS = 90.0


class LogLevel(enum.StrEnum):
    """Log level."""

    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    DEBUG = "debug"


@dataclass(frozen=True, slots=True)
class Settings:
    """Options shared by all commands."""

    config_path: Path
    state_path: Path


def _parse_named[E: (SleepLight, SleepSound)](enumeration: type[E], value: str) -> E:
    try:
        return enumeration[value.upper()]
    except KeyError as error:
        message = f"must be one of {', '.join(member.name.lower() for member in enumeration)}"
        raise typer.BadParameter(message) from error


def _parse_sound(value: str) -> SleepSound:
    return _parse_named(SleepSound, value)


def _parse_light(value: str) -> SleepLight:
    return _parse_named(SleepLight, value)


_SOUNDS = f"[{'|'.join(member.name.lower() for member in SleepSound)}]"
_LIGHTS = f"[{'|'.join(member.name.lower() for member in SleepLight)}]"


# Set up the `typer` app.
app = typer.Typer(no_args_is_help=True)


@app.callback()
def main(  # noqa: PLR0913
    context: typer.Context,
    *,
    log_level: Annotated[LogLevel, typer.Option(help="Log level")] = LogLevel.INFO,
    log_file_path: Annotated[Path | None, typer.Option(help="If provided, persist logs in this file.")] = None,
    stderr_level: Annotated[
        LogLevel | None, typer.Option(help="Log level on stderr, if it should differ from --log-level.")
    ] = None,
    config_path: Annotated[Path, typer.Option(help="Configuration file.", envvar="WL75_CONFIG")] = DEFAULT_CONFIG_PATH,
    state_path: Annotated[
        Path, typer.Option(help="State file of the scheduler.", envvar="WL75_STATE")
    ] = scheduler.STATE_PATH,
) -> None:
    """Keep the Sleep sounds of a Beurer WL75 playing all night."""
    # Remove the default handler from the `loguru` logger and re-instantiate it with the desired log level.
    logger.remove()
    logger.add(sys.stderr, level=(stderr_level or log_level).upper())

    # If a log file path is provided, add a file handler as well.
    if log_file_path:
        logger.add(log_file_path, level=log_level.upper(), rotation="1 MB", retention=5)

    context.obj = Settings(config_path=config_path, state_path=state_path)


@app.command()
def scan(
    context: typer.Context,
    seconds: Annotated[float, typer.Option(help="How long to listen and sample RSSI.", min=1, max=600)] = 60,
    *,
    save: Annotated[bool, typer.Option(help="Save the lamp's CoreBluetooth UUID to the configuration file.")] = True,
) -> None:
    """List devices named WL75 with RSSI and advertisement data. Connects to nothing."""
    settings: Settings = context.obj
    logger.info(f"Listening for advertisements for {seconds:g} s.")
    sightings = _run(client.scan(seconds), extra_seconds=seconds)
    if not sightings:
        logger.error("No WL75 found. A connected lamp does not advertise: is the LightUp app open on the phone?")
        raise typer.Exit(code=1)
    for sighting in sightings:
        typer.echo(sighting.describe())
    if not save:
        return
    if len(sightings) > 1:
        logger.warning(f"Found {len(sightings)} lamps. Set `device_uuid` in {settings.config_path} by hand.")
        return
    save_value("device_uuid", sightings[0].address, settings.config_path)
    logger.info(f"Saved device_uuid = {sightings[0].address} to {settings.config_path}.")


@app.command()
def inspect(
    context: typer.Context,
    listen: Annotated[float, typer.Option(help="Seconds to listen for notifications.", min=0, max=60)] = 10,
) -> None:
    """Connect without sending any command: list services, read the firmware strings, listen for notifications."""
    config = _load(context)

    async def run() -> client.Inspection:
        return await client.inspect(await client.find_lamp(config.device_uuid), listen)

    inspection = _run(run())
    typer.echo(inspection.describe())


@app.command()
def status(context: typer.Context) -> None:
    """Send the handshake and query the Sleep status."""
    typer.echo(_on_lamp(context, lambda session: session.status()).describe())


@app.command()
def start(  # noqa: PLR0913
    context: typer.Context,
    sound: Annotated[SleepSound | None, typer.Option(parser=_parse_sound, metavar=_SOUNDS, help="Sound.")] = None,
    minutes: Annotated[int, typer.Option(help="Duration.", min=MIN_MINUTES, max=MAX_MINUTES)] = MAX_MINUTES,
    volume: Annotated[int | None, typer.Option(help="Volume.", min=MIN_VOLUME, max=MAX_VOLUME)] = None,
    light: Annotated[SleepLight | None, typer.Option(parser=_parse_light, metavar=_LIGHTS, help="Light.")] = None,
    *,
    fade: Annotated[bool, typer.Option(help="Fade the sound out towards the end.")] = False,
) -> None:
    """Start the Sleep function, or reconfigure a running one. Sound, volume and light default to the configuration."""
    config = _load(context, writes=True)

    async def run(session: LampSession) -> SleepState:
        return await client.start_sleep(
            session,
            minutes=minutes,
            light=config.start_light if light is None else light,
            sound=config.sound if sound is None else sound,
            volume=config.volume if volume is None else volume,
            fade_out=fade,
        )

    typer.echo(_on_lamp(context, run).describe())


@app.command()
def extend(
    context: typer.Context,
    minutes: Annotated[int, typer.Option(help="New duration.", min=MIN_MINUTES, max=MAX_MINUTES)] = MAX_MINUTES,
) -> None:
    """Restart the countdown of a running Sleep function. Never starts one."""
    config = _load(context, writes=True)

    async def run(session: LampSession) -> SleepState:
        return await client.extend_sleep(session, minutes=minutes, light=config.renew_light)

    state = _on_lamp(context, run)
    typer.echo(state.describe())
    if not state.is_on:
        logger.error("Sleep is not running, so there is nothing to extend. Nothing was changed.")
        raise typer.Exit(code=1)


@app.command()
def stop(context: typer.Context) -> None:
    """Stop the Sleep function."""
    typer.echo(_on_lamp(context, client.stop_sleep).describe())


@app.command()
def tick(context: typer.Context) -> None:
    """Run one scheduler tick: start or top up the Sleep function if it is due. launchd calls this."""
    settings: Settings = context.obj
    config = _load(context, writes=True)
    if not scheduler.run_tick(config, settings.config_path, settings.state_path):
        raise typer.Exit(code=1)


@app.command()
def skip(
    context: typer.Context,
    *,
    undo: Annotated[bool, typer.Option(help="Cancel a skip.")] = False,
) -> None:
    """Skip tonight: the scheduler starts nothing and tops up nothing until the next night."""
    settings: Settings = context.obj
    config = _load(context)
    with scheduler.locked(settings.state_path.with_suffix(".lock")) as acquired:
        if not acquired:
            logger.error("A tick is running right now. Try again in a minute.")
            raise typer.Exit(code=1)
        state = scheduler.load_state(settings.state_path)
        night = session.night_to_skip(config, scheduler.local_now())
        window = session.window_of(config, night, None)
        label = f"the night of {night:%a %d %b}, {window.start:%H:%M} to {window.end:%H:%M}"
        if undo:
            if state.skip_night != night:
                typer.echo(f"Nothing to undo: {label} is not skipped.")
                return
            scheduler.save_state(settings.state_path, replace(state, skip_night=None))
            typer.echo(f"Skip cancelled: the scheduler runs {label} as usual.")
            return
        scheduler.save_state(settings.state_path, replace(state, skip_night=night))
    typer.echo(f"Skipping {label}.")
    if session.window_at(config, scheduler.local_now()) is not None:
        typer.echo("A run that is playing now ends by itself within the hour. `wl75 stop` ends it at once.")


@app.command()
def doctor(context: typer.Context) -> None:
    """Check the configuration, the LaunchAgent, the last tick, Mac sleep and the latest night. No Bluetooth."""
    settings: Settings = context.obj
    config, problem = None, None
    try:
        config = load_config(settings.config_path)
    except ConfigError as error:
        problem = str(error)
    facts = checks.Facts(
        config=config,
        config_problem=problem,
        state=scheduler.load_state(settings.state_path),
        now=scheduler.local_now(),
        agent_loaded=checks.agent_loaded(),
        tick_seconds=checks.installed_tick_seconds(),
        mac_sleep_minutes=checks.mac_sleep_minutes(),
    )
    findings = checks.examine(facts)
    for finding in findings:
        typer.echo(finding.describe())
    if any(finding.level is checks.Level.FAIL for finding in findings):
        raise typer.Exit(code=1)


def _on_lamp[T](context: typer.Context, operation: Callable[[LampSession], Awaitable[T]]) -> T:
    """Run `operation` in a session with the lamp, and keep the saved UUID current."""
    settings: Settings = context.obj
    config = _load(context, writes=True)

    def on_found(address: str) -> None:
        if address.casefold() != config.device_uuid.casefold():
            logger.warning(f"Found the lamp under a new UUID. Saving {address} to {settings.config_path}.")
            save_value("device_uuid", address, settings.config_path)

    return _run(client.run_on_lamp(config.device_uuid, operation, on_found))


def _load(context: typer.Context, *, writes: bool = False) -> Config:
    """Load the configuration. Commands that write to the lamp also need the firmware gate to be passed."""
    settings: Settings = context.obj
    try:
        config = load_config(settings.config_path)
    except ConfigError as error:
        logger.error(error)
        raise typer.Exit(code=2) from error
    if writes and not config.verified_firmware:
        logger.error(
            f"`verified_firmware` is not set in {settings.config_path}. Run `inspect`, compare the firmware strings "
            "with the documented ones, and store the string first. Nothing was sent."
        )
        raise typer.Exit(code=2)
    return config


def _run[T](operation: Coroutine[object, object, T], extra_seconds: float = 0) -> T:
    """Run `operation` against the lamp with a hard overall timeout. Exit with code 1 if it fails."""
    limit = RUN_TIMEOUT_SECONDS + extra_seconds

    async def bounded() -> T:
        async with asyncio.timeout(limit):
            return await operation

    try:
        return asyncio.run(bounded())
    except LampError as error:
        logger.error(error)
        raise typer.Exit(code=1) from error
    except TimeoutError as error:
        logger.error(
            f"Gave up after {limit:g} s. If macOS is showing a Bluetooth permission prompt, answer it and retry."
        )
        raise typer.Exit(code=1) from error


if __name__ == "__main__":
    # Run the `typer` app.
    app()
