"""The OPC UA client: a device controller driven by the contract.

Nothing here knows a namespace index or a NodeId literal. The namespace URI
from tags.yaml is resolved to an index at connect time and every node is found
by browse path, so the gateway is free to lay its namespaces out as it likes.

Values arrive by subscription only. There is deliberately no read API: a
controller that can read is a controller that will end up polling.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from asyncua import Client, Node, ua
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256

from contract import CmdCode, Contract, Signal
from security import ClientSecurity

log = logging.getLogger("client")

# The one piece of address-space layout tags.yaml does not carry: the object
# the gateway hangs every device off. Signal names supply the rest of the path.
PLANT = "Plant"

# How often the server may send a notification batch. Nothing changes faster
# than the gateway polls, so there is no point asking for less than that.
PUBLISH_PERIOD_MS = 100

# How often the server is asked to look at each node. Named rather than left
# to asyncua's default because it is a term in the latency budget.
SAMPLING_PERIOD_MS = 50

# How long to wait between attempts to get the session back.
RECONNECT_PERIOD = 1.0

# Absolute deadband, in engineering units, on analogue signals. Applied here
# rather than by ua.DataChangeFilter, for two reasons found in asyncua 2.0.1:
# its server ANDs the deadband test with the change test, so a poll failure
# whose value sits inside the band never reaches the client at all; and its
# deadband arithmetic raises TypeError on the null value a bad status carries.
# A percent deadband is worse still - accepted, then ignored with a warning.
DEADBAND = 0.01

# Deadband only means anything for a value you can subtract.
ANALOGUE_TYPES = ("Float", "Double")


@dataclass(frozen=True)
class Update:
    """One notification: what the server currently believes about one signal.

    `status` is not decoration. When the gateway's poll fails it publishes a
    bad status and no value, and that has to reach whoever is driving this.
    """

    signal: Signal
    value: float | bool | None
    status: ua.StatusCode
    source_timestamp: datetime | None

    @property
    def ok(self) -> bool:
        return self.status.is_good()


Sink = Callable[[Update], Awaitable[None]]


def method_name(code: CmdCode) -> str:
    """Command codes are contractual; the method names follow from them."""
    return code.name.capitalize()


class _ChangeHandler:
    """What asyncua calls on every notification. Translates a node back into
    the contract signal it came from and hands the caller an Update."""

    def __init__(self, controller: DeviceController, sink: Sink) -> None:
        self.controller = controller
        self.sink = sink

    async def datachange_notification(self, node: Node, val, data) -> None:
        signal = self.controller.signal_of(node)
        if signal is None:
            return
        dv = data.monitored_item.Value
        update = Update(signal, val, dv.StatusCode, dv.SourceTimestamp)
        if self.controller.report(update):
            await self.sink(update)

    async def status_change_notification(self, status) -> None:
        # The subscription itself died, which is not a value and has no signal.
        log.warning("subscription status: %s", status.Status)


class DeviceController:
    def __init__(
        self,
        contract: Contract,
        endpoint: str,
        *,
        deadband: float = DEADBAND,
        security: ClientSecurity | None = None,
    ) -> None:
        self.contract = contract
        self.endpoint = endpoint
        self.deadband = deadband
        self.security = security
        self.client = Client(endpoint)
        self.client.connection_lost_callback = self._connection_lost

        self.idx: int | None = None
        self._nodes: dict[str, Node] = {}
        self._signals: dict[ua.NodeId, Signal] = {}
        self._methods: dict[str, Node] = {}
        self._plant: Node | None = None
        self._sub = None
        self._sink: Sink | None = None
        # The last update actually passed on, per signal - the thing a
        # deadband is measured from.
        self._reported: dict[str, Update] = {}
        self._closing = False
        self._reconnecting: asyncio.Task | None = None

    # ---------------------------------------------------------------- connect

    async def connect(self) -> None:
        if self.security is not None:
            await self._present(self.security)
        await self.client.connect()
        self.idx = await self.client.get_namespace_index(
            self.contract.meta.namespace_uri
        )
        log.info("%s -> ns=%d", self.contract.meta.namespace_uri, self.idx)
        await self._resolve()

    async def _present(self, security: ClientSecurity) -> None:
        """Certificate, key and account, in that order. Applied on every
        connect, so a reconnection is as authenticated as the first one."""
        # Announced URI and certificate URI have to agree or the gateway files
        # this connection under rejected without ever reading the account.
        self.client.application_uri = security.application_uri
        await self.client.set_security(
            SecurityPolicyBasic256Sha256,
            certificate=str(security.certificate),
            private_key=str(security.private_key),
            server_certificate=str(security.server_certificate),
            mode=ua.MessageSecurityMode.SignAndEncrypt,
        )
        self.client.set_user(security.username)
        self.client.set_password(security.password)

    async def disconnect(self) -> None:
        self._closing = True
        if self._reconnecting is not None:
            self._reconnecting.cancel()
            await asyncio.gather(self._reconnecting, return_exceptions=True)
        await self._quiet_disconnect()

    async def _quiet_disconnect(self) -> None:
        # A session whose server has gone cannot be closed politely, and the
        # attempt is not worth an exception either way.
        try:
            await self.client.disconnect()
        except Exception as exc:
            log.debug("disconnect: %r", exc)

    async def _resolve(self) -> None:
        """Find every contract node by browse path, from Objects down."""
        self._plant = await self.client.nodes.objects.get_child(self._path(PLANT))
        for signal in self.contract.signals:
            node = await self._plant.get_child(self._path(*signal.name.split(".")))
            self._nodes[signal.name] = node
            self._signals[node.nodeid] = signal
        for code in CmdCode:
            if code is CmdCode.NONE:
                continue
            name = method_name(code)
            self._methods[name] = await self._plant.get_child(self._path(name))

    def _path(self, *names: str) -> list[ua.QualifiedName]:
        # Qualify every step with the resolved index. A bare string would be
        # parsed as ns=0 and find the standard address space, or nothing.
        return [ua.QualifiedName(name, self.idx) for name in names]

    def signal_of(self, node: Node) -> Signal | None:
        return self._signals.get(node.nodeid)

    # ------------------------------------------------------------- reconnect

    async def _connection_lost(self, exc: Exception) -> None:
        if self._closing or (
            self._reconnecting is not None and not self._reconnecting.done()
        ):
            return
        log.warning("connection lost (%r); reconnecting", exc)
        # Not awaited here: asyncua fires this from inside its own supervisor,
        # which tears the session down as soon as we return.
        self._reconnecting = asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        """Rebuild the session, re-resolve every node, re-subscribe.

        asyncua's own auto_reconnect is not usable against a server that has
        restarted: its transfer_subscriptions can land on a subscription id the
        new server happens to have reissued to somebody else, and the republish
        that follows never terminates, because the server answers an unknown
        sequence number with an empty message instead of BadMessageNotAvailable.
        Resolving again from scratch is also the only way to notice that the
        namespace index moved while we were away.
        """
        while not self._closing:
            await asyncio.sleep(RECONNECT_PERIOD)
            await self._quiet_disconnect()
            try:
                await self.connect()
                if self._sink is not None:
                    await self.watch(self._sink)
            except (OSError, asyncio.TimeoutError, ua.UaError) as exc:
                log.debug("reconnect failed: %r", exc)
                continue
            log.info("reconnected; subscription restored")
            return

    # ------------------------------------------------------------- subscribe

    async def watch(self, sink: Sink) -> None:
        """Subscribe to every contract signal. This is the only way values
        arrive - there is no polling loop anywhere in this client."""
        self._sink = sink
        # A new subscription starts with no history, so its first value for
        # each signal is news however small the move since the old one.
        self._reported.clear()
        self._sub = await self.client.create_subscription(
            PUBLISH_PERIOD_MS, _ChangeHandler(self, sink)
        )
        await self._sub.subscribe_data_change(
            [self._nodes[s.name] for s in self.contract.signals],
            sampling_interval=SAMPLING_PERIOD_MS,
        )

    def report(self, update: Update) -> bool:
        """Is this update news? Analogue moves smaller than the deadband are
        not; a change of status always is.

        Measured against the last update reported, not the last one received,
        so a slow drift is eventually reported once it has gone far enough.
        Comparing against the last one received - what asyncua's server-side
        deadband does - lets a value creep any distance unnoticed.
        """
        last = self._reported.get(update.signal.name)
        noise = (
            last is not None
            and update.signal.opcua.type in ANALOGUE_TYPES
            and last.status == update.status
            and last.value is not None
            and update.value is not None
            and abs(update.value - last.value) < self.deadband
        )
        if noise:
            return False
        self._reported[update.signal.name] = update
        return True

    # ---------------------------------------------------------------- writes

    async def write_setpoint(self, name: str, value: float) -> None:
        """Write a setpoint. Anything the PLC owns is refused here, not at the
        server: a controller that can write an actuator will eventually be
        asked to."""
        signal = next(s for s in self.contract.signals if s.name == name)
        if signal.opcua.access != "RW":
            raise ValueError(f"{name} is owned by {signal.owner}; not writable")
        await self._nodes[name].write_value(
            ua.Variant(value, ua.VariantType[signal.opcua.type])
        )

    async def history(
        self, name: str, *, seconds: float = 60.0, limit: int = 0
    ) -> list[Update]:
        """What the server kept for this signal, most recent last.

        Not a way around the no-polling rule: this asks once for what has
        already happened, rather than repeatedly for what is happening.
        """
        end = datetime.now(UTC)
        values = await self._nodes[name].read_raw_history(
            starttime=end - timedelta(seconds=seconds),
            endtime=end,
            numvalues=limit,
        )
        signal = next(s for s in self.contract.signals if s.name == name)
        return [
            Update(
                signal,
                dv.Value.Value if dv.Value is not None else None,
                dv.StatusCode,
                dv.SourceTimestamp,
            )
            for dv in values
        ]

    async def command(self, code: CmdCode) -> None:
        """Issue a command as a Method call.

        Raises ua.UaStatusCodeError if the call was not Good. The code carries
        the difference the handshake exists to preserve: BadInvalidState is the
        PLC refusing, BadTimeout is nobody having answered at all.
        """
        assert self._plant is not None
        await self._plant.call_method(self._methods[method_name(code)])
