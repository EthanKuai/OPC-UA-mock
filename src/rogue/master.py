"""A second Modbus master on the same PLC, parallel to the gateway.

Nothing in here is an exploit. It is an ordinary Modbus TCP client making
ordinary Modbus writes, and that is the point: `owner` in tags.yaml is a
comment. Modbus has no notion of who may write what, no session, and no
arbitration between masters. The gateway's careful ownership rules stop at
the edge of the gateway.
"""

from __future__ import annotations

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from contract import CmdCode, Contract, RegisterBlock, encode

# The same end device the gateway talks to, on the same unit id. There is no
# second slot for a second master; both are simply masters.
DEVICE_ID = 0


class RogueMaster:
    def __init__(self, contract: Contract, host: str, port: int) -> None:
        self.contract = contract
        self.client = AsyncModbusTcpClient(host, port=port, timeout=1, retries=1)

    async def connect(self) -> None:
        await self.client.connect()

    def close(self) -> None:
        self.client.close()

    async def write_signal(self, name: str, value: float) -> None:
        """Write any signal, whoever the contract says owns it."""
        signal = next(s for s in self.contract.signals if s.name == name)
        words = encode(signal.modbus, value, self.contract.meta)
        wr = await self.client.write_registers(
            signal.modbus.addr, words, device_id=DEVICE_ID
        )
        if wr.isError():
            raise ModbusException(f"write {name} <- {value} -> {wr}")

    async def command(self, code: CmdCode) -> int:
        """Issue a command in the gateway's own command block. Returns the
        sequence number used, which the PLC will echo into the ack block."""
        cmd, ack = self.contract.command_block, self.contract.ack_block
        # Any number the PLC has not just acknowledged will be executed. A
        # rogue is under no obligation to follow the gateway's numbering; it
        # only has to differ, which is exactly what makes the gap visible.
        seq = ((await self.read_block(ack))["ack_seq"] + 1) & 0xFFFF
        await self.write_block(
            cmd, {"cmd_code": int(code), "arg0_lo": 0, "arg0_hi": 0, "seq": seq}
        )
        return seq

    async def read_block(self, block: RegisterBlock) -> dict[str, int]:
        # Blocks live in holding registers by definition: they are written, and
        # Modbus has no write function code for the input table.
        rr = await self.client.read_holding_registers(
            block.base, count=len(block.layout), device_id=DEVICE_ID
        )
        if rr.isError():
            raise ModbusException(f"read {block.name} -> {rr}")
        return dict(zip(block.layout, rr.registers))

    async def write_block(self, block: RegisterBlock, fields: dict[str, int]) -> None:
        wr = await self.client.write_registers(
            block.base, [fields[name] for name in block.layout], device_id=DEVICE_ID
        )
        if wr.isError():
            raise ModbusException(f"write {block.name} <- {fields} -> {wr}")
