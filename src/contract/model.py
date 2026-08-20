from __future__ import annotations

from dataclasses import dataclass


class ContractError(ValueError):
    """Raised when config/tags.yaml violates a contract invariant."""


# Modbus register width, in 16-bit words, per declared type.
MODBUS_TYPE_WIDTH = {
    "bool": 1,
    "uint16": 1,
    "int16": 1,
    "uint32": 2,
    "int32": 2,
    "float32": 2,
}


@dataclass(frozen=True)
class ModbusMapping:
    table: str
    addr: int
    type: str
    scale: float = 1.0

    @property
    def width(self) -> int:
        try:
            return MODBUS_TYPE_WIDTH[self.type]
        except KeyError:
            raise ContractError(f"unknown modbus type {self.type!r}") from None

    @property
    def span(self) -> range:
        return range(self.addr, self.addr + self.width)


@dataclass(frozen=True)
class OpcUaMapping:
    id: str
    type: str
    access: str
    unit: str | None = None
    # Whether the server keeps a history of this signal. A property of the
    # northbound interface, like access and unit, so it belongs in the
    # contract rather than in whoever happens to build the address space.
    historize: bool = False


@dataclass(frozen=True)
class Signal:
    name: str
    iec: str
    modbus: ModbusMapping
    opcua: OpcUaMapping
    owner: str


@dataclass(frozen=True)
class RegisterBlock:
    """A command_block or ack_block: a fixed range of holding registers
    written/read atomically in one FC16/FC3 transaction."""

    name: str
    base: int
    layout: tuple[str, ...]
    table: str = "holding"

    @property
    def span(self) -> range:
        return range(self.base, self.base + len(self.layout))


WORD_ORDERS = ("high_first", "low_first")
BYTE_ORDERS = ("big", "little")


@dataclass(frozen=True)
class Meta:
    namespace_uri: str
    word_order: str
    byte_order: str
    scan_period_ms: int
    poll_period_ms: int


@dataclass(frozen=True)
class Contract:
    meta: Meta
    signals: tuple[Signal, ...]
    command_block: RegisterBlock
    ack_block: RegisterBlock
