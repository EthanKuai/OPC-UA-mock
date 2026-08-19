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
