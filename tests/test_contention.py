"""Two masters, one PLC, no arbitration.

Everything here runs against a real PLC process. The gateway's own Modbus half
drives one side and the rogue master drives the other, because the contention
is a southbound phenomenon: by the time a value reaches OPC UA it has already
been decided on the wire.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from asyncua import ua

from client import DeviceController
from contract import CmdCode, Contract, Signal, encode, load_contract
from gateway import (
    CONTENDED_STATUS,
    CommandContended,
    CommandHandshake,
    ModbusLink,
)
from plant import serving
from processes import free_port, launch, spawn
from rogue import RogueMaster

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "config" / "tags.yaml"

SETPOINT = "Conveyor1.SpeedSetpoint"
ACTUAL = "Conveyor1.ActualSpeed"

GATEWAY_SPEED = 1.5
ROGUE_SPEED = 0.2


@pytest.fixture
def contract() -> Contract:
    return load_contract(CONFIG)


@pytest.fixture
def plc():
    process = spawn("plc", free_port())
    yield process
    process.kill()


@pytest.fixture
async def link(contract, plc):
    """The gateway's southbound half, on its own."""
    link = ModbusLink(contract, "127.0.0.1", plc.port)
    await link.connect()
    yield link
    link.close()


@pytest.fixture
async def rogue(contract, plc):
    master = RogueMaster(contract, "127.0.0.1", plc.port)
    await master.connect()
    yield master
    master.close()


def signal(contract: Contract, name: str) -> Signal:
    return next(s for s in contract.signals if s.name == name)


async def read(link: ModbusLink, contract: Contract, name: str) -> float:
    reading = await link.read_signal(signal(contract, name))
    assert reading.ok, reading.error
    return reading.value


async def write(link: ModbusLink, contract: Contract, name: str, value: float) -> None:
    """A gateway write: exactly what the gateway does for a client setpoint."""
    mapping = signal(contract, name).modbus
    await link.write_registers(mapping.addr, encode(mapping, value, contract.meta))


async def wait_for(
    link: ModbusLink, contract: Contract, name: str, value: float, timeout: float = 5.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await read(link, contract, name) == pytest.approx(value):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{name} never reached {value} within {timeout}s")


async def wait_for_ack(
    rogue: RogueMaster, contract: Contract, seq: int, timeout: float = 2.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if (await rogue.read_block(contract.ack_block))["ack_seq"] == seq:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"PLC never acknowledged the rogue's seq={seq}")


async def test_the_rogue_writes_a_signal_the_contract_gives_to_the_gateway(
    link, rogue, contract
):
    """`owner: gateway` is documentation. Modbus enforces nothing, so the
    gateway reads back a value it never wrote and cannot tell that it didn't."""
    assert signal(contract, SETPOINT).owner == "gateway"

    await write(link, contract, SETPOINT, GATEWAY_SPEED)
    assert await read(link, contract, SETPOINT) == pytest.approx(GATEWAY_SPEED)

    await rogue.write_signal(SETPOINT, ROGUE_SPEED)

    reading = await link.read_signal(signal(contract, SETPOINT))
    # Not an error and not a bad status - a perfectly good read of a value the
    # gateway never asked for.
    assert reading.ok
    assert reading.value == pytest.approx(ROGUE_SPEED)


async def test_a_running_rogue_undoes_the_gateways_write(link, contract, plc):
    """The same thing through the process `just rogue` starts, rather than an
    object in this one: the gateway's setpoint does not survive."""
    await write(link, contract, SETPOINT, GATEWAY_SPEED)

    process = launch("rogue", plc.port)
    try:
        await wait_for(link, contract, SETPOINT, ROGUE_SPEED, timeout=10.0)
    finally:
        process.kill()


async def test_the_rogue_stops_a_machine_the_gateway_started(link, rogue, contract):
    """The command block is holding registers like any other. A second master
    can drive the machine, and the PLC cannot tell the two apart."""
    handshake = CommandHandshake(link, contract)
    await write(link, contract, SETPOINT, GATEWAY_SPEED)

    await handshake.invoke(CmdCode.START)
    await wait_for(link, contract, ACTUAL, GATEWAY_SPEED)

    await rogue.command(CmdCode.STOP)
    await wait_for(link, contract, ACTUAL, 0.0)


async def test_a_sequence_the_gateway_never_issued_is_refused_not_absorbed(
    link, rogue, contract
):
    """The reason the handshake counts instead of toggling: a toggle driven by
    two masters comes back to where it started and looks untouched, while a
    sequence number the gateway never issued cannot be mistaken for its own.

    The gateway refuses rather than commanding on top of a plant state it did
    not put there - and then resyncs, so the caller's retry goes through.
    """
    handshake = CommandHandshake(link, contract)
    await handshake.invoke(CmdCode.START)

    seq = await rogue.command(CmdCode.STOP)
    await wait_for_ack(rogue, contract, seq)

    with pytest.raises(CommandContended):
        await handshake.invoke(CmdCode.STOP)

    # Refusing for ever would hand the plant to whoever wrote last.
    await handshake.invoke(CmdCode.STOP)


async def test_an_uncontended_gateway_never_reports_contention(link, contract):
    """The check has to stay quiet while nobody else is writing, or it is just
    an alarm that is always on."""
    handshake = CommandHandshake(link, contract)
    for code in (CmdCode.START, CmdCode.STOP, CmdCode.START, CmdCode.STOP):
        await handshake.invoke(code)


async def test_contention_reaches_the_client_as_a_bad_status(contract, plc, rogue):
    """The end of the chain. A command issued into a contended plant comes back
    to the client as BadSequenceNumberUnknown - not as Good, and not as a
    result invented from an ack that belonged to somebody else.
    """
    async with serving(contract, plc.port) as endpoint:
        controller = DeviceController(contract, endpoint)
        await controller.connect()
        try:
            await controller.command(CmdCode.START)

            seq = await rogue.command(CmdCode.STOP)
            await wait_for_ack(rogue, contract, seq)

            with pytest.raises(ua.UaStatusCodeError) as raised:
                await controller.command(CmdCode.STOP)
            assert raised.value.code == CONTENDED_STATUS

            # And the retry that follows still works.
            await controller.command(CmdCode.STOP)
        finally:
            await controller.disconnect()
