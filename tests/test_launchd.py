"""The LaunchAgent template, filled in the way `make install` does it."""

import plistlib
import re
from pathlib import Path


REPOSITORY = Path(__file__).parents[1]
TEMPLATE = REPOSITORY / "launchd" / "local.wl75-sleep.plist.template"
VALUES = {
    "PYTHON": "/repo/.venv/bin/python",
    "REPOSITORY": "/repo",
    "CONFIG": "/repo/config.toml",
    "STATE": "/Users/me/Library/Application Support/wl75-sleep/state.json",
    "LOG_FILE": "/Users/me/Library/Logs/wl75-sleep/wl75-sleep.log",
    "LOG_DIR": "/Users/me/Library/Logs/wl75-sleep",
    "TICK_SECONDS": "300",
}


def _render() -> dict[str, object]:
    text = TEMPLATE.read_text(encoding="utf-8")
    for name, value in VALUES.items():
        text = text.replace(f"@{name}@", value)
    assert not re.search(r"@[A-Z_]+@", text)
    return plistlib.loads(text.encode())


def test_every_placeholder_is_set_by_the_makefile() -> None:
    placeholders = set(re.findall(r"@([A-Z_]+)@", TEMPLATE.read_text(encoding="utf-8")))
    makefile = (REPOSITORY / "Makefile").read_text(encoding="utf-8")
    assert placeholders == set(VALUES)
    assert placeholders == set(re.findall(r"s\|@([A-Z_]+)@\|", makefile))


def test_the_agent_runs_only_the_tick() -> None:
    agent = _render()
    assert agent["Label"] == "local.wl75-sleep"
    assert agent["ProgramArguments"] == [
        "/repo/.venv/bin/python",
        "/repo/src/wl75_sleep/main.py",
        "--config-path",
        "/repo/config.toml",
        "--state-path",
        "/Users/me/Library/Application Support/wl75-sleep/state.json",
        "--log-file-path",
        "/Users/me/Library/Logs/wl75-sleep/wl75-sleep.log",
        "--stderr-level",
        "warning",
        "tick",
    ]
    assert agent["StartInterval"] == 300
    assert agent["RunAtLoad"] is True
