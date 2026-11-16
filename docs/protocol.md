# WL75 protocol: the Sleep subset

This is the part of the Beurer WL75's Bluetooth Low Energy protocol that
`wl75-sleep` uses. It covers the Sleep function only. The tool cannot build
any other command.

## Sources

Every fact below carries one or more of these tags.

| Tag | Source |
| --- | --- |
| **[doc]** | [`docs/wl75-protocol.md`][ref-doc] of `paukul/beurer-ha-bt` at commit `247817fe`. The author decompiled the LightUp app 3.8, then verified on a real lamp (their sections 10 to 14) and against an HCI capture of the app (their section 11). |
| **[code]** | [`protocol.py`][ref-code] of the same project, from which `src/wl75_sleep/protocol.py` is ported. |
| **[measured]** | Measured for this project on a WL75 with the documented firmware: SW V1.39.0518, FW V2.0.0518, HW V4. See [Measured behaviour](#measured-behaviour). |

## Connection

| Fact | Value | Source |
| --- | --- | --- |
| Advertised name | `WL75` (the app matches the prefix) | [doc] §1, [measured] |
| Pairing or bonding | None | [doc] §1, [measured] |
| Service | `14839ac4-7d7e-415c-9a42-167340cf2339` | [doc] §1, [measured] |
| Write characteristic | `8b00ace7-eb0b-49b0-bbe9-9aee0a26e1a3`, write without response | [doc] §1, §2.4, [measured] |
| Notify characteristic | `0734594a-a8e7-4b1a-a6b1-cd5243059a57` | [doc] §1, [measured] |
| Software revision | Device Information `0x180A`, characteristic `0x2A28` | [doc] §1, §3, [measured] |
| MTU | Never negotiated. Frames must fit 20 bytes. Ours are at most 9. | [doc] §1 |

A connected lamp does not advertise. If a scan finds nothing, the phone app
probably holds the connection.

## Frame

```text
FE EF  LEN  payload…  CHK
LEN = len(payload) + 1
CHK = LEN xor every payload byte
```

Source: [doc] §2.2. Answers use the same frame ([doc] §2.3). The first payload
byte is the command in a request and the answer code in an answer.

The answer code is `0xF0 | cmd` for commands below `0x10`, otherwise
`0xE0 | (cmd & 0x0F)` ([doc] §4).

## Rules of the link

| Rule | Source |
| --- | --- |
| Every connection starts with the handshake `00 75`. Without it the lamp disconnects. | [doc] §3, §10 |
| Every command is answered, also those the app does not wait for. | [doc] §11 |
| Answers arrive about 130 to 240 ms after the write, one frame per notification. | [doc] §10, [measured] |
| Commands sent 10 to 30 ms apart garble the answers: several frames per notification, some cut off. Send one request, wait for its answer, then send the next. | [doc] §11 |
| A parser must resynchronise on `FE EF` and drop frames with a bad checksum. | [doc] §11 |

## The allowlist

These four commands are the only ones `protocol.build_frame` accepts.

| Payload | Meaning | Answer | Source |
| --- | --- | --- | --- |
| `00 75` | Handshake | `F0 75` | [doc] §3, §10, [measured] |
| `1D` | Sleep status | `ED on minutes light brightness sound volume fade` | [doc] §5, §10, [measured] |
| `1E 01` / `1E 00` | Sleep on / off | `EE 01` | [doc] §11, [measured] for `1E 01` |
| `1F 01 minutes light brightness` | Duration and light. Restarts the countdown. | `EF 01` | [doc] §11, §14, [measured] |
| `1F 02 sound volume fade` | Sound | `EF 01` | [doc] §11, §14, [measured] |

Value ranges, taken from the app's controls ([doc] §6). Builders raise on
anything outside them.

| Value | Range |
| --- | --- |
| minutes | 1 to 60 |
| brightness | 10 to 100 |
| volume | 1 to 10 |
| light | 0 off, 1 sunset, 2 mood, 3 relax |
| sound | 0 off, 1 radio, 2 harmony, 3 relax, 4 instrumental, 5 rain drops |
| fade | 0 or 1 |

## Behaviour of the Sleep function

| Fact | Source |
| --- | --- |
| `1F` is rejected with `EF 00` unless Sleep is running. Send it after `1E 01`. | [doc] §14 |
| `1E 01` alone starts a 2-minute run. | [doc] §14 |
| `1F 01 N …` restarts the countdown at N minutes, also mid-run and while a sound plays. | [doc] §14, [measured] |
| While running, `ED` minutes is the remaining time. It is rounded, and never reads less than 1. | [doc] §14, [measured] |
| While running, `ED` brightness is the current brightness, which falls towards the end. | [doc] §14 |
| While running, `ED` volume is the configured volume, also during a fade. | [measured] |
| After the end or `1E 00`, `ED` minutes reads 2 again. Light, brightness, sound and volume stay stored. | [doc] §14, [measured] |
| After the end, `ED` fade reads the lamp's stored setting, not the one sent for the run. | [measured] |
| `B0 04` arrives unprompted when the Sleep timer ends, if a connection is open. | [doc] §4, §14, [measured] |

## Golden frames

Computed with the checksum rule, which reproduces the documented frames.
`tests/test_protocol.py` checks each one byte for byte.

```text
handshake                   FE EF 03 00 75 76
sleep status                FE EF 02 1D 1F
sleep on                    FE EF 03 1E 01 1C
sleep off                   FE EF 03 1E 00 1D
60 min, light off, bri 10   FE EF 06 1F 01 3C 00 0A 2E
rain, volume 5, fade off    FE EF 06 1F 02 05 05 00 1B
rain, volume 5, fade on     FE EF 06 1F 02 05 05 01 1A
documented status answer    FE EF 09 ED 00 02 01 63 00 06 01 83
```

## Measured behaviour

The reference project tested with the sound off. These results add what the
sound does, measured with `wl75` and `tools/listening_test.py`, which holds
one connection for a whole run and timestamps every frame.

### Sound

| Test | Result |
| --- | --- |
| 10-minute run, rain, top-up to 10 minutes with 7 minutes left | Inaudible. The volume stayed steady. |
| 3-minute run, top-up with 90 seconds left, status every 5 seconds near the end | An audible volume jump at the top-up. Not repeated, cause unknown. |
| 3-minute run, fade off, nothing sent | Steady volume to the end, then the sound stops at once. |
| 10-minute run, fade on, status every 20 seconds | Full volume until about 30 seconds before the end, then a smooth fade to silence. The 29 status queries were inaudible. |

So the scheduler keeps fade on all night and tops up at minute 45 of 60:
every top-up restarts the countdown long before the fade would begin, and
only the last run of the night fades out.

### Link

Measured from a Mac at about −97 dBm, where the lamp sent about two
advertisements a minute.

- Finding the lamp takes nearly all of a connection. Once it is seen,
  connecting, the handshake and a status take about a second.
- 20 status runs in a row all succeeded: median 5.1 s, slowest 25.8 s. At
  other hours about one attempt in three failed, and the lamp sometimes did
  not advertise for 90 seconds, also while Sleep was running.
- An open connection is stable: one held for 13 minutes without a drop.
- The lamp advertises the 16-bit service UUID `0x6c53`. The reference unit
  advertised `0x7336`, so `wl75-sleep` matches the name or a saved UUID
  instead, as the app does.

### macOS

A LaunchAgent that runs the virtual environment's Python reaches the lamp
without a permission prompt and without an app bundle. Tested with
Homebrew's Python 3.15, whose `Python.app` has no
`NSBluetoothAlwaysUsageDescription`.

### Open questions

- Whether the fade lasts 30 seconds for every run length, or scales with it.
- Whether a `1F 02` sent mid-run, as for the optional volume change, is
  audible.
- Whether the lamp advertises less often while Sleep runs.
- Whether `1E 00` is answered with `EE 01`. It has not been sent.

[ref-code]: https://github.com/paukul/beurer-ha-bt/blob/247817fe/custom_components/beurer_wl75/protocol.py
[ref-doc]: https://github.com/paukul/beurer-ha-bt/blob/247817fe/docs/wl75-protocol.md
