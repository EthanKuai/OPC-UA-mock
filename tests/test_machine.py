import math

import pytest

from machine import Cnc, CncState, Conveyor, PartSensor
from machine.cnc import CYCLE_ORDER


def test_position_advances_linearly_at_constant_speed():
    conveyor = Conveyor(ramp_time=0.1, max_speed=1.0, speed_setpoint=1.0, running=True)
    dt = 0.01

    # Run past the ramp so speed is settled at the setpoint.
    for _ in range(20):
        conveyor.step(dt)
    assert conveyor.speed == pytest.approx(1.0)

    conveyor.position = 0.0
    for _ in range(50):
        conveyor.step(dt)

    assert conveyor.position == pytest.approx(1.0 * dt * 50)


def test_speed_ramps_over_ramp_time_never_steps():
    ramp_time = 1.0
    max_speed = 2.0
    conveyor = Conveyor(
        ramp_time=ramp_time, max_speed=max_speed, speed_setpoint=max_speed, running=True
    )
    dt = 0.01
    max_delta_per_tick = (max_speed / ramp_time) * dt

    ticks = int(ramp_time / dt)
    for _ in range(ticks):
        previous = conveyor.speed
        conveyor.step(dt)
        assert conveyor.speed - previous <= max_delta_per_tick + 1e-9

    assert conveyor.speed == pytest.approx(max_speed)


def test_speed_ramps_to_zero_when_stopped():
    conveyor = Conveyor(ramp_time=0.5, max_speed=1.0, speed_setpoint=1.0, running=True)
    for _ in range(100):
        conveyor.step(0.01)
    assert conveyor.speed == pytest.approx(1.0)

    conveyor.running = False
    for _ in range(100):
        conveyor.step(0.01)
    assert conveyor.speed == pytest.approx(0.0)


def test_part_sensor_detects_presence_within_tolerance():
    sensor = PartSensor(position=1.0, tolerance=0.05)
    conveyor = Conveyor(
        ramp_time=0.01, max_speed=1.0, speed_setpoint=0.0, position=1.02, sensors=[sensor]
    )
    assert conveyor.parts_present() == [True]

    conveyor.position = 5.0
    assert conveyor.parts_present() == [False]


DURATIONS = {
    CncState.HOMING: 0.2,
    CncState.LOADING: 0.1,
    CncState.RUNNING: 0.5,
    CncState.UNLOADING: 0.1,
}


def test_cnc_cycle_takes_configured_wall_clock_time():
    cnc = Cnc(durations=DURATIONS)
    dt = 0.01
    cnc.start()

    elapsed = 0.0
    visited = [cnc.state]
    while cnc.state is not CncState.IDLE:
        cnc.step(dt)
        elapsed += dt
        if cnc.state != visited[-1]:
            visited.append(cnc.state)

    assert visited == [*CYCLE_ORDER, CncState.IDLE]
    # Each of the 4 state transitions can overshoot its configured duration by
    # up to one tick, the same "N ticks +-1" quantization as the PLC scan.
    assert elapsed == pytest.approx(sum(DURATIONS.values()), abs=len(CYCLE_ORDER) * dt)


def test_cnc_fault_holds_until_reset():
    cnc = Cnc(durations=DURATIONS)
    cnc.start()
    dt = 0.01
    while cnc.state is not CncState.RUNNING:
        cnc.step(dt)
    assert cnc.state is CncState.RUNNING

    cnc.fault()
    assert cnc.state is CncState.FAULT

    cnc.step(10.0)
    assert cnc.state is CncState.FAULT  # a fault never self-clears

    cnc.reset()
    assert cnc.state is CncState.IDLE
    cnc.start()
    assert cnc.state is CncState.HOMING
