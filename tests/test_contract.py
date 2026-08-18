from pathlib import Path

import pytest

from contract import ContractError, load_contract

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_loads_real_contract():
    contract = load_contract(REPO_ROOT / "config" / "tags.yaml")

    assert contract.meta.namespace_uri == "http://mock.local/plant/"
    assert contract.meta.word_order == "high_first"
    assert contract.meta.scan_period_ms == 10

    names = {s.name for s in contract.signals}
    assert names == {
        "Conveyor1.SpeedSetpoint",
        "Conveyor1.RampTime",
        "Conveyor1.ActualSpeed",
        "Conveyor1.Position",
    }

    position = next(s for s in contract.signals if s.name == "Conveyor1.Position")
    assert position.modbus.type == "float32"
    assert position.modbus.width == 2  # a 32-bit value spans two registers

    setpoint = next(s for s in contract.signals if s.name == "Conveyor1.SpeedSetpoint")
    assert setpoint.modbus.table == "holding"
    assert setpoint.modbus.addr == 10
    assert setpoint.modbus.width == 1
    assert setpoint.opcua.access == "RW"
    assert setpoint.owner == "gateway"

    assert contract.command_block.base == 100
    assert contract.command_block.layout == ("cmd_code", "arg0_lo", "arg0_hi", "seq")
    assert contract.ack_block.base == 110


def test_duplicate_signal_name_raises():
    with pytest.raises(ContractError, match="duplicate signal name"):
        load_contract(FIXTURES / "duplicate_name.yaml")


def test_32bit_signal_occupies_two_registers_and_overlap_is_caught():
    with pytest.raises(ContractError, match="overlapping modbus addresses"):
        load_contract(FIXTURES / "overlapping_32bit.yaml")


def test_command_and_ack_block_collision_raises():
    with pytest.raises(ContractError, match="overlapping modbus addresses"):
        load_contract(FIXTURES / "block_collision.yaml")
