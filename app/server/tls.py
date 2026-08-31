"""Trust the certificate authorities the operating system trusts.

Python does not use the OS trust store.  It ships its own CA bundle via
``certifi``, which knows the public authorities and nothing else.  On a
corporate network that inspects TLS - Zscaler, Netskope, Palo Alto and the
like - every HTTPS connection is re-signed by a company CA that the OS has
been told to trust but ``certifi`` has not.  The browser works; Python fails
with:

    [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate

``truststore`` fixes this by making Python's ssl module read the platform
store: the Windows certificate store, macOS Keychain, or the system CA
directory on Linux.  Injecting it means the app trusts exactly what the
machine trusts - no more, no less.

Verification is never disabled here.  Turning it off would make the app
accept any certificate at all, which on the very networks that need this fix
is precisely the wrong response.
"""

from __future__ import annotations

import os
from typing import Any

_state: dict[str, Any] = {}


def configure() -> dict:
    """Point Python's TLS at the OS trust store. Safe to call more than once.

    Returns a dict describing what is in use, for the health check and the
    preflight to report.
    """
    if _state:
        return _state

    preference = (os.getenv("CADSMITH_TRUST_STORE") or "system").strip().lower()
    bundle = os.getenv("SSL_CERT_FILE") or os.getenv("REQUESTS_CA_BUNDLE")

    if bundle:
        _state.update(source="bundle", ok=True,
                      detail=f"using the CA bundle at {bundle}")
        return _state

    if preference == "certifi":
        _state.update(source="certifi", ok=True,
                      detail="using certifi (CADSMITH_TRUST_STORE=certifi)")
        return _state

    try:
        import truststore

        truststore.inject_into_ssl()
        _state.update(source="system", ok=True,
                      detail="using the operating system certificate store")
    except ImportError:
        _state.update(
            source="certifi", ok=True,
            detail=("using certifi; the OS certificate store is not in use. "
                    "On a network that inspects TLS, install truststore: "
                    "pip install truststore"))
    except Exception as exc:
        _state.update(
            source="certifi", ok=False,
            detail=f"could not use the OS certificate store ({exc}); "
                   f"falling back to certifi")
    return _state


def status() -> dict:
    return dict(_state) if _state else configure()
