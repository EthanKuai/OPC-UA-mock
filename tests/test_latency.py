"""End-to-end latency, and the four periods it is made of.

The stimulus is a register written straight into the PLC over Modbus, so the
clock starts at a moment this test knows exactly. What is measured from there
is the northbound path and nothing else: the PLC's next scan, the gateway's
next poll, the server's sampling of the node, and the next publish.

Security is left off. The budget is made of periods; encrypting the channel
would change the number without changing any of the terms being measured.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

import pytest

from client import PUBLISH_PERIOD_MS, SAMPLING_PERIOD_MS, DeviceController, Update
from contract import CmdCode, Contract, encode, load_contract
from gateway import ModbusLink
from plant import serving
from plc.scan import RAMP_TIME_MIN
from processes import free_port, spawn

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "config" / "tags.yaml"

SETPOINT = "Conveyor1.SpeedSetpoint"
ACTUAL = "Conveyor1.ActualSpeed"
RAMP = "Conveyor1.RampTime"

SAMPLES = 25
LOW = 1.0
HIGH = 1.5
# Long enough for everything the previous step provoked to have arrived.
SETTLE = 0.3


@pytest.fixture
def contract() -> Contract:
    return load_contract(CONFIG)


@pytest.fixture
def plc():
    process = spawn("plc", free_port())
    yield process
    process.kill()


class FirstArrival:
    """When the first update for one signal turns up, and nothing else."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.at: float | None = None
        self.arrived = asyncio.Event()

    async def __call__(self, update: Update) -> None:
        if update.signal.name == self.name and self.at is None:
            self.at = asyncio.get_running_loop().time()
            self.arrived.set()

    def reset(self) -> None:
        self.at = None
        self.arrived.clear()


async def write(link: ModbusLink, contract: Contract, name: str, value: float) -> None:
    mapping = next(s for s in contract.signals if s.name == name).modbus
    await link.write_registers(mapping.addr, encode(mapping, value, contract.meta))


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


async def test_latency_stays_inside_the_configured_budget(contract, plc):
    budget = {
        "PLC scan period": contract.meta.scan_period_ms,
        "gateway poll period": contract.meta.poll_period_ms,
        "sampling interval": SAMPLING_PERIOD_MS,
        "publishing interval": PUBLISH_PERIOD_MS,
    }
    total = sum(budget.values())

    link = ModbusLink(contract, "127.0.0.1", plc.port)
    await link.connect()
    try:
        # The shortest ramp the PLC will accept, so a step is over within two
        # scans and the next sample starts from a machine that has settled.
        await write(link, contract, RAMP, RAMP_TIME_MIN)

        async with serving(contract, plc.port) as endpoint:
            controller = DeviceController(contract, endpoint)
            await controller.connect()
            arrival = FirstArrival(ACTUAL)
            await controller.watch(arrival)
            try:
                await controller.command(CmdCode.START)
                latencies = await _measure(link, contract, arrival, total)
            finally:
                await controller.disconnect()
    finally:
        link.close()

    p50 = percentile(latencies, 0.50)
    p99 = percentile(latencies, 0.99)

    print(f"\n{'latency budget':<22}{'ms':>8}")
    for term, value in budget.items():
        print(f"  {term:<20}{value:>8}")
    print(f"  {'budget':<20}{total:>8}")
    print(f"  {'measured p50':<20}{p50:>8.1f}")
    print(f"  {'measured p99':<20}{p99:>8.1f}   ({SAMPLES} samples)")

    assert p99 <= total


async def _measure(
    link: ModbusLink, contract: Contract, arrival: FirstArrival, budget_ms: int
) -> list[float]:
    """One sample per setpoint step: write, then wait for the machine's answer
    to come back round the long way."""
    latencies = []
    for i in range(SAMPLES):
        arrival.reset()
        started = asyncio.get_running_loop().time()
        await write(link, contract, SETPOINT, HIGH if i % 2 == 0 else LOW)
        await asyncio.wait_for(arrival.arrived.wait(), timeout=budget_ms / 1000 * 5)
        assert arrival.at is not None
        latencies.append((arrival.at - started) * 1000)
        await asyncio.sleep(SETTLE)
    return latencies
