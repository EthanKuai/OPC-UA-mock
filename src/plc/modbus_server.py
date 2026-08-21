from __future__ import annotations

from pymodbus.constants import ExcCodes
from pymodbus.logging import Log
from pymodbus.server import ModbusTcpServer
from pymodbus.server.requesthandler import ServerRequestHandler
from pymodbus.simulator import SimData, SimDevice
from pymodbus.simulator.simdata import DataType

MAX_CONNECTIONS = 4

# Modbus function code -> the image table it addresses. Input registers (FC4)
# and discrete inputs (FC2) have no write function code at all, which is what
# actually keeps PLC-owned values read-only on the wire.
_FUNC_TABLE = {
    1: "coil",
    5: "coil",
    15: "coil",
    2: "discrete",
    3: "holding",
    6: "holding",
    16: "holding",
    22: "holding",
    23: "holding",
    4: "input",
}


class PlcContext:
    """The datastore IS the PLC image table.

    A client write mutates the table and returns - no logic runs here. The
    next scan tick snapshots the table and acts on what it finds.
    """

    def __init__(self, memory: dict[str, dict[int, int]]) -> None:
        self.memory = memory

    def device_ids(self) -> list[int]:
        return [0]

    def _table(self, func_code: int) -> dict[int, int] | ExcCodes:
        name = _FUNC_TABLE.get(func_code)
        if name is None:
            return ExcCodes.ILLEGAL_FUNCTION
        table = self.memory.get(name)
        if table is None:
            # The contract maps nothing to this table, so no address in it exists.
            return ExcCodes.ILLEGAL_ADDRESS
        return table

    async def async_getValues(
        self, device_id: int, func_code: int, address: int, count: int = 1
    ) -> list[int] | ExcCodes:
        table = self._table(func_code)
        if isinstance(table, ExcCodes):
            return table
        wanted = range(address, address + count)
        if any(a not in table for a in wanted):
            # An unmapped address is an error, not a zero. Returning zeros here
            # is how a gateway ends up serving values that were never real.
            return ExcCodes.ILLEGAL_ADDRESS
        return [table[a] for a in wanted]

    async def async_setValues(
        self, device_id: int, func_code: int, address: int, values: list[int]
    ) -> ExcCodes | None:
        table = self._table(func_code)
        if isinstance(table, ExcCodes):
            return table
        if any(address + offset not in table for offset in range(len(values))):
            return ExcCodes.ILLEGAL_ADDRESS
        for offset, value in enumerate(values):
            table[address + offset] = value
        return None


class _RejectedHandler(ServerRequestHandler):
    """Accepted by TCP, then closed immediately: the PLC is at its connection
    limit. Real controllers cap concurrent masters; the cap has to be visible."""

    def callback_connected(self) -> None:
        super().callback_connected()
        Log.warning("Refusing connection, already at {} masters", MAX_CONNECTIONS)
        self.close()


class PlcModbusServer(ModbusTcpServer):
    """Modbus TCP server living in the PLC process, serving the image tables."""

    def __init__(
        self, memory: dict[str, dict[int, int]], address: tuple[str, int]
    ) -> None:
        # ModbusTcpServer insists on a SimDevice to construct. The real store
        # is the PLC image table, swapped in immediately below; nothing ever
        # reads this placeholder.
        placeholder = SimDevice(0, [SimData(0, count=1, datatype=DataType.REGISTERS)])
        super().__init__(placeholder, address=address)
        self.context = PlcContext(memory)

    def callback_new_connection(self) -> ServerRequestHandler:
        # active_connections holds the established ones; this call is the next.
        if len(self.active_connections) >= MAX_CONNECTIONS:
            return _RejectedHandler(
                self, self.trace_packet, self.trace_pdu, self.trace_connect
            )
        return super().callback_new_connection()
