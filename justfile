test:
    uv run pytest

plc:
    uv run python -m plc

gateway:
    uv run python -m gateway

client:
    uv run python -m client

rogue:
    uv run python -m rogue

certs:
    uv run python -m security

latency:
    uv run pytest tests/test_latency.py -s -q

endpoint := "opc.tcp://127.0.0.1:4840/plant/server/"
# The gateway's endpoint is encrypted; asyncua's console scripts have no flag
# for their own application_uri, so they authenticate as the `inspect`
# identity `just certs` issues specifically for that hardcoded default.
security := "Basic256Sha256,SignAndEncrypt,certs/inspect.der,certs/inspect-key.pem,certs/gateway.der"

# Browse the address space with the shipped asyncua CLI - no code, no UaExpert.
inspect:
    uv run uals -u {{endpoint}} --security "{{security}}" --user operator --password operator -p "0:Objects,2:Plant" -d 3

# Same tools, same credentials, for a specific node once `inspect` has shown
# you its browse path (ns=2 is this gateway's one contract namespace):
#   uv run uaread  -u {{endpoint}} --security "{{security}}" --user operator --password operator -p "0:Objects,2:Plant,2:Conveyor1,2:ActualSpeed"
#   uv run uawrite -u {{endpoint}} --security "{{security}}" --user operator --password operator -p "0:Objects,2:Plant,2:Conveyor1,2:SpeedSetpoint" -t double 1.25
#   uv run uacall  -u {{endpoint}} --security "{{security}}" --user operator --password operator -p "0:Objects,2:Plant" -m 2:Start
#   uv run uasubscribe -u {{endpoint}} --security "{{security}}" --user operator --password operator -p "0:Objects,2:Plant,2:Conveyor1,2:ActualSpeed"
# uadiscover never opens a session, so it needs no security at all:
#   uv run uadiscover -u {{endpoint}}
