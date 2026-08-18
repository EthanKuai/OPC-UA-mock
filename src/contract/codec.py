from __future__ import annotations

import struct
from collections.abc import Sequence

from .model import MODBUS_TYPE_WIDTH, ContractError, Meta, ModbusMapping

# Modbus itself defines each register as big-endian on the wire, so a
# single-register value needs no reordering. word_order/byte_order from
# tags.yaml meta describe how this device lays out values spanning MORE than
# one register - the part the Modbus spec leaves undefined.
_STRUCT_FORMAT = {
    "int16": ">h",
    "uint16": ">H",
    "int32": ">i",
    "uint32": ">I",
    "float32": ">f",
}


def value_to_words(value: float | bool, type_name: str, meta: Meta) -> list[int]:
    if type_name == "bool":
        return [1 if value else 0]
    try:
        fmt = _STRUCT_FORMAT[type_name]
    except KeyError:
        raise ContractError(f"unknown modbus type {type_name!r}") from None

    packed = struct.pack(fmt, float(value) if type_name == "float32" else int(value))
    chunks = [packed[i : i + 2] for i in range(0, len(packed), 2)]
    if len(chunks) > 1:
        if meta.byte_order == "little":
            chunks = [c[::-1] for c in chunks]
        if meta.word_order == "low_first":
            chunks.reverse()
    return [int.from_bytes(c, "big") for c in chunks]


def words_to_value(words: Sequence[int], type_name: str, meta: Meta) -> float | bool:
    if type_name == "bool":
        return bool(words[0])
    try:
        fmt = _STRUCT_FORMAT[type_name]
    except KeyError:
        raise ContractError(f"unknown modbus type {type_name!r}") from None

    width = MODBUS_TYPE_WIDTH[type_name]
    if len(words) != width:
        raise ContractError(f"{type_name} needs {width} register(s), got {len(words)}")

    chunks = [w.to_bytes(2, "big") for w in words]
    if width > 1:
        # Undo value_to_words in reverse order.
        if meta.word_order == "low_first":
            chunks.reverse()
        if meta.byte_order == "little":
            chunks = [c[::-1] for c in chunks]
    return struct.unpack(fmt, b"".join(chunks))[0]


def decode(mapping: ModbusMapping, words: Sequence[int], meta: Meta) -> float | bool:
    value = words_to_value(words, mapping.type, meta)
    if mapping.type == "bool":
        return value
    return value * mapping.scale


def encode(mapping: ModbusMapping, value: float | bool, meta: Meta) -> list[int]:
    if mapping.type == "bool":
        return value_to_words(value, mapping.type, meta)
    scaled = value / mapping.scale
    if mapping.type != "float32":
        scaled = round(scaled)
    return value_to_words(scaled, mapping.type, meta)
