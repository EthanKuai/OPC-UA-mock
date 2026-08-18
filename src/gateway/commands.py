"""The sequence-number command handshake, gateway side.

A bare register write is not a command here, because a second master exists
and can overwrite anything at any time. Every command carries a sequence
number; only an ack bearing that same number proves the PLC saw this command
and not somebody else's.
"""

from __future__ import annotations

import asyncio

from contract import AckResult, CmdCode, Contract

from .poller import ModbusLink

ACK_TIMEOUT = 1.0
ACK_POLL_PERIOD = 0.02

# seq 0 is never used, so a PLC that has booted and never been commanded
# (ack_seq == 0) can never look like it acknowledged something.
SEQ_MIN = 1
SEQ_MAX = 0xFFFF


class CommandTimeout(Exception):
    """The PLC did not acknowledge within ACK_TIMEOUT."""


def next_seq(current: int) -> int:
    return SEQ_MIN if current >= SEQ_MAX else max(current + 1, SEQ_MIN)


class CommandHandshake:
    def __init__(self, link: ModbusLink, contract: Contract) -> None:
        self.link = link
        self.contract = contract

    async def invoke(self, code: CmdCode, arg0: int = 0) -> AckResult:
        cmd, ack = self.contract.command_block, self.contract.ack_block

        # Base the next sequence on what the PLC currently reports rather than
        # on a counter of our own: after a PLC restart, or after the rogue
        # master has written the block, ours is the number that is stale.
        seq = next_seq((await self.link.read_block(ack))["ack_seq"])

        await self.link.write_block(
            cmd,
            {
                "cmd_code": int(code),
                # arg0 is in the contract for commands that carry a value.
                # START/STOP do not; the conveyor's setpoint is its own signal,
                # and giving it a second writer is exactly the bug to avoid.
                "arg0_lo": arg0 & 0xFFFF,
                "arg0_hi": (arg0 >> 16) & 0xFFFF,
                "seq": seq,
            },
        )

        deadline = asyncio.get_running_loop().time() + ACK_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            fields = await self.link.read_block(ack)
            if fields["ack_seq"] == seq:
                # ack_result is only meaningful once ack_seq matches: the PLC
                # writes the result first and the sequence last.
                return AckResult(fields["ack_result"])
            await asyncio.sleep(ACK_POLL_PERIOD)

        raise CommandTimeout(f"no ack for {code.name} seq={seq} in {ACK_TIMEOUT}s")
