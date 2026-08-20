"""Historical Access: what the server kept, and what it did not.

The contract decides which signals are historised, so this suite is really
asking whether that declaration reaches the address space.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from client import DeviceController
from contract import CmdCode, Contract, load_contract
from plant import serving
from processes import free_port, spawn

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "config" / "tags.yaml"

SETPOINT = "Conveyor1.SpeedSetpoint"
POSITION = "Conveyor1.Position"
SPEED = 1.0
# Several poll periods, so the position has moved more than once.
RUNNING = 1.0


@pytest.fixture
def contract() -> Contract:
    return load_contract(CONFIG)


@pytest.fixture
def plc():
    process = spawn("plc", free_port())
    yield process
    process.kill()


@pytest.fixture
async def controller(contract, plc):
    async with serving(contract, plc.port) as endpoint:
        controller = DeviceController(contract, endpoint)
        await controller.connect()
        try:
            yield controller
        finally:
            await controller.disconnect()


async def test_a_historised_signal_can_be_read_back_as_a_series(controller, contract):
    """Position is declared historised, so a client that was not even
    connected while the conveyor ran can still see where it went."""
    assert next(s for s in contract.signals if s.name == POSITION).opcua.historize

    await controller.write_setpoint(SETPOINT, SPEED)
    await controller.command(CmdCode.START)
    await asyncio.sleep(RUNNING)
    await controller.command(CmdCode.STOP)

    series = await controller.history(POSITION)

    assert len(series) > 1
    assert all(update.ok for update in series)
    # A conveyor only goes forwards, and time only goes one way either.
    values = [update.value for update in series]
    stamps = [update.source_timestamp for update in series]
    assert values == sorted(values)
    assert stamps == sorted(stamps)
    assert values[-1] > values[0]


async def test_history_stores_changes_rather_than_polls(controller):
    """The gateway rewrites every node every 100 ms whether the value moved or
    not. A standing still conveyor must not fill the archive with copies."""
    await asyncio.sleep(RUNNING)

    series = await controller.history(POSITION)

    # One sample for the value it has held throughout, give or take the
    # initial zero the server wrote before the first poll landed.
    assert len(series) <= 2


async def test_a_signal_the_contract_does_not_historise_has_no_history(
    controller, contract
):
    """Historising everything is how an archive becomes useless.

    asyncua answers a history read on an unhistorised node with an empty
    result rather than BadHistoryOperationUnsupported, so what actually tells
    a client whether history exists is the HistoryRead bit in AccessLevel,
    which asyncua does set.
    """
    assert not next(s for s in contract.signals if s.name == SETPOINT).opcua.historize

    await controller.write_setpoint(SETPOINT, SPEED)
    await asyncio.sleep(0.3)

    assert await controller.history(SETPOINT) == []
