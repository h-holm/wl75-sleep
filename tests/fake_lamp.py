"""A fake WL75 that answers like the real lamp, for driving `LampSession` in tests.

Modelled on `tests/fake_lamp.py` of https://github.com/paukul/beurer-ha-bt (MIT, Copyright (c) 2026 Pascal
Friederich, commit 247817fe). See `src/wl75_sleep/protocol.py` for the full licence text.
"""

import asyncio

from wl75_sleep import protocol
lazy from wl75_sleep.client import NotifyCallback


def frame(payload: bytes) -> bytes:
    """Frame any payload, including answer codes, which `protocol.build_frame` refuses."""
    return bytes([*protocol.HEADER, len(payload) + 1, *payload, protocol.checksum(payload)])


class FakeLamp:
    """Stands in for `bleak.BleakClient` and answers written frames."""

    def __init__(self) -> None:
        """Start disconnected, with Sleep off."""
        self.address = "11111111-2222-3333-4444-555555555555"
        self.is_connected = False
        self.connects = 0
        self.disconnects = 0
        self.written: list[bytes] = []
        # Sleep off, as documented: 2 minutes, sunset, brightness 99, sound off, volume 6, fade on.
        self.sleep = bytearray.fromhex("00 02 01 63 00 06 01")
        self.handshake_done = False
        # Faults, keyed by command byte: leave unanswered, answer with something else, or fail the write.
        self.unanswered: set[int] = set()
        self.canned_answers: dict[int, bytes] = {}
        self.write_errors: dict[int, Exception] = {}
        self.connect_error: Exception | None = None
        self._notify: NotifyCallback | None = None

    async def connect(self) -> None:
        """Connect, or raise `connect_error`."""
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error
        self.is_connected = True

    async def disconnect(self) -> None:
        """Disconnect."""
        self.disconnects += 1
        self.is_connected = False

    async def start_notify(self, char_specifier: str, callback: NotifyCallback) -> None:
        """Remember the notification callback."""
        assert char_specifier == protocol.NOTIFY_UUID
        self._notify = callback

    def push(self, raw: bytes) -> None:
        """Deliver one notification."""
        assert self._notify is not None
        self._notify(None, bytearray(raw))

    async def write_gatt_char(self, char_specifier: str, data: bytes, response: bool | None = None) -> None:
        """Take one frame and schedule the answer."""
        assert self.is_connected
        assert char_specifier == protocol.WRITE_UUID
        assert response is False
        (payload,) = protocol.parse_frames(data)
        command = payload[0]
        if command in self.write_errors:
            raise self.write_errors[command]
        self.written.append(payload)
        if command in self.unanswered:
            return
        answer = self.canned_answers.get(command) or self._answer(payload)
        asyncio.get_running_loop().call_soon(self.push, frame(answer))

    def _answer(self, payload: bytes) -> bytes:
        command, arguments = payload[0], payload[1:]
        code = protocol.response_code(command)
        # The real lamp drops the link when the first frame is not the handshake.
        assert command == protocol.CMD_HANDSHAKE or self.handshake_done, "request before handshake"
        match command:
            case protocol.CMD_HANDSHAKE:
                self.handshake_done = True
                return bytes([code, *arguments])
            case protocol.CMD_SLEEP_STATUS:
                return bytes([code, *self.sleep])
            case protocol.CMD_SLEEP_SWITCH:
                # Starts with the default 2 minutes and forgets the duration when stopped.
                self.sleep[0] = arguments[0]
                self.sleep[1] = protocol.DEFAULT_RUN_MINUTES
            case protocol.CMD_SLEEP_CONFIG:
                if not self.sleep[0]:
                    return bytes([code, 0])  # Only accepted while running.
                if arguments[0] == protocol.SLEEP_CONFIG_LIGHT:
                    self.sleep[1:4] = arguments[1:4]
                else:
                    self.sleep[4:7] = arguments[1:4]
            case _:
                message = f"The fake lamp got a command that is not on the allowlist: {payload.hex(' ')}"
                raise AssertionError(message)
        return bytes([code, protocol.ACK_OK])
