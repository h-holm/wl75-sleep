import dataclasses
from datetime import time
lazy from pathlib import Path

import pytest

from wl75_sleep import config
from wl75_sleep.config import EXAMPLE_CONFIG_PATH, Config, ConfigError, load_config, save_value
from wl75_sleep.protocol import SleepLight, SleepSound


def test_missing_file_yields_the_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path / "config.toml") == Config()


def test_example_file_equals_the_defaults() -> None:
    assert load_config(EXAMPLE_CONFIG_PATH) == Config()


def test_every_field_has_a_parser() -> None:
    assert set(config._PARSERS) == {field.name for field in dataclasses.fields(Config)}  # noqa: SLF001


def test_all_values_are_read(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
        device_uuid = "11111111-2222-3333-4444-555555555555"
        verified_firmware = "V1.39.0518"
        window_start = "21:30"
        window_end = "06:15"
        sound = "harmony"
        volume = 3
        later_volume = 2
        later_volume_from = "02:00"
        start_light = "sunset"
        renew_light = "off"
        renew_after_minutes = 30
        fade_last_segment = false
        segment_minutes = 50
        """,
        encoding="utf-8",
    )
    assert load_config(path) == Config(
        device_uuid="11111111-2222-3333-4444-555555555555",
        verified_firmware="V1.39.0518",
        window_start=time(21, 30),
        window_end=time(6, 15),
        sound=SleepSound.HARMONY,
        volume=3,
        later_volume=2,
        later_volume_from=time(2, 0),
        start_light=SleepLight.SUNSET,
        renew_light=SleepLight.OFF,
        renew_after_minutes=30,
        fade_last_segment=False,
        segment_minutes=50,
    )


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("volume = ", "not valid TOML"),
        ('raw_send = "yes"', "unknown key: raw_send"),
        ("device_uuid = 5", "device_uuid must be of type str"),
        ('window_start = "22"', "window_start must look like"),
        ('window_end = "25:00"', "window_end is not a time of day"),
        ('sound = "waves"', "sound must be one of off, radio, harmony, relax, instrumental, rain"),
        ('start_light = "disco"', "start_light must be one of off, sunset, mood, relax"),
        ("volume = 0", "volume must be an integer from 1 to 10"),
        ("volume = 11", "volume must be an integer from 1 to 10"),
        ("volume = true", "volume must be an integer"),
        ("renew_after_minutes = 60", "renew_after_minutes must be an integer from 1 to 59"),
        ('fade_last_segment = "yes"', "fade_last_segment must be of type bool"),
        ("segment_minutes = 0", "segment_minutes must be an integer from 1 to 60"),
        ("segment_minutes = 61", "segment_minutes must be an integer from 1 to 60"),
        ("segment_minutes = 45", r"renew_after_minutes \(45\) must be less than segment_minutes \(45\)"),
        ('window_start = "07:00"\nwindow_end = "07:00"', "must differ"),
        ("later_volume = 0", "later_volume must be an integer from 1 to 10"),
        ("later_volume = 4", "set both or neither"),
        ('later_volume_from = "23:00"', "set both or neither"),
        ('later_volume = 4\nlater_volume_from = "12:00"', r"\(12:00\) must lie within the window, 22:00 to 07:00"),
        ('later_volume = 4\nlater_volume_from = "07:00"', "must lie within the window"),
        (
            'window_start = "20:00"\nwindow_end = "23:00"\nlater_volume = 4\nlater_volume_from = "23:30"',
            "must lie within the window",
        ),
    ],
)
def test_invalid_values_are_refused(tmp_path: Path, line: str, message: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(line, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_save_value_creates_the_file_from_the_example(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    save_value("device_uuid", "11111111-2222-3333-4444-555555555555", path)

    assert load_config(path) == Config(device_uuid="11111111-2222-3333-4444-555555555555")
    # Comments and every other line survive.
    expected = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8").replace(
        'device_uuid = ""', 'device_uuid = "11111111-2222-3333-4444-555555555555"'
    )
    assert path.read_text(encoding="utf-8") == expected


def test_save_value_replaces_an_existing_value(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    save_value("device_uuid", "old", path)
    save_value("device_uuid", "new", path)
    save_value("verified_firmware", "V1.39.0518", path)

    config = load_config(path)
    assert (config.device_uuid, config.verified_firmware) == ("new", "V1.39.0518")
    assert path.read_text(encoding="utf-8").count("device_uuid") == 1


def test_save_value_appends_a_missing_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("volume = 4\n", encoding="utf-8")
    save_value("device_uuid", "abc", path)

    assert path.read_text(encoding="utf-8") == 'volume = 4\ndevice_uuid = "abc"\n'
