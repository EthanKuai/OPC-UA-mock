"""The gateway's northbound half: an OPC UA server generated from the contract.

Nothing here is hand-built per signal. The address space, the data types, the
units and the access levels all come out of config/tags.yaml, so a signal added
to the contract appears on OPC UA without touching this file.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from asyncua import Server, ua
from asyncua.common.methods import uamethod
from pymodbus.exceptions import ModbusException

from contract import AckResult, CmdCode, Contract, Signal, encode

from .commands import CommandHandshake, CommandTimeout
from .poller import ModbusLink, Poller, Reading

log = logging.getLogger("gateway")

# A rejected command is a successful call that returns a bad result; a command
# that never got through is a failed call. The client has to tell them apart.
ACK_STATUS = {
    AckResult.OK: ua.StatusCodes.Good,
    AckResult.RANGE: ua.StatusCodes.BadOutOfRange,
    AckResult.STATE: ua.StatusCodes.BadInvalidState,
    AckResult.BUSY: ua.StatusCodes.BadResourceUnavailable,
}

# The poll failed, so every value behind it is unknown. Not stale-but-Good.
STALE_STATUS = ua.StatusCodes.BadCommunicationError


class _WriteWatcher:
    """Server-side subscription: a client write to a setpoint node has to
    reach the PLC, and this is where it is noticed."""

    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway

    async def datachange_notification(self, node, val, data) -> None:
        await self.gateway.on_node_changed(node, val)


class Gateway:
    def __init__(
        self,
        contract: Contract,
        plc_host: str,
        plc_port: int,
        endpoint: str,
    ) -> None:
        self.contract = contract
        self.link = ModbusLink(contract, plc_host, plc_port)
        self.poller = Poller(self.link, contract)
        self.commands = CommandHandshake(self.link, contract)
        self.server = Server()
        self.endpoint = endpoint

        self.idx: int | None = None
        self._node_ids: dict[str, ua.NodeId] = {}
        self._by_node_id: dict[ua.NodeId, Signal] = {}
        # What the PLC last reported, per signal, as raw registers. Registers
        # compare exactly where scaled floats do not.
        self._last_words: dict[str, tuple[int, ...] | None] = {}

    # ---------------------------------------------------------------- startup

    async def init(self) -> None:
        await self.server.init()
        # Bind every interface. asyncua rewrites the advertised endpoint to the
        # address the client actually reached us on, so a client that resolves
        # this host gets an endpoint it can resolve back.
        self.server.set_endpoint(self.endpoint)
        self.server.set_server_name("Mock Plant Gateway")

        # The namespace index is whatever the server assigns; only the URI is
        # contractual. Clients must resolve the URI, never assume ns=2.
        self.idx = await self.server.register_namespace(
            self.contract.meta.namespace_uri
        )
        await self._build_address_space()

    async def _build_address_space(self) -> None:
        plant = await self.server.nodes.objects.add_object(
            ua.NodeId("Plant", self.idx), self._qname("Plant")
        )

        devices: dict[str, object] = {}
        for signal in self.contract.signals:
            device_name, _, leaf = signal.name.rpartition(".")
            parent = plant
            if device_name:
                if device_name not in devices:
                    devices[device_name] = await plant.add_object(
                        ua.NodeId(device_name, self.idx), self._qname(device_name)
                    )
                parent = devices[device_name]
            await self._add_signal(parent, signal, leaf or signal.name)

        # The command block is one PLC-wide resource in the contract, not a
        # per-device one, so the methods that drive it live at plant level.
        await self._add_commands(plant)

    async def _add_signal(self, parent, signal: Signal, browse_name: str) -> None:
        node_id = ua.NodeId.from_string(f"ns={self.idx};{signal.opcua.id}")
        vtype = ua.VariantType[signal.opcua.type]

        node = await parent.add_variable(
            node_id,
            self._qname(browse_name),
            ua.Variant(self._zero(vtype), vtype),
            varianttype=vtype,
        )
        if signal.opcua.access == "RW":
            await node.set_writable(True)
        if signal.opcua.unit:
            await node.add_property(
                ua.NodeId(f"{node_id.Identifier}.EngineeringUnits", self.idx),
                self._qname("EngineeringUnits"),
                ua.EUInformation(DisplayName=ua.LocalizedText(signal.opcua.unit)),
            )

        self._node_ids[signal.name] = node_id
        self._by_node_id[node_id] = signal
        self._last_words[signal.name] = None

    def _qname(self, name: str) -> ua.QualifiedName:
        # Browse names belong in the contract's namespace, not in ns=0 where
        # they would collide with the standard address space.
        return ua.QualifiedName(name, self.idx)

    @staticmethod
    def _zero(vtype: ua.VariantType) -> float | bool | int:
        if vtype is ua.VariantType.Boolean:
            return False
        return 0.0 if vtype in (ua.VariantType.Float, ua.VariantType.Double) else 0

    async def _add_commands(self, plant) -> None:
        @uamethod
        async def Start(parent) -> ua.StatusCode:
            return await self._invoke(CmdCode.START)

        @uamethod
        async def Stop(parent) -> ua.StatusCode:
            return await self._invoke(CmdCode.STOP)

        await plant.add_method(
            ua.NodeId("Plant.Start", self.idx),
            self._qname("Start"),
            Start,
            [],
            # No output arguments: the result IS the call's StatusCode.
            [],
        )
        await plant.add_method(
            ua.NodeId("Plant.Stop", self.idx),
            self._qname("Stop"),
            Stop,
            [],
            [],
        )

    # --------------------------------------------------------------- commands

    async def _invoke(self, code: CmdCode) -> ua.StatusCode:
        """Returns - never raises - the StatusCode for the call itself.

        asyncua turns a returned ua.StatusCode into the CallMethodResult's own
        status, but collapses any raised exception into BadUnexpectedError. So
        raising here would erase the difference between "the PLC rejected this"
        and "the PLC never heard it", which is the one thing a caller needs.
        """
        try:
            result = await self.commands.invoke(code)
        except CommandTimeout as exc:
            # Never acknowledged: we do not know whether it ran.
            log.warning("%s timed out: %s", code.name, exc)
            return ua.StatusCode(ua.StatusCodes.BadTimeout)
        except (ModbusException, OSError) as exc:
            log.warning("%s failed: %s", code.name, exc)
            return ua.StatusCode(STALE_STATUS)

        log.info("%s -> %s", code.name, result.name)
        return ua.StatusCode(ACK_STATUS[result])

    # ------------------------------------------------------------------ polls

    async def publish(self, reading: Reading) -> None:
        node_id = self._node_ids[reading.signal.name]
        vtype = ua.VariantType[reading.signal.opcua.type]

        if reading.ok:
            # Record the registers before publishing: publishing wakes the
            # write watcher, which uses them to recognise its own echo.
            self._last_words[reading.signal.name] = reading.words
            value = ua.DataValue(
                Value=ua.Variant(reading.value, vtype),
                StatusCode=ua.StatusCode(ua.StatusCodes.Good),
                SourceTimestamp=reading.at,
                ServerTimestamp=datetime.now(UTC),
            )
        else:
            self._last_words[reading.signal.name] = None
            value = ua.DataValue(
                StatusCode=ua.StatusCode(STALE_STATUS),
                SourceTimestamp=reading.at,
                ServerTimestamp=datetime.now(UTC),
            )

        await self.server.write_attribute_value(node_id, value)

    # ------------------------------------------------------------ client writes

    async def on_node_changed(self, node, value) -> None:
        signal = self._by_node_id.get(node.nodeid)
        if signal is None or value is None:
            return
        if self._last_words.get(signal.name) is None:
            # Nothing has been polled from this address yet - either we just
            # subscribed (asyncua delivers the node's initial value straight
            # away, which is not a client write) or the PLC is unreachable.
            # Either way there is no value here worth pushing down.
            return

        words = tuple(encode(signal.modbus, value, self.contract.meta))
        if words == self._last_words.get(signal.name):
            # This is our own poll being published back, not a client write.
            return

        try:
            await self.link.write_registers(signal.modbus.addr, list(words))
        except (ModbusException, OSError) as exc:
            log.warning("write %s <- %s failed: %s", signal.name, value, exc)
            return
        log.info("%s <- %s", signal.name, value)
        self._last_words[signal.name] = words

    # ------------------------------------------------------------------- run

    async def run(self) -> None:
        async with self.server:
            # Serve first, connect south second. A gateway whose PLC is down is
            # still a gateway: it has to be browsable and say so on every node,
            # not refuse to start.
            await self.link.connect()
            # Prime the nodes from the PLC before watching them, so the first
            # values a client sees are the PLC's and not this server's zeros.
            for reading in await self.poller.poll_once():
                await self.publish(reading)

            watcher = await self.server.create_subscription(50, _WriteWatcher(self))
            await watcher.subscribe_data_change(
                [
                    self.server.get_node(self._node_ids[s.name])
                    for s in self.contract.signals
                    if s.opcua.access == "RW"
                ]
            )
            log.info(
                "serving %s, ns=%d %s, polling every %d ms",
                self.endpoint,
                self.idx,
                self.contract.meta.namespace_uri,
                self.contract.meta.poll_period_ms,
            )
            try:
                await self.poller.run(self.publish)
            finally:
                self.link.close()


async def serve(contract: Contract, plc: tuple[str, int], endpoint: str) -> None:
    gateway = Gateway(contract, plc[0], plc[1], endpoint)
    await gateway.init()
    await gateway.run()
