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


class CommandContended(Exception):
    """The PLC acknowledged a sequence number this gateway never issued, so
    another master has been driving it."""


def next_seq(current: int) -> int:
    return SEQ_MIN if current >= SEQ_MAX else max(current + 1, SEQ_MIN)


class CommandHandshake:
    def __init__(self, link: ModbusLink, contract: Contract) -> None:
        self.link = link
        self.contract = contract
        # The last sequence number this gateway put on the wire. The PLC should
        # be reporting exactly this one back; anything else came from somebody
        # else's command.
        self._issued: int | None = None

    async def invoke(self, code: CmdCode, arg0: int = 0) -> AckResult:
        cmd, ack = self.contract.command_block, self.contract.ack_block

        # The sequence the PLC reports having last executed. If this gateway
        # is the only master, it is the one we last issued.
        current = (await self.link.read_block(ack))["ack_seq"]

        if self._issued is not None and current != self._issued:
            # This is the whole reason the handshake counts rather than
            # toggling: a toggle bit driven by two masters comes back to where
            # it started and looks untouched. Resync so a retry can go through,
            # but refuse this one - whatever state the caller thinks the plant
            # is in, it was not this gateway that put it there. A PLC restart
            # reads the same way, and should: we did not see it get here.
            expected, self._issued = self._issued, current
            raise CommandContended(
                f"PLC acknowledged seq={current}, we last issued seq={expected}"
            )

        # Numbered from the PLC's, not from a counter of our own, so that a
        # resync after contention or a restart lands somewhere it will accept.
        seq = next_seq(current)

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
        # Recorded before the ack arrives: a command that timed out is still a
        # command we issued, and its late ack is ours, not a stranger's.
        self._issued = seq

        deadline = asyncio.get_running_loop().time() + ACK_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            fields = await self.link.read_block(ack)
            if fields["ack_seq"] == seq:
                # ack_result is only meaningful once ack_seq matches: the PLC
                # writes the result first and the sequence last.
                return AckResult(fields["ack_result"])
            await asyncio.sleep(ACK_POLL_PERIOD)

        raise CommandTimeout(f"no ack for {code.name} seq={seq} in {ACK_TIMEOUT}s")
