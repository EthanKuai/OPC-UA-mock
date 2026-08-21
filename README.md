# OPC UA mock plant

A small industrial plant that exists to be wrong in the right places. A PLC
simulates a conveyor on a fixed scan; a gateway polls it over Modbus TCP and
serves it over OPC UA; a client subscribes and issues commands. A fifth
process, the rogue, is a second Modbus master with no business being there.

Everything below runs on one machine, in separate processes on purpose: the
process boundary is what forces the protocols to be real.

## The contract

`config/tags.yaml` is the single source of truth. Register addresses, scaling,
word order, OPC UA node ids, data types, engineering units, access levels, the
command and ack block layouts and both loop periods all come from it, through
`src/contract`. Nothing else hard-codes an address, and nothing hard-codes a
namespace index - the namespace URI is resolved to an index at connect time.

`owner:` is the one field the contract cannot enforce. It says who is supposed
to write a signal. Modbus has no opinion on the matter, which is what the
rogue is for.

`opcua:` has a second shape besides a plain Variable's `type`: `states`, a
register-code-to-name map, builds the signal as a conforming
`FiniteStateMachineType` (`CurrentState`, `LastTransition`) instead. `Cnc.State`
is the one example; its cycle is driven the same way Conveyor1's is, through
`CncStart`/`CncReset` methods and the same sequence-numbered command block.

## Running it

```fish
just certs      # once: application instance certificates, and the trust decision
just plc        # terminal 1
just gateway    # terminal 2
just client     # terminal 3
just rogue      # terminal 4, when you want to see the plant taken away
```

`just test` runs everything. `just latency` prints the budget below. `just
inspect` browses the live address space with asyncua's own console scripts
(`uals`, `uaread`, `uawrite`, `uacall`, `uasubscribe`, `uadiscover` - see the
justfile for the rest of them); for a GUI without Wine, `pip install
opcua-client-gui` is the closest thing to UaExpert.

## The five processes

| process | what it is |
|---|---|
| `plc` | fixed 10 ms scan, three image tables, watchdog, Modbus TCP server capped at 4 masters |
| `gateway` | Modbus master polling every 100 ms, OPC UA server generated from the contract |
| `client` | OPC UA client: subscribes, never polls; writes setpoints, never actuators |
| `rogue` | a second Modbus master, writing whatever it likes |
| `security` | provisions certificates into `certs/` |

The PLC executes only on its scan tick. A Modbus write mutates the image
table; the next scan reads it. Outputs reach the table only at end of scan.

## Commands, and why they are not register writes

A second master exists, so a bare write to a command register is not a
command. Every command is an FC16 write of `[cmd_code, arg0_lo, arg0_hi, seq]`
in one transaction, and the PLC answers by writing `ack_result` first and
`ack_seq` last. The gateway remembers the sequence it issued: if the PLC
reports back a number the gateway never sent, somebody else has been driving
the plant, and the OPC UA method call returns `BadSequenceNumberUnknown`
rather than a cheerful `Good`. A toggle bit could not do this - driven by two
masters it returns to where it started and looks untouched.

## Latency budget

End-to-end northbound latency is the sum of three configured periods:

| term | value |
|---|---|
| PLC scan period | 10 ms |
| gateway poll period | 100 ms |
| publishing interval | 100 ms |
| **budget** | **210 ms** |

Not four: asyncua's server does not sample - it notifies on change - so the
50 ms sampling interval the client requests is agreed to and then never spent.
It sets the effective sampling rate, but that rate is already the gateway's
100 ms poll period, already counted above.

A representative run on loopback, 25 samples: p50 99.6 ms, p99 200.0 ms.
`just latency` reprints the table. The two 100 ms terms dominate and the scan
is noise.

`tests/test_latency.py` writes a setpoint straight into the PLC over Modbus,
starts the clock there, and stops it when the resulting speed change reaches
the client's subscription handler.

## Security, and the door left open beside it

The OPC UA side is locked down:

- application instance certificates for gateway and client, with the
  SubjectAltName URI matching the announced ApplicationUri
- `Basic256Sha256` with `SignAndEncrypt`, and **no** `None` endpoint at all,
  so an anonymous client has nothing to connect to rather than something to be
  turned away from
- `UserName` as the only identity token
- a trust list: `certs/trusted/` holds the client certificate, and that copy
  is the entire difference between the client and an intruder
- refused certificates are written to `certs/rejected/`, named by thumbprint,
  so the first question after a failed connection has a file for an answer

**The rogue bypasses every line of that.** It presents no certificate, opens
no secure channel, authenticates as nobody, and writes the same registers the
gateway writes. `just rogue` takes a setpoint away from the client in about a
hundred milliseconds, and the client watches it happen on its own
subscription. The PLC cannot tell the two masters apart, because Modbus TCP
gives it nothing to tell them apart with: no session, no identity, no
arbitration.

Worse, the ack block is holding registers like anything else. A rogue that
writes `ack_seq` to the number the gateway is waiting for - and gets it in
before the PLC's next scan - makes a command return `Good` that the PLC never
executed: the PLC then sees `cmd_seq == ack_seq` and does nothing at all. No
sequence discipline can catch a forged acknowledgement; catching it needs
authentication, and there is none to be had on this wire. That one is reasoned
rather than tested, because winning a 10 ms race on purpose makes for a
flaky test.

That contrast is the point of the whole repository. Certificates on the
northbound side are worth having, and they secure exactly one of the two ways
into this plant.

## Layout

```
config/tags.yaml   the contract
src/contract       loader, validation, codec
src/machine        conveyor and CNC physics, pure, stepped by dt
src/plc            scan loop, image tables, Modbus server
src/gateway        Modbus client, OPC UA server, command handshake
src/client         OPC UA DeviceController
src/rogue          second Modbus master
src/security       certificates and the rejecting validator
certs/             generated, gitignored
```
