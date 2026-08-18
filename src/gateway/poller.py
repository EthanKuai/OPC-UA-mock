"""The gateway's southbound half: one Modbus master talking to the PLC."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from contract import Contract, RegisterBlock, Signal, decode

# Modbus TCP's device id only disambiguates devices behind a serial bridge.
# The PLC is the end device, so it answers on 0.
DEVICE_ID = 0


@dataclass(frozen=True)
class Reading:
    """One signal as of one poll attempt.

    `words` is the raw registers, kept because the northbound side needs to
    compare what a client wrote against what the PLC already holds, and only
    the registers compare exactly.
    """

    signal: Signal
    at: datetime
    value: float | bool | None = None
    words: tuple[int, ...] | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class ModbusLink:
    """One TCP connection to the PLC, serialised by one lock.

    The poll loop and an in-flight command both use it. Serialising them keeps
    the command handshake's read-after-write ordering true on the wire, which
    is the whole basis for trusting the ack.
    """

    def __init__(self, contract: Contract, host: str, port: int) -> None:
        self.contract = contract
        self.client = AsyncModbusTcpClient(host, port=port, timeout=1, retries=1)
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        await self.client.connect()

    def close(self) -> None:
        self.client.close()

    def _reader(self, table: str):
        return (
            self.client.read_input_registers
            if table == "input"
            else self.client.read_holding_registers
        )

    async def read_registers(self, table: str, addr: int, count: int) -> list[int]:
        async with self._lock:
            rr = await self._reader(table)(addr, count=count, device_id=DEVICE_ID)
        if rr.isError():
            raise ModbusException(f"{table}[{addr}:{addr + count}] -> {rr}")
        return rr.registers

    async def write_registers(self, addr: int, words: list[int]) -> None:
        async with self._lock:
            # FC16 even for a single register: one PDU, so the PLC can never
            # see half of a multi-register value.
            wr = await self.client.write_registers(addr, words, device_id=DEVICE_ID)
        if wr.isError():
            raise ModbusException(f"write {addr} <- {words} -> {wr}")

    async def read_signal(self, signal: Signal) -> Reading:
        at = datetime.now(UTC)
        m = signal.modbus
        try:
            words = await self.read_registers(m.table, m.addr, m.width)
        except (ModbusException, asyncio.TimeoutError, OSError) as exc:
            return Reading(signal, at, error=f"{type(exc).__name__}: {exc}")
        return Reading(
            signal,
            at,
            value=decode(m, words, self.contract.meta),
            words=tuple(words),
        )

    async def read_block(self, block: RegisterBlock) -> dict[str, int]:
        words = await self.read_registers(block.table, block.base, len(block.layout))
        return dict(zip(block.layout, words))

    async def write_block(self, block: RegisterBlock, fields: dict[str, int]) -> None:
        await self.write_registers(block.base, [fields[n] for n in block.layout])


class Poller:
    """Reads every contract signal every poll_period_ms and hands each result,
    good or bad, to a sink. A failed read is still a result: the sink has to
    publish it as bad rather than leave the last good value standing."""

    def __init__(self, link: ModbusLink, contract: Contract) -> None:
        self.link = link
        self.contract = contract
        self.period = contract.meta.poll_period_ms / 1000

    async def poll_once(self) -> list[Reading]:
        # One transaction per signal. The contract leaves gaps between signals
        # and the PLC rejects reads that span an address it never defined, so
        # a single sweeping read is not available here.
        return [await self.link.read_signal(s) for s in self.contract.signals]

    async def run(self, sink) -> None:
        while True:
            started = asyncio.get_running_loop().time()
            for reading in await self.poll_once():
                await sink(reading)
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.0, self.period - elapsed))
