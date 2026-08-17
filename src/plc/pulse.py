from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PulseLatch:
    """A sticky bit. set() can be called at any time, e.g. from a fast
    physical event; read_and_clear() is the scan-synchronous read a PLC
    input-capture channel provides, catching pulses shorter than one scan
    that a live read would miss."""

    _latched: bool = False

    def set(self) -> None:
        self._latched = True

    def read_and_clear(self) -> bool:
        value = self._latched
        self._latched = False
        return value
