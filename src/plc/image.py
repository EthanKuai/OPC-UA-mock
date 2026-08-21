from __future__ import annotations

from dataclasses import dataclass, field

from machine import CncState


@dataclass
class InputImage:
    """Frozen snapshot taken at the start of a scan. Logic reads only this,
    never live memory or the machine, so a write that lands mid-scan is not
    observed until the next scan's snapshot."""

    holding: dict[int, int]
    conveyor_speed: float
    conveyor_position: float
    conveyor_fault: bool
    cnc_state: CncState
    part_pulse: bool


@dataclass
class OutputImage:
    """Computed by logic(); nothing here takes effect until write_outputs()
    commits it at the end of the scan."""

    memory_updates: dict[str, dict[int, int]] = field(default_factory=dict)
    conveyor_running: bool | None = None
    conveyor_speed_setpoint: float | None = None
    conveyor_ramp_time: float | None = None
    # One-shot triggers, not levels: Cnc.start()/reset() are themselves
    # idempotent outside the state they apply in, so "act this scan" is all
    # write_outputs() needs to know.
    cnc_start: bool = False
    cnc_reset: bool = False
