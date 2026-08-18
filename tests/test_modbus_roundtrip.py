"""End-to-end tests against a real PLC process over real Modbus TCP.

Everything here talks to `python -m plc` running as a separate OS process, so
the encode/decode boundary is genuine: this side of the socket only ever sees
16-bit registers, exactly like the gateway will.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException

from contract import (
    AckResult,
    CmdCode,
    Contract,
    Signal,
    decode,
    encode,
    load_contract,
)
from plc import MAX_CONNECTIONS

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "config" / "tags.yaml"

# Modbus TCP carries a device id that only matters behind a serial bridge.
# The PLC is the end device, so it answers on 0.
DEVICE_ID = 0

ILLEGAL_ADDRESS = 2  # Modbus exception code 02


@dataclass
class PlcProcess:
    proc: subprocess.Popen
    port: int

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_plc() -> PlcProcess:
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "plc", str(port)],
        cwd=REPO_ROOT,
        env=os.environ | {"PYTHONPATH": str(REPO_ROOT / "src")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    plc = PlcProcess(proc, port)
    for _ in range(100):
        if proc.poll() is not None:
            raise RuntimeError(f"PLC exited early:\n{proc.stdout.read()}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return plc
        except OSError:
            time.sleep(0.05)
    plc.kill()
    raise RuntimeError(f"PLC never listened on {port}:\n{proc.stdout.read()}")


@pytest.fixture(scope="module")
def plc():
    """One PLC for the tests that only read and write registers."""
    process = _spawn_plc()
    yield process
    process.kill()


@pytest.fixture
def own_plc():
    """A PLC of this test's own, for tests that count connections or kill it."""
    process = _spawn_plc()
    yield process
    process.kill()


@pytest.fixture(scope="module")
def contract() -> Contract:
    return load_contract(CONFIG)


async def _connect(port: int) -> AsyncModbusTcpClient:
    client = AsyncModbusTcpClient("127.0.0.1", port=port, timeout=1, retries=1)
    await client.connect()
    return client


@pytest.fixture
async def client(plc):
    conn = await _connect(plc.port)
    yield conn
    conn.close()


def signal(contract: Contract, name: str) -> Signal:
    return next(s for s in contract.signals if s.name == name)


async def read_signal(client: AsyncModbusTcpClient, sig: Signal) -> list[int]:
    """The raw registers a signal occupies - no interpretation applied."""
    m = sig.modbus
    read = (
        client.read_input_registers
        if m.table == "input"
        else client.read_holding_registers
    )
    rr = await read(m.addr, count=m.width, device_id=DEVICE_ID)
    assert not rr.isError(), rr
    return rr.registers


async def test_float32_survives_the_round_trip(client, contract):
    """A 32-bit float written and read back equal."""
    ramp = signal(contract, "Conveyor1.RampTime")
    written = 1.25

    words = encode(ramp.modbus, written, contract.meta)
    wr = await client.write_registers(ramp.modbus.addr, words, device_id=DEVICE_ID)
    assert not wr.isError(), wr

    read_back = decode(ramp.modbus, await read_signal(client, ramp), contract.meta)
    assert read_back == pytest.approx(written)


async def test_float32_wire_layout_matches_the_declared_word_order(client, contract):
    """The round trip above passes even if both ends are wrong together, as
    long as they are wrong in the same way. This pins the bytes on the wire.

    1.25 is 0x3FA00000. high_first puts the high word at the lower address.
    """
    ramp = signal(contract, "Conveyor1.RampTime")
    assert contract.meta.word_order == "high_first"

    words = encode(ramp.modbus, 1.25, contract.meta)
    await client.write_registers(ramp.modbus.addr, words, device_id=DEVICE_ID)

    assert await read_signal(client, ramp) == [0x3FA0, 0x0000]


async def test_wrong_word_order_reads_garbage_and_no_error(client, contract):
    """The word-order lesson: a master that guesses the wrong order does not
    get an error. It gets a plausible-looking number that is nonsense.
    """
    ramp = signal(contract, "Conveyor1.RampTime")

    await client.write_registers(
        ramp.modbus.addr, encode(ramp.modbus, 1.25, contract.meta), device_id=DEVICE_ID
    )
    words = await read_signal(client, ramp)

    right = decode(ramp.modbus, words, contract.meta)
    wrong = decode(ramp.modbus, words, replace(contract.meta, word_order="low_first"))

    assert right == pytest.approx(1.25)
    assert wrong != pytest.approx(1.25)
    # Not a small rounding difference - a completely different magnitude.
    assert abs(wrong) < 1e-30


