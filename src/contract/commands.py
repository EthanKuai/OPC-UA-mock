from __future__ import annotations

from enum import IntEnum


class CmdCode(IntEnum):
    NONE = 0
    START = 1
    STOP = 2
    CNC_START = 3
    CNC_RESET = 4


class AckResult(IntEnum):
    OK = 0
    RANGE = 1
    STATE = 2
    BUSY = 3
