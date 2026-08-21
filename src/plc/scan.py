from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from contract import AckResult, CmdCode, Contract, Meta, Signal, decode, encode
from machine import Cnc, CncState, Conveyor

from .image import InputImage, OutputImage
from .pulse import PulseLatch


# A client can write any float into the RampTime register, including 0, which
# would divide by zero in Conveyor.step. The scan clamps it instead - the PLC
# is the last line of defence on a value it did not choose.
RAMP_TIME_MIN = 0.05
RAMP_TIME_MAX = 60.0


@dataclass
class ScanStats:
    ticks: int = 0
    overruns: int = 0


class Plc:
    """Fixed-period scan loop: read_inputs -> execute -> write_outputs,
    every scan_period_ms. Logic never runs off-tick, and outputs are never
    visible outside of write_outputs()."""

    def __init__(self, contract: Contract, conveyor: Conveyor, cnc: Cnc) -> None:
        self.contract = contract
        self.conveyor = conveyor
        self.cnc = cnc
        self.memory: dict[str, dict[int, int]] = {}
        self.part_pulse = PulseLatch()
        self.watchdog_tripped = False
        self.stats = ScanStats()

        by_name = {s.name: s for s in contract.signals}
        self._setpoint_signal = by_name["Conveyor1.SpeedSetpoint"]
        self._actual_signal = by_name["Conveyor1.ActualSpeed"]
        self._ramp_signal = by_name["Conveyor1.RampTime"]
        self._position_signal = by_name["Conveyor1.Position"]
        self._cnc_state_signal = by_name["Cnc.State"]
        # The register code for each state, read from the same contract the
        # gateway uses to decode it back - one number, declared once.
        self._cnc_codes = {
            CncState(name): code
            for code, name in self._cnc_state_signal.opcua.states.items()
        }
        self._cmd_idx = {
            name: i for i, name in enumerate(contract.command_block.layout)
        }
        self._ack_idx = {name: i for i, name in enumerate(contract.ack_block.layout)}
        self._init_registers()

    def _init_registers(self) -> None:
        for s in self.contract.signals:
            table = self.memory.setdefault(s.modbus.table, {})
            for addr in s.modbus.span:
                table.setdefault(addr, 0)
        for block in (self.contract.command_block, self.contract.ack_block):
            table = self.memory.setdefault(block.table, {})
            for addr in block.span:
                table.setdefault(addr, 0)

        # Seed the writable ramp-time parameter from the machine it configures,
        # so a client reads the value actually in force rather than zero.
        ramp = self._ramp_signal.modbus
        words = encode(ramp, self.conveyor.ramp_time, self.contract.meta)
        self.memory[ramp.table].update(zip(ramp.span, words))

    def read_inputs(self) -> InputImage:
        return InputImage(
            holding=dict(self.memory["holding"]),
            conveyor_speed=self.conveyor.speed,
            conveyor_position=self.conveyor.position,
            conveyor_fault=self.conveyor.fault,
            cnc_state=self.cnc.state,
            part_pulse=self.part_pulse.read_and_clear(),
        )

    def execute(self, image: InputImage) -> OutputImage:
        meta = self.contract.meta
        outputs = OutputImage()

        raw = [image.holding[a] for a in self._setpoint_signal.modbus.span]
        outputs.conveyor_speed_setpoint = decode(
            self._setpoint_signal.modbus, raw, meta
        )

        ramp_raw = [image.holding[a] for a in self._ramp_signal.modbus.span]
        ramp = decode(self._ramp_signal.modbus, ramp_raw, meta)
        outputs.conveyor_ramp_time = min(max(ramp, RAMP_TIME_MIN), RAMP_TIME_MAX)

        self._publish(outputs, self._actual_signal, image.conveyor_speed, meta)
        self._publish(outputs, self._position_signal, image.conveyor_position, meta)
        self._publish(
            outputs, self._cnc_state_signal, self._cnc_codes[image.cnc_state], meta
        )

        cb, ab = self.contract.command_block, self.contract.ack_block
        cmd_code = image.holding[cb.base + self._cmd_idx["cmd_code"]]
        cmd_seq = image.holding[cb.base + self._cmd_idx["seq"]]
        last_ack_seq = image.holding[ab.base + self._ack_idx["ack_seq"]]

        if cmd_seq != last_ack_seq:
            if cmd_code == CmdCode.START:
                if image.conveyor_fault:
                    ack_result = AckResult.STATE
                else:
                    ack_result = AckResult.OK
                    outputs.conveyor_running = True
            elif cmd_code == CmdCode.STOP:
                ack_result = AckResult.OK
                outputs.conveyor_running = False
            elif cmd_code == CmdCode.CNC_START:
                if image.cnc_state is CncState.IDLE:
                    ack_result = AckResult.OK
                    outputs.cnc_start = True
                else:
                    ack_result = AckResult.STATE
            elif cmd_code == CmdCode.CNC_RESET:
                if image.cnc_state is CncState.FAULT:
                    ack_result = AckResult.OK
                    outputs.cnc_reset = True
                else:
                    ack_result = AckResult.STATE
            else:
                ack_result = AckResult.RANGE

            ack_table = outputs.memory_updates.setdefault(ab.table, {})
            ack_table[ab.base + self._ack_idx["ack_result"]] = int(ack_result)
            ack_table[ab.base + self._ack_idx["ack_seq"]] = cmd_seq

        return outputs

    @staticmethod
    def _publish(
        outputs: OutputImage, signal: Signal, value: float, meta: Meta
    ) -> None:
        table = outputs.memory_updates.setdefault(signal.modbus.table, {})
        table.update(zip(signal.modbus.span, encode(signal.modbus, value, meta)))

    def write_outputs(self, outputs: OutputImage) -> None:
        if outputs.conveyor_running is not None:
            self.conveyor.running = outputs.conveyor_running
        if outputs.conveyor_speed_setpoint is not None:
            self.conveyor.speed_setpoint = outputs.conveyor_speed_setpoint
        if outputs.conveyor_ramp_time is not None:
            self.conveyor.ramp_time = outputs.conveyor_ramp_time
        if outputs.cnc_start:
            self.cnc.start()
        if outputs.cnc_reset:
            self.cnc.reset()
        for table, updates in outputs.memory_updates.items():
            self.memory[table].update(updates)

    def scan(self, dt: float) -> None:
        image = self.read_inputs()
        outputs = self.execute(image)
        self.write_outputs(outputs)
        self.conveyor.step(dt)
        self.cnc.step(dt)

    async def run(self, *, ticks: int | None = None) -> None:
        dt = self.contract.meta.scan_period_ms / 1000
        next_deadline = time.monotonic() + dt
        while ticks is None or self.stats.ticks < ticks:
            if self.watchdog_tripped:
                break
            start = time.monotonic()
            self.scan(dt)
            self.stats.ticks += 1
            duration = time.monotonic() - start
            if duration > dt:
                self.stats.overruns += 1
                self.watchdog_tripped = True
                break
            sleep_for = next_deadline - time.monotonic()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            next_deadline += dt