async def test_plc_owned_float32_decodes_as_a_live_value(client, contract):
    """Position is encoded by the PLC and decoded here, so the two ends are
    independent implementations of the same contract."""
    setpoint = signal(contract, "Conveyor1.SpeedSetpoint")
    position = signal(contract, "Conveyor1.Position")

    await client.write_registers(
        setpoint.modbus.addr,
        encode(setpoint.modbus, 1.0, contract.meta),
        device_id=DEVICE_ID,
    )
    await command(client, contract, CmdCode.START)

    first = decode(position.modbus, await read_signal(client, position), contract.meta)
    await asyncio.sleep(0.5)
    second = decode(position.modbus, await read_signal(client, position), contract.meta)

    assert second > first
    # A conveyor doing at most 2 m/s cannot have gone metres in half a second;
    # a word-order slip here would show up as ~1e-40 or ~1e38, not as 0.5.
    assert 0 < second - first < 2.0

    await command(client, contract, CmdCode.STOP)


async def command(
    client: AsyncModbusTcpClient, contract: Contract, code: CmdCode, seq: int = 1
) -> int:
    """Write the command block with FC16 and wait for the PLC to acknowledge."""
    cb, ab = contract.command_block, contract.ack_block
    fields = dict.fromkeys(cb.layout, 0) | {"cmd_code": int(code), "seq": seq}
    wr = await client.write_registers(
        cb.base, [fields[name] for name in cb.layout], device_id=DEVICE_ID
    )
    assert not wr.isError(), wr

    ack_seq = ab.layout.index("ack_seq")
    ack_result = ab.layout.index("ack_result")
    for _ in range(100):
        rr = await client.read_holding_registers(
            ab.base, count=len(ab.layout), device_id=DEVICE_ID
        )
        assert not rr.isError(), rr
        if rr.registers[ack_seq] == seq:
            return rr.registers[ack_result]
        await asyncio.sleep(0.02)
    raise AssertionError(f"PLC never acknowledged seq={seq}")


async def test_command_block_is_acknowledged(client, contract):
    assert await command(client, contract, CmdCode.START, seq=41) == AckResult.OK
    assert await command(client, contract, CmdCode.STOP, seq=42) == AckResult.OK
    # An unknown code is rejected rather than ignored.
    assert await command(client, contract, CmdCode(0), seq=43) == AckResult.RANGE


async def test_unmapped_address_is_an_exception_not_zeros(client):
    """An address the contract never defined must be an error. Answering with
    zeros is how a gateway ends up publishing values that were never real."""
    rr = await client.read_holding_registers(9999, count=1, device_id=DEVICE_ID)
    assert rr.isError()
    assert rr.exception_code == ILLEGAL_ADDRESS


async def test_plc_owned_signal_cannot_be_written(client, contract):
    """ActualSpeed lives in the input table, which has no write function code
    at all - the write lands on a holding address that does not exist."""
    actual = signal(contract, "Conveyor1.ActualSpeed")
    rr = await client.write_registers(actual.modbus.addr, [1], device_id=DEVICE_ID)
    assert rr.isError()
    assert rr.exception_code == ILLEGAL_ADDRESS


async def test_connection_beyond_the_limit_is_rejected(own_plc, contract):
    """A real controller caps concurrent masters. The cap has to be visible:
    the accepted masters keep working, the extra one does not."""
    accepted = [await _connect(own_plc.port) for _ in range(MAX_CONNECTIONS)]
    try:
        for conn in accepted:
            rr = await conn.read_holding_registers(
                contract.ack_block.base, count=1, device_id=DEVICE_ID
            )
            assert not rr.isError(), rr

        extra = await _connect(own_plc.port)
        try:
            with pytest.raises(ConnectionException):
                await extra.read_holding_registers(
                    contract.ack_block.base, count=1, device_id=DEVICE_ID
                )
        finally:
            extra.close()

        # The cap must not have disturbed the masters already connected.
        for conn in accepted:
            rr = await conn.read_holding_registers(
                contract.ack_block.base, count=1, device_id=DEVICE_ID
            )
            assert not rr.isError(), rr
    finally:
        for conn in accepted:
            conn.close()


async def test_dead_plc_raises_rather_than_serving_stale_values(own_plc, contract):
    """Killing the PLC must produce a connection error, not the last values
    read. This is the failure the gateway has to turn into a bad StatusCode."""
    conn = await _connect(own_plc.port)
    try:
        position = signal(contract, "Conveyor1.Position")
        assert await read_signal(conn, position) is not None

        own_plc.kill()
        await asyncio.sleep(0.2)

        with pytest.raises(ConnectionException):
            await conn.read_input_registers(
                position.modbus.addr,
                count=position.modbus.width,
                device_id=DEVICE_ID,
            )
    finally:
        conn.close()
