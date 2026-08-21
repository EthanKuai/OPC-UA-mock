"""Application instance certificates for the plant.

OPC UA identifies applications, not only users: above SecurityPolicy None
every application needs a certificate whose SubjectAltName URI matches the
ApplicationUri it announces. Those two drifting apart is the usual reason a
secure connection is refused, so both come from here.
"""

from __future__ import annotations

import shutil
import socket
from dataclasses import dataclass
from pathlib import Path

from asyncua.crypto.cert_gen import setup_self_signed_certificate
from cryptography.x509.oid import ExtendedKeyUsageOID

GATEWAY_URI = "urn:mock.local:plant:gateway"
CLIENT_URI = "urn:mock.local:plant:client"

# asyncua's own console scripts (uaread, uawrite, uals, ...) construct a bare
# Client() with no flag to override its application_uri, so a certificate for
# `just inspect` has to be issued under that hardcoded default, not ours.
INSPECT_URI = "urn:example.org:FreeOpcUa:opcua-asyncio"

# Demo credentials for a mock plant. A real deployment would not keep these in
# source, and would not give every operator the same account.
USER = "operator"
PASSWORD = "operator"

SUBJECT = {"countryName": "GB", "organizationName": "Mock Plant"}


@dataclass(frozen=True)
class ServerSecurity:
    certificate: Path
    private_key: Path
    trusted: Path
    rejected: Path
    application_uri: str
    users: dict[str, str]


@dataclass(frozen=True)
class ClientSecurity:
    certificate: Path
    private_key: Path
    server_certificate: Path
    application_uri: str
    username: str
    password: str


@dataclass(frozen=True)
class Certificates:
    """Where everything lives. The folder is the configuration."""

    root: Path

    @property
    def gateway_cert(self) -> Path:
        return self.root / "gateway.der"

    @property
    def gateway_key(self) -> Path:
        return self.root / "gateway-key.pem"

    @property
    def client_cert(self) -> Path:
        return self.root / "client.der"

    @property
    def client_key(self) -> Path:
        return self.root / "client-key.pem"

    @property
    def inspect_cert(self) -> Path:
        return self.root / "inspect.der"

    @property
    def inspect_key(self) -> Path:
        return self.root / "inspect-key.pem"

    @property
    def trusted(self) -> Path:
        return self.root / "trusted"

    @property
    def rejected(self) -> Path:
        return self.root / "rejected"

    def server(self) -> ServerSecurity:
        return ServerSecurity(
            self.gateway_cert,
            self.gateway_key,
            self.trusted,
            self.rejected,
            GATEWAY_URI,
            {USER: PASSWORD},
        )

    def client(self) -> ClientSecurity:
        return ClientSecurity(
            self.client_cert,
            self.client_key,
            self.gateway_cert,
            CLIENT_URI,
            USER,
            PASSWORD,
        )


async def ensure_certificates(root: Path) -> Certificates:
    """Generate whatever is missing and trust the client. Idempotent."""
    certs = Certificates(root)
    for folder in (root, certs.trusted, certs.rejected):
        folder.mkdir(parents=True, exist_ok=True)

    await issue(certs.gateway_key, certs.gateway_cert, GATEWAY_URI, server=True)
    await issue(certs.client_key, certs.client_cert, CLIENT_URI, server=False)
    await issue(certs.inspect_key, certs.inspect_cert, INSPECT_URI, server=False)

    # Trusting is a decision, not a by-product of generating. This one line is
    # the entire difference between the client and an intruder: both have a
    # perfectly valid certificate, and only one of them is in this folder.
    shutil.copyfile(certs.client_cert, certs.trusted / certs.client_cert.name)
    shutil.copyfile(certs.inspect_cert, certs.trusted / certs.inspect_cert.name)
    return certs


async def issue(key: Path, cert: Path, uri: str, *, server: bool) -> None:
    """Self-signed, because there is no CA here. A real plant would have one,
    and the trusted folder would hold its certificate instead of every peer's.

    The hostname goes into the certificate because asyncua checks its own
    against it at startup; a certificate generated on another machine is
    refused by the server that loads it.
    """
    await setup_self_signed_certificate(
        key,
        cert,
        uri,
        socket.gethostname(),
        [
            ExtendedKeyUsageOID.SERVER_AUTH
            if server
            else ExtendedKeyUsageOID.CLIENT_AUTH
        ],
        SUBJECT,
    )
