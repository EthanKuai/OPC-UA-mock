from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class PartSensor:
    position: float
    tolerance: float = 0.01


@dataclass
class Conveyor:
    ramp_time: float  # seconds to accelerate from 0 to max_speed
    max_speed: float
    speed_setpoint: float = 0.0
    running: bool = False
    fault: bool = False
    position: float = 0.0
    speed: float = 0.0
    sensors: list[PartSensor] = field(default_factory=list)

    def step(self, dt: float) -> None:
        target = self.speed_setpoint if (self.running and not self.fault) else 0.0
        max_delta = (self.max_speed / self.ramp_time) * dt
        delta = target - self.speed
        if abs(delta) <= max_delta:
            self.speed = target
        else:
            self.speed += math.copysign(max_delta, delta)
        self.position += self.speed * dt

    def parts_present(self) -> list[bool]:
        return [abs(self.position - s.position) < s.tolerance for s in self.sensors]
