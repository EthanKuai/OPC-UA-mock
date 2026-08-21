"""Certificate validation, with somewhere for the refusals to go.

asyncua turns away an untrusted certificate on its own. What it does not do is
keep it, and the first question after a refused connection is which
certificate was refused - which needs to be a file an operator can look at,
and move into the trusted folder if it turns out to belong there.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from asyncua.common.utils import ServiceError
from asyncua.crypto.truststore import TrustStore
from asyncua.crypto.validator import CertificateValidator, CertificateValidatorOptions
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

log = logging.getLogger("security")


class RejectingValidator:
    """Full validation against a trust list, and every failure filed by
    thumbprint - the same name the other end will have logged."""

    def __init__(self, trusted: Path, rejected: Path) -> None:
        self.rejected = rejected
        # _keep() writes here on every refusal. ensure_certificates() makes
        # this folder too, but only serve() depending on that would mean the
        # first refusal a gateway started some other way ever sees is a
        # crash instead of a clean rejection.
        self.rejected.mkdir(parents=True, exist_ok=True)
        self._trust_store = TrustStore([trusted], [])
        self._validator = CertificateValidator(
            CertificateValidatorOptions.TRUSTED_VALIDATION
            | CertificateValidatorOptions.PEER_CLIENT,
            self._trust_store,
        )

    async def load(self) -> None:
        await self._trust_store.load()

    async def __call__(self, cert: x509.Certificate, app_description) -> None:
        try:
            await self._validator.validate(cert, app_description)
        except ServiceError:
            path = self._keep(cert)
            log.warning(
                "refused %s; certificate kept at %s",
                app_description.ApplicationUri,
                path,
            )
            raise

    def _keep(self, cert: x509.Certificate) -> Path:
        der = cert.public_bytes(Encoding.DER)
        # SHA-1 is the thumbprint algorithm OPC UA specifies. It names the file
        # here; nothing is being secured by it.
        digest = hashlib.sha1(der, usedforsecurity=False).hexdigest()
        path = self.rejected / f"{digest}.der"
        path.write_bytes(der)
        return path
