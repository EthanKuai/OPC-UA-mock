from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CncState(Enum):
    IDLE = "Idle"
    HOMING = "Homing"
    LOADING = "Loading"
    RUNNING = "Running"
    UNLOADING = "Unloading"
    FAULT = "Fault"


CYCLE_ORDER = (CncState.HOMING, CncState.LOADING, CncState.RUNNING, CncState.UNLOADING)


@dataclass
class Cnc:
    durations: dict[CncState, float]  # wall-clock seconds spent in each cycle state
    state: CncState = CncState.IDLE
    elapsed: float = 0.0  # time spent in the current state

    def start(self) -> None:
        if self.state is CncState.IDLE:
            self.state = CYCLE_ORDER[0]
            self.elapsed = 0.0

    def fault(self) -> None:
        self.state = CncState.FAULT
        self.elapsed = 0.0

    def reset(self) -> None:
        if self.state is CncState.FAULT:
            self.state = CncState.IDLE
            self.elapsed = 0.0

    def step(self, dt: float) -> None:
        if self.state not in CYCLE_ORDER:
            return
        self.elapsed += dt
        if self.elapsed >= self.durations[self.state]:
            self._advance()

    def _advance(self) -> None:
        idx = CYCLE_ORDER.index(self.state)
        if idx == len(CYCLE_ORDER) - 1:
            self.state = CncState.IDLE
        else:
            self.state = CYCLE_ORDER[idx + 1]
        self.elapsed = 0.0
