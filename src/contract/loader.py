from __future__ import annotations

from pathlib import Path

import yaml

from .model import (
    BYTE_ORDERS,
    WORD_ORDERS,
    Contract,
    ContractError,
    Meta,
    ModbusMapping,
    OpcUaMapping,
    RegisterBlock,
    Signal,
)


def load_contract(path: str | Path) -> Contract:
    raw = yaml.safe_load(Path(path).read_text())

    meta = Meta(**raw["meta"])

    signals = tuple(
        Signal(
            name=s["name"],
            iec=s["iec"],
            modbus=ModbusMapping(**s["modbus"]),
            opcua=OpcUaMapping(**s["opcua"]),
            owner=s["owner"],
        )
        for s in raw["signals"]
    )

    command_block = RegisterBlock(
        name="command_block",
        base=raw["command_block"]["base"],
        layout=tuple(raw["command_block"]["layout"]),
    )
    ack_block = RegisterBlock(
        name="ack_block",
        base=raw["ack_block"]["base"],
        layout=tuple(raw["ack_block"]["layout"]),
    )

    contract = Contract(
        meta=meta,
        signals=signals,
        command_block=command_block,
        ack_block=ack_block,
    )
    _validate(contract)
    return contract


def _validate(contract: Contract) -> None:
    _check_orders(contract.meta)
    _check_duplicate_names(contract.signals)
    _check_no_overlaps(contract)


def _check_orders(meta: Meta) -> None:
    # Every 32-bit codec decision reads these, so an unrecognised value must
    # fail at load time rather than silently decode to garbage.
    if meta.word_order not in WORD_ORDERS:
        raise ContractError(
            f"word_order must be one of {WORD_ORDERS}, got {meta.word_order!r}"
        )
    if meta.byte_order not in BYTE_ORDERS:
        raise ContractError(
            f"byte_order must be one of {BYTE_ORDERS}, got {meta.byte_order!r}"
        )


def _check_duplicate_names(signals: tuple[Signal, ...]) -> None:
    counts: dict[str, int] = {}
    for s in signals:
        counts[s.name] = counts.get(s.name, 0) + 1
    dupes = sorted(name for name, count in counts.items() if count > 1)
    if dupes:
        raise ContractError(f"duplicate signal name(s): {', '.join(dupes)}")


def _check_no_overlaps(contract: Contract) -> None:
    entries: list[tuple[str, range, str]] = [
        (s.modbus.table, s.modbus.span, s.name) for s in contract.signals
    ]
    entries.append(
        (contract.command_block.table, contract.command_block.span, "command_block")
    )
    entries.append((contract.ack_block.table, contract.ack_block.span, "ack_block"))

    by_table: dict[str, list[tuple[range, str]]] = {}
    for table, span, label in entries:
        by_table.setdefault(table, []).append((span, label))

    for table, spans in by_table.items():
        spans.sort(key=lambda item: item[0].start)
        for (a_span, a_label), (b_span, b_label) in zip(spans, spans[1:]):
            if a_span.stop > b_span.start:
                raise ContractError(
                    f"overlapping modbus addresses on table {table!r}: "
                    f"{a_label} [{a_span.start}-{a_span.stop - 1}] overlaps "
                    f"{b_label} [{b_span.start}-{b_span.stop - 1}]"
                )
