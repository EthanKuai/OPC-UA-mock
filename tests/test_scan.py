import asyncio
import contextlib
import dataclasses
import time
from pathlib import Path

import pytest

from contract import AckResult, CmdCode, Contract, load_contract
from machine import Cnc, CncState, Conveyor
from plc import Plc, PulseLatch

REPO_ROOT = Path(__file__).resolve().parent.parent

# Values these tests never wait out; only the constructor needs them.
CNC_DURATIONS = {
    CncState.HOMING: 1.0,
    CncState.LOADING: 1.0,
    CncState.RUNNING: 1.0,
    CncState.UNLOADING: 1.0,
}


@pytest.fixture
def contract() -> Contract:
    return load_contract(REPO_ROOT / "config" / "tags.yaml")


@pytest.fixture
def timing_contract(contract: Contract) -> Contract:
    # A larger scan period than the real 10ms contract so scheduling jitter
    # from Python/asyncio overhead can't spuriously trip the watchdog or
    # skew the tick count in these timing-sensitive tests.
    meta = dataclasses.replace(contract.meta, scan_period_ms=50)
    return dataclasses.replace(contract, meta=meta)


def make_plc(contract: Contract) -> Plc:
    conveyor = Conveyor(ramp_time=0.2, max_speed=2.0)
    cnc = Cnc(durations=CNC_DURATIONS)
    return Plc(contract, conveyor, cnc)


def cmd_addr(contract: Contract, field: str) -> int:
    return contract.command_block.base + contract.command_block.layout.index(field)


def ack_addr(contract: Contract, field: str) -> int:
    return contract.ack_block.base + contract.ack_block.layout.index(field)


def setpoint_addr(contract: Contract) -> int:
    return next(s for s in contract.signals if s.name == "Conveyor1.SpeedSetpoint").modbus.addr


def actual_addr(contract: Contract) -> int:
    return next(s for s in contract.signals if s.name == "Conveyor1.ActualSpeed").modbus.addr


# --- scan timing --------------------------------------------------------


async def test_scan_is_periodic(timing_contract: Contract):
    plc = make_plc(timing_contract)
    period = timing_contract.meta.scan_period_ms / 1000
    duration = period * 10

    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(plc.run(), timeout=duration)

    expected_ticks = duration / period
    assert abs(plc.stats.ticks - expected_ticks) <= 1


async def test_watchdog_trips_on_slow_logic(timing_contract: Contract):
    plc = make_plc(timing_contract)
    period = timing_contract.meta.scan_period_ms / 1000
    real_execute = plc.execute

    def slow_execute(image):
        time.sleep(period * 3)
        return real_execute(image)

    plc.execute = slow_execute

    await plc.run()

    assert plc.watchdog_tripped is True
    assert plc.stats.overruns == 1
    assert plc.stats.ticks == 1


# --- snapshot / output-boundary invariants -------------------------------


def test_input_mutated_mid_scan_not_seen_until_next_scan(contract: Contract):
    plc = make_plc(contract)
    addr = setpoint_addr(contract)
    plc.memory["holding"][addr] = 1000  # 1.0 m/s

    image = plc.read_inputs()
    plc.memory["holding"][addr] = 2000  # "arrives" after the snapshot was taken
    outputs = plc.execute(image)

    assert outputs.conveyor_speed_setpoint == pytest.approx(1.0)


def test_outputs_change_only_at_scan_boundaries(contract: Contract):
    plc = make_plc(contract)
    addr = setpoint_addr(contract)
    plc.memory["holding"][addr] = 500  # 0.5 m/s

    image = plc.read_inputs()
    before_setpoint = plc.conveyor.speed_setpoint
    before_memory = {table: dict(regs) for table, regs in plc.memory.items()}
    outputs = plc.execute(image)

    assert plc.conveyor.speed_setpoint == before_setpoint  # not applied yet
    assert plc.memory == before_memory  # nothing committed yet either

    plc.write_outputs(outputs)

    assert plc.conveyor.speed_setpoint == pytest.approx(0.5)


def test_pulse_shorter_than_dt_is_missed_without_latch_and_caught_with_one():
    live_present = False
    latch = PulseLatch()

    def read_live() -> bool:
        return live_present

    assert read_live() is False
    assert latch.read_and_clear() is False

    # A pulse that starts and ends entirely between two scans.
    live_present = True
    latch.set()
    live_present = False

    # The naive live read missed it; the latch caught it.
    assert read_live() is False
    assert latch.read_and_clear() is True

    # And it doesn't fire again on the following scan.
    assert latch.read_and_clear() is False


# --- command handshake ----------------------------------------------------


def test_no_new_command_when_seq_unchanged(contract: Contract):
    plc = make_plc(contract)
    plc.scan(0.01)

    assert plc.memory["holding"][ack_addr(contract, "ack_seq")] == 0
    assert plc.memory["holding"][ack_addr(contract, "ack_result")] == 0
    assert plc.conveyor.running is False


def test_start_command_starts_conveyor(contract: Contract):
    plc = make_plc(contract)
    plc.memory["holding"][cmd_addr(contract, "cmd_code")] = CmdCode.START
    plc.memory["holding"][cmd_addr(contract, "seq")] = 1

    plc.scan(0.01)

    assert plc.memory["holding"][ack_addr(contract, "ack_seq")] == 1
    assert plc.memory["holding"][ack_addr(contract, "ack_result")] == AckResult.OK
    assert plc.conveyor.running is True


def test_stop_command_stops_conveyor(contract: Contract):
    plc = make_plc(contract)
    plc.conveyor.running = True
    plc.memory["holding"][cmd_addr(contract, "cmd_code")] = CmdCode.STOP
    plc.memory["holding"][cmd_addr(contract, "seq")] = 1

    plc.scan(0.01)

    assert plc.memory["holding"][ack_addr(contract, "ack_result")] == AckResult.OK
    assert plc.conveyor.running is False


def test_unknown_command_code_is_rejected_as_range(contract: Contract):
    plc = make_plc(contract)
    plc.memory["holding"][cmd_addr(contract, "cmd_code")] = 99
    plc.memory["holding"][cmd_addr(contract, "seq")] = 1

    plc.scan(0.01)

    assert plc.memory["holding"][ack_addr(contract, "ack_result")] == AckResult.RANGE
    assert plc.conveyor.running is False


def test_start_while_faulted_is_rejected_as_state(contract: Contract):
    plc = make_plc(contract)
    plc.conveyor.fault = True
    plc.memory["holding"][cmd_addr(contract, "cmd_code")] = CmdCode.START
    plc.memory["holding"][cmd_addr(contract, "seq")] = 1

    plc.scan(0.01)

    assert plc.memory["holding"][ack_addr(contract, "ack_result")] == AckResult.STATE
    assert plc.conveyor.running is False


def test_actual_speed_is_published_to_memory_each_scan(contract: Contract):
    plc = make_plc(contract)
    plc.conveyor.speed = 1.5

    plc.scan(0.01)

    raw = plc.memory["input"][actual_addr(contract)]
    assert raw == pytest.approx(1500)  # scale 0.001
