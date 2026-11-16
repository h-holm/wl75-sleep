# Agent instructions

This tool drives a real Beurer WL75 over Bluetooth Low Energy. Read
`docs/protocol.md` before changing anything that talks to the lamp.

* Send only the allowlisted commands that `protocol.build_frame` accepts. Never
  add a raw-send command, or clock, alarm or settings commands.
* Keep values in the app's ranges: minutes 1–60, volume 1–10, brightness 10–100.
* One request in flight at a time. Every connection ends with a disconnect,
  also on error and timeout.
* Never start a Sleep run unless the user asks for one.
* Test new code against `tests/fake_lamp.py`, never the real lamp. Mark
  hardware-only code `# pragma: no cover` and keep it thin.
* Use `uv`, and run `make check` before committing.
