"""The gateway's northbound half: an OPC UA server generated from the contract.

Nothing here is hand-built per signal. The address space, the data types, the
units and the access levels all come out of config/tags.yaml, so a signal added
to the contract appears on OPC UA without touching this file.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import struct
from datetime import UTC, datetime, timedelta

from asyncua import Server, ua
from asyncua.common.callback import CallbackType
from asyncua.common.methods import uamethod
from asyncua.common.statemachine import State, StateMachine, Transition
from asyncua.crypto.permission_rules import User, UserRole
from pymodbus.exceptions import ModbusException

from contract import AckResult, CmdCode, Contract, Signal, encode
from security import RejectingValidator, ServerSecurity

from .commands import CommandContended, CommandHandshake, CommandTimeout
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

# How much history to keep for the signals the contract asks to be historised.
# In memory, so both limits matter: whichever is reached first wins.
HISTORY_PERIOD = timedelta(minutes=10)
HISTORY_COUNT = 10_000

# Another master has commanded the PLC since we last did. The command was not
# sent: the caller's picture of the plant is already wrong, and issuing on top
# of it would only make the next picture wrong too.
CONTENDED_STATUS = ua.StatusCodes.BadSequenceNumberUnknown

# The two state_type values StateMachine.add_state accepts that this gateway
# ever produces: the lowest-numbered declared state is the machine's initial
# one, every other declared state is a plain one.
INITIAL_STATE_TYPE = ua.NodeId(2309, 0)
STATE_TYPE = ua.NodeId(2307, 0)


class _Operators:
    """The UserName token half of the endpoint: a name and a password, checked
    against the accounts the gateway was started with."""

    def __init__(self, users: dict[str, str]) -> None:
        self.users = users

    def get_user(self, iserver, username=None, password=None, certificate=None):
        expected = self.users.get(username or "")
        if expected is None or password is None:
            return None
        if not hmac.compare_digest(expected, password):
            return None
        return User(role=UserRole.User, name=username)


class Gateway:
    def __init__(
        self,
        contract: Contract,
        plc_host: str,
        plc_port: int,
        endpoint: str,
        security: ServerSecurity | None = None,
    ) -> None:
        self.contract = contract
        self.link = ModbusLink(contract, plc_host, plc_port)
        self.poller = Poller(self.link, contract)
        self.commands = CommandHandshake(self.link, contract)
        self.security = security
        self.server = Server(
            user_manager=_Operators(security.users) if security else None
        )
        self.endpoint = endpoint

        # Set once every signal has a confirmed PLC state. Not needed for the
        # write path itself - the value setter checks _last_words per signal
        # regardless - but a caller that wants to write right after startup
        # without individually handling BadWaitingForInitialData can wait on
        # this instead.
        self.primed = asyncio.Event()
        self._stopping = asyncio.Event()

        self.idx: int | None = None
        self._node_ids: dict[str, ua.NodeId] = {}
        self._by_node_id: dict[ua.NodeId, Signal] = {}
        # What the PLC last reported, per signal, as raw registers. None until
        # the first poll lands, which is also how a value setter knows there
        # is nothing yet to accept a client's write against.
        self._last_words: dict[str, tuple[int, ...] | None] = {}
        # NodeIds a Write service request is about to touch, populated by the
        # PreWrite callback and consumed by the value setter below. Only a
        # write that passed through here came from a client; the poll loop's
        # own write_attribute_value() calls never do.
        self._external_writes: set[ua.NodeId] = set()

        # Per state-machine signal: the StateMachine itself, its declared
        # states and transitions by register code, and the code last
        # published - so a transition is only recorded on a real change, not
        # re-stamped with "now" every poll like CurrentState is.
        self._machines: dict[str, StateMachine] = {}
        self._states: dict[str, dict[int, State]] = {}
        self._transitions: dict[str, dict[int, Transition]] = {}
        self._last_state_code: dict[str, int | None] = {}

    # ---------------------------------------------------------------- startup

    async def init(self) -> None:
        await self.server.init()
        if self.security is not None:
            await self._lock_down(self.security)
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
        self.server.subscribe_server_callback(
            CallbackType.PreWrite, self._mark_external_write
        )

    async def _lock_down(self, security: ServerSecurity) -> None:
        """One encrypted endpoint, one identity token, one trust list."""
        # The URI has to match the one inside the certificate, or the server
        # refuses its own certificate before any client turns up.
        await self.server.set_application_uri(security.application_uri)
        await self.server.load_certificate(security.certificate)
        await self.server.load_private_key(security.private_key)
        # No NoSecurity endpoint at all, so an anonymous client has nothing to
        # connect to rather than something to be turned away from.
        self.server.set_security_policy(
            [ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt]
        )
        self.server.set_identity_tokens([ua.UserNameIdentityToken])

        validator = RejectingValidator(security.trusted, security.rejected)
        await validator.load()
        self.server.set_certificate_validator(validator)
        log.info(
            "Basic256Sha256/SignAndEncrypt, UserName only, trusting %s",
            security.trusted,
        )

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
            if signal.opcua.states is not None:
                await self._add_state_signal(parent, signal, leaf or signal.name)
            else:
                await self._add_signal(parent, signal, leaf or signal.name)

        # The command block is one PLC-wide resource in the contract, not a
        # per-device one, so the methods that drive it live at plant level.
        await self._add_commands(plant)

    async def _historize(self) -> None:
        """Keep the signals the contract says to keep.

        The history subscribes to the node, so it stores changes rather than
        polls: the gateway rewrites every node every poll period, and a value
        that did not move is not a sample worth keeping.
        """
        keep = [s for s in self.contract.signals if s.opcua.historize]
        if not keep:
            return
        await self.server.historize_node_data_change(
            [self.server.get_node(self._node_ids[s.name]) for s in keep],
            HISTORY_PERIOD,
            HISTORY_COUNT,
        )
        log.info("historising %s", ", ".join(s.name for s in keep))

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
            self.server.set_attribute_value_setter(node_id, self._set_signal)
        if signal.opcua.unit:
            await node.add_property(
                ua.NodeId(f"{node_id.Identifier}.EngineeringUnits", self.idx),
                self._qname("EngineeringUnits"),
                ua.EUInformation(DisplayName=ua.LocalizedText(signal.opcua.unit)),
            )

        self._node_ids[signal.name] = node_id
        self._by_node_id[node_id] = signal
        self._last_words[signal.name] = None

    async def _add_state_signal(self, parent, signal: Signal, browse_name: str) -> None:
        """A conforming FiniteStateMachineType instead of a plain Variable:
        CurrentState and LastTransition, generated from opcua.states rather
        than hand-built per signal - the same "add it to the contract, not
        to this file" promise ordinary signals get."""
        codes = sorted(signal.opcua.states)
        initial = codes[0]

        machine = StateMachine(self.server, parent, self.idx, browse_name)
        await machine.install(optionals=True)
        # _write_state()/_write_transition() write State.node.nodeid into the
        # Id property as a NodeId, but the standard nodeset's own instance of
        # that property is typed String - every real transition then raises
        # BadTypeMismatch. Id is documentary; the register code in self._states
        # is what this gateway actually keys off, so disable just that write.
        machine._current_state_id_node = None
        machine._last_transition_id_node = None

        states: dict[int, State] = {}
        transitions: dict[int, Transition] = {}
        for code in codes:
            name = signal.opcua.states[code]
            state_type = INITIAL_STATE_TYPE if code == initial else STATE_TYPE
            # +1: StateMachine.add_state() checks `if not state.number`, so a
            # register code of 0 (this contract's own initial-state code) is
            # read as "missing" and rejected. The OPC UA StateNumber is
            # cosmetic - the register code is what's authoritative - so
            # shifting it is free.
            number = code + 1
            states[code] = State(str(code), name, number)
            await machine.add_state(states[code], state_type=state_type)
            transitions[code] = Transition(str(code), f"To{name}", number)
            await machine.add_transition(transitions[code])

        self._machines[signal.name] = machine
        self._states[signal.name] = states
        self._transitions[signal.name] = transitions
        self._last_state_code[signal.name] = None

        # CurrentState is what a client subscribes to and what a poll
        # failure has to mark Bad - both keyed the same way as a plain
        # signal's own Value node. _current_state_node is private; install()
        # gives no public way to get it back.
        self._node_ids[signal.name] = machine._current_state_node.nodeid
        self._by_node_id[self._node_ids[signal.name]] = signal
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

        @uamethod
        async def CncStart(parent) -> ua.StatusCode:
            return await self._invoke(CmdCode.CNC_START)

        @uamethod
        async def CncReset(parent) -> ua.StatusCode:
            return await self._invoke(CmdCode.CNC_RESET)

        for name, fn in (
            ("Start", Start),
            ("Stop", Stop),
            ("CncStart", CncStart),
            ("CncReset", CncReset),
        ):
            await plant.add_method(
                ua.NodeId(f"Plant.{name}", self.idx),
                self._qname(name),
                fn,
                [],
                # No output arguments: the result IS the call's StatusCode.
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
        except CommandContended as exc:
            log.warning("%s refused: %s", code.name, exc)
            return ua.StatusCode(CONTENDED_STATUS)
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
        if reading.signal.opcua.states is not None:
            await self._publish_state(reading)
            return

        node_id = self._node_ids[reading.signal.name]
        vtype = ua.VariantType[reading.signal.opcua.type]

        if reading.ok:
            # Record the registers before publishing: publishing is what
            # invokes the value setter, which reads _last_words to know
            # whether this signal has a confirmed PLC state yet.
            self._last_words[reading.signal.name] = reading.words
            value = ua.DataValue(
                Value=ua.Variant(reading.value, vtype),
                StatusCode=ua.StatusCode(ua.StatusCodes.Good),
                SourceTimestamp=reading.at,
                ServerTimestamp=datetime.now(UTC),
            )
        else:
            self._last_words[reading.signal.name] = None
            value = self._bad_value(reading.at)

        await self.server.write_attribute_value(node_id, value)

    async def _publish_state(self, reading: Reading) -> None:
        """CurrentState follows the same poll straight through like any other
        signal, but a *transition* is only recorded - LastTransition stamped
        with now - on a code that actually differs from the last one
        published, or every poll would look like a fresh transition."""
        name = reading.signal.name
        node_id = self._node_ids[name]

        if not reading.ok:
            self._last_words[name] = None
            self._last_state_code[name] = None
            await self.server.write_attribute_value(
                node_id, self._bad_value(reading.at)
            )
            return

        self._last_words[name] = reading.words
        code = int(reading.value)
        state = self._states[name].get(code)
        if state is None:
            log.warning("%s: no declared state for register code %d", name, code)
            self._last_state_code[name] = None
            await self.server.write_attribute_value(
                node_id, self._bad_value(reading.at)
            )
            return

        changed = code != self._last_state_code[name]
        self._last_state_code[name] = code
        transition = self._transitions[name][code] if changed else None
        await self._machines[name].change_state(state, transition)

    @staticmethod
    def _bad_value(at: datetime) -> ua.DataValue:
        return ua.DataValue(
            StatusCode=ua.StatusCode(STALE_STATUS),
            SourceTimestamp=at,
            ServerTimestamp=datetime.now(UTC),
        )

    # ------------------------------------------------------------ client writes

    def _mark_external_write(self, event, _service) -> None:
        """PreWrite fires only for a real client's Write service request - the
        poll loop's own write_attribute_value() calls bypass the session
        entirely and never reach it. Recording the NodeIds here is how the
        value setter below tells the two apart."""
        if event.is_external:
            self._external_writes.update(
                wv.NodeId for wv in event.request_params.NodesToWrite
            )

    def _set_signal(self, node, attr, datavalue: ua.DataValue) -> None:
        """The setter for every writable node's Value attribute. Runs
        synchronously in the write path - the poll loop's republish and a
        client's write alike - so a client's write_value() gets a real
        StatusCode back rather than a value the address space accepted and
        then quietly dropped.
        """
        if node.nodeid in self._external_writes:
            self._external_writes.discard(node.nodeid)
            signal = self._by_node_id[node.nodeid]
            if self._last_words.get(signal.name) is None:
                # Nothing has been polled from this address yet, so there is
                # no confirmed PLC state to write against.
                raise ua.uaerrors.BadWaitingForInitialData()
            try:
                words = encode(signal.modbus, datavalue.Value.Value, self.contract.meta)
            except struct.error:
                raise ua.uaerrors.BadOutOfRange() from None
            # write_registers() awaits, which a setter cannot - it hands off
            # to a task and returns the caller a synchronous accept.
            asyncio.create_task(self._push(signal, datavalue.Value.Value, words))
        node.attributes[attr].value = datavalue

    async def _push(self, signal: Signal, value, words: list[int]) -> None:
        try:
            await self.link.write_registers(signal.modbus.addr, words)
        except (ModbusException, OSError) as exc:
            log.warning("write %s <- %s failed: %s", signal.name, value, exc)
            return
        log.info("%s <- %s", signal.name, value)

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
            self.primed.set()

            # After the server is up: historising subscribes to the nodes, and
            # a subscription made before there is a server to publish it never
            # delivers anything past the value it started with.
            await self._historize()

            log.info(
                "serving %s, ns=%d %s, polling every %d ms",
                self.endpoint,
                self.idx,
                self.contract.meta.namespace_uri,
                self.contract.meta.poll_period_ms,
            )
            # Polling runs as its own task so that stopping can be asked for
            # rather than imposed. Cancelling run() from outside interrupts
            # asyncua's shutdown half way through, and the listening socket is
            # never closed - the port stays taken until the process exits.
            poll = asyncio.create_task(self.poller.run(self.publish))
            stop = asyncio.create_task(self._stopping.wait())
            try:
                done, pending = await asyncio.wait(
                    {poll, stop}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    # The poll loop ends only by failing; say so rather than
                    # returning as though the gateway had been asked to stop.
                    task.result()
            finally:
                self.link.close()

    async def stop(self) -> None:
        """Ask run() to return, so the server shuts itself down properly."""
        self._stopping.set()


async def serve(
    contract: Contract,
    plc: tuple[str, int],
    endpoint: str,
    security: ServerSecurity | None = None,
) -> None:
    gateway = Gateway(contract, plc[0], plc[1], endpoint, security)
    await gateway.init()
    await gateway.run()
