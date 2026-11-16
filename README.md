# wl75-sleep

[![Check](https://github.com/h-holm/wl75-sleep/actions/workflows/check.yml/badge.svg)](https://github.com/h-holm/wl75-sleep/actions/workflows/check.yml)

Keeps the Sleep sounds of a Beurer WL75 wake-up light playing all night.

The WL75's Sleep function plays a sound such as rain for at most 60 minutes.
`wl75-sleep` runs on a Mac within Bluetooth range of the lamp. It starts Sleep
over Bluetooth Low Energy at a set time and tops up the timer until a set end
time, so the sound plays without a break and fades out at the end.

## How It Works

A LaunchAgent runs `wl75 tick` every 5 minutes. Each tick decides from the
configuration, a small state file and the clock, and only connects when
something is due:

* **Start**: at the start of the window, start Sleep with the configured sound,
  volume and light.
* **Top-up**: 45 minutes after the last success, restart the countdown at
  60 minutes. A failed top-up is tried again on the next tick, so each one
  gets three chances before the timer runs out.
* **End**: the last run is cut to end with the window, and fades out.
* **Stop by hand**: if Sleep is off while a run should still be going, someone
  stopped it on the lamp, and it stays off for the rest of the night. If the
  timer ran out instead, the next tick starts it again.

Every tick reconnects and disconnects, so the lamp stays free for the LightUp
app in between. Top-ups come 15 minutes before a run would end, long before the
lamp starts to fade out, so they are inaudible.

The tool can build four commands only: handshake, Sleep status, Sleep on/off
and Sleep configuration. Clock, alarm and settings commands cannot be built,
values stay within the app's ranges, and commands that change the lamp refuse
to run until its firmware has been checked. [docs/protocol.md](docs/protocol.md)
describes the protocol and the measured behaviour of the lamp.

## Requirements

* A Mac within Bluetooth range of the lamp that does not go to sleep:
  `sudo pmset -a sleep 0`.
* A WL75 with software version `V1.39.0518`, the version the protocol was
  documented on.
* [`uv`][uv-install].
* Bluetooth permission for your terminal: System Settings → Privacy & Security
  → Bluetooth.

## Setup

```shell
git clone https://github.com/h-holm/wl75-sleep.git
cd wl75-sleep
uv sync
```

Below, `wl75` stands for `uv run src/wl75_sleep/main.py`.

1. Close the LightUp app: a connected lamp does not advertise. Run
   `wl75 scan`. It finds the lamp and saves its UUID to `config.toml`, which it
   creates from [`config.example.toml`](config.example.toml).
2. Run `wl75 inspect`. It reads the firmware strings without sending a
   command. If they match [docs/protocol.md](docs/protocol.md#sources), set
   `verified_firmware` in `config.toml` to the software version.
3. Set the window, sound and volume in `config.toml`.
4. Within earshot, try `wl75 start --minutes 2`.
5. Run `make install` to load the LaunchAgent, then `wl75 doctor` to check the
   setup.

To move to another Mac, copy your `config.toml` into the new clone, run
`wl75 scan` to save the lamp's UUID as that Mac sees it, and continue with
step 5.

## Usage

| Command | Effect |
| --- | --- |
| `wl75 skip` | Skip tonight. `--undo` cancels it. |
| `wl75 stop` | Stop Sleep, and so the rest of tonight. |
| `wl75 status` | Show the lamp's Sleep status. |
| `wl75 doctor` | Check the configuration, the LaunchAgent, the last tick, the Mac's sleep setting and the latest night. No Bluetooth. |
| `make logs` | Show the last 50 log lines. |
| `make uninstall` | Unload and remove the LaunchAgent. |

The log is in `~/Library/Logs/wl75-sleep/`, the state in
`~/Library/Application Support/wl75-sleep/`.

## Configuration

`config.toml` is gitignored. The defaults come from
[`config.example.toml`](config.example.toml).

| Key | Default | Meaning |
| --- | --- | --- |
| `window_start`, `window_end` | `22:00`, `07:00` | When Sleep plays. The window may cross midnight. |
| `sound` | `rain` | `off`, `radio`, `harmony`, `relax`, `instrumental` or `rain`. |
| `volume` | `5` | 1 to 10. |
| `later_volume`, `later_volume_from` | not set | Optional: switch to another volume at a set time. Set both or neither. |
| `start_light`, `renew_light` | `off` | Light at the start and on top-ups: `off`, `sunset`, `mood` or `relax`. |
| `renew_after_minutes` | `45` | Top up this long after the last success. |
| `fade_last_segment` | `true` | Fade out at the end of the night. |

## Troubleshooting

* **"The lamp is not advertising"**: the LightUp app holds the connection, or
  the signal is too weak. Close the app. `wl75 scan` shows the signal strength.
* **Slow or failed ticks**: each log line says how long the lamp took to find,
  and `wl75 doctor` counts failed attempts. If top-ups often need more than one
  tick, move the Mac closer to the lamp.
* **The first scan hangs**: the terminal is waiting for Bluetooth permission.
  Grant it and scan again.

## Development

```shell
make check                                # Lint, format check, type check and tests.
uv run --group pre prek install           # Optional pre-commit hooks.
```

The tests run against [`tests/fake_lamp.py`](tests/fake_lamp.py), never against
a real lamp. [`tools/listening_test.py`](tools/listening_test.py) holds one
connection for a whole Sleep run and timestamps every frame, to match what is
heard to what was sent.

## Credits

The protocol and parts of `src/wl75_sleep/protocol.py` come from
[paukul/beurer-ha-bt][reference] (MIT). The project structure follows
[h-holm/uv-python-project-template][template].

## License

[MIT](LICENSE)

[reference]: https://github.com/paukul/beurer-ha-bt
[template]: https://github.com/h-holm/uv-python-project-template
[uv-install]: https://docs.astral.sh/uv/getting-started/installation
