"""Loads `config.toml` and updates single values in it."""

import json
import re
import shutil
import tomllib
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any
lazy from collections.abc import Callable

from wl75_sleep.protocol import MAX_MINUTES, MAX_VOLUME, MIN_MINUTES, MIN_VOLUME, SleepLight, SleepSound


REPOSITORY_ROOT = Path(__file__).parents[2]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "config.toml"
EXAMPLE_CONFIG_PATH = REPOSITORY_ROOT / "config.example.toml"


class ConfigError(Exception):
    """The configuration file is missing a value or holds an invalid one."""


@dataclass(frozen=True, slots=True)
class Config:
    """Settings of the Sleep extender. The defaults match `config.example.toml`."""

    device_uuid: str = ""
    verified_firmware: str = ""
    window_start: time = time(22, 0)
    window_end: time = time(7, 0)
    sound: SleepSound = SleepSound.RAIN
    volume: int = 5
    # Optional: from `later_volume_from` to the end of the window, play at `later_volume` instead. Both or neither.
    later_volume: int | None = None
    later_volume_from: time | None = None
    start_light: SleepLight = SleepLight.OFF
    renew_light: SleepLight = SleepLight.OFF
    renew_after_minutes: int = 45
    # Sleep runs with fade on all night. The lamp fades only in the last seconds of a countdown, so only the end of
    # the night fades out.
    fade_last_segment: bool = True
    # Longest run sent to the lamp. 60 is the app's limit. Shorter segments are for tests.
    segment_minutes: int = MAX_MINUTES


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    """Load the configuration from `path`. A missing file yields the defaults.

    Raises:
        ConfigError: If the file is not valid TOML, has an unknown key or holds an invalid value.
    """
    if not path.is_file():
        return Config()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        message = f"{path} is not valid TOML: {error}"
        raise ConfigError(message) from error

    try:
        loaded = Config(**{key: _PARSERS[key](key, value) for key, value in raw.items()})
    except KeyError as error:
        message = f"{path} has an unknown key: {error.args[0]}."
        raise ConfigError(message) from error
    if loaded.renew_after_minutes >= loaded.segment_minutes:
        message = (
            f"renew_after_minutes ({loaded.renew_after_minutes}) must be less than segment_minutes "
            f"({loaded.segment_minutes}), or the timer runs out before the top-up."
        )
        raise ConfigError(message)
    if loaded.window_start == loaded.window_end:
        message = "window_start and window_end must differ."
        raise ConfigError(message)
    _check_later_volume(loaded)
    return loaded


def _check_later_volume(config: Config) -> None:
    if (config.later_volume is None) != (config.later_volume_from is None):
        message = "later_volume and later_volume_from go together: set both or neither."
        raise ConfigError(message)
    change = config.later_volume_from
    if change is None:
        return
    start, end = config.window_start, config.window_end
    inside = start <= change < end if start < end else change >= start or change < end
    if not inside:
        message = f"later_volume_from ({change:%H:%M}) must lie within the window, {start:%H:%M} to {end:%H:%M}."
        raise ConfigError(message)


def save_value(key: str, value: str, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Set the string `key` to `value` in the file at `path`, keeping every other line and comment.

    A missing file is created from `config.example.toml` first.
    """
    if not path.is_file():
        shutil.copyfile(EXAMPLE_CONFIG_PATH, path)
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(key)}[ \t]*=[ \t]*(\"[^\"\n]*\"|'[^'\n]*')", flags=re.MULTILINE)
    assignment = f"{key} = {json.dumps(value)}"
    text, count = pattern.subn(lambda _: assignment, text, count=1)
    if not count:
        text = f"{text.rstrip()}\n{assignment}\n"
    path.write_text(text, encoding="utf-8")


def _require[T](key: str, value: object, expected: type[T]) -> T:
    if not isinstance(value, expected):
        message = f"{key} must be of type {expected.__name__}, got {value!r}."
        raise ConfigError(message)
    return value


def _parse_time(key: str, value: object) -> time:
    text = _require(key, value, str)
    if not re.fullmatch(r"\d{2}:\d{2}", text):
        message = f'{key} must look like "22:00", got {text!r}.'
        raise ConfigError(message)
    try:
        return time.fromisoformat(text)
    except ValueError as error:
        message = f"{key} is not a time of day: {text!r}."
        raise ConfigError(message) from error


def _parse_enum[E: (SleepLight, SleepSound)](key: str, value: object, enumeration: type[E]) -> E:
    text = _require(key, value, str)
    try:
        return enumeration[text.upper()]
    except KeyError as error:
        names = ", ".join(member.name.lower() for member in enumeration)
        message = f"{key} must be one of {names}, got {text!r}."
        raise ConfigError(message) from error


def _parse_int(key: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        message = f"{key} must be an integer from {low} to {high}, got {value!r}."
        raise ConfigError(message)
    return value


def _parse_text(key: str, value: object) -> str:
    return _require(key, value, str)


def _parse_flag(key: str, value: object) -> bool:
    return _require(key, value, bool)


def _parse_volume(key: str, value: object) -> int:
    return _parse_int(key, value, MIN_VOLUME, MAX_VOLUME)


def _parse_renew_after(key: str, value: object) -> int:
    # A top-up must happen before the lamp's 60-minute timer runs out.
    return _parse_int(key, value, 1, MAX_MINUTES - 1)


def _parse_segment(key: str, value: object) -> int:
    return _parse_int(key, value, MIN_MINUTES, MAX_MINUTES)


def _parse_light(key: str, value: object) -> SleepLight:
    return _parse_enum(key, value, SleepLight)


def _parse_sound(key: str, value: object) -> SleepSound:
    return _parse_enum(key, value, SleepSound)


# One parser per field of `Config`.
_PARSERS: dict[str, Callable[[str, object], Any]] = {
    "device_uuid": _parse_text,
    "verified_firmware": _parse_text,
    "window_start": _parse_time,
    "window_end": _parse_time,
    "sound": _parse_sound,
    "volume": _parse_volume,
    "later_volume": _parse_volume,
    "later_volume_from": _parse_time,
    "start_light": _parse_light,
    "renew_light": _parse_light,
    "renew_after_minutes": _parse_renew_after,
    "fade_last_segment": _parse_flag,
    "segment_minutes": _parse_segment,
}
