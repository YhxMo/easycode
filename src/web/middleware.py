"""Origin and token checks for the local Web API."""

import hmac
import logging
import os
import urllib.parse

from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

_KNOWN_FRONTEND_ORIGINS = {
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
}


def _normalise_host(host: str) -> str:
    """Lowercase a host, strip an IPv6 ``[]`` wrapper and any zone id."""
    h = (host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if "%" in h:  # IPv6 zone id, e.g. fe80::1%lo0
        h = h.split("%", 1)[0]
    return h


def _is_loopback_host(host: str) -> bool:
    """True for loopback hostnames/addresses (localhost, 127.0.0.0/8, ::1)."""
    h = _normalise_host(host)
    return h.startswith("127.") or h in ("localhost", "::1", "0:0:0:0:0:0:0:1")


def _bind_is_loopback(bind_host: str) -> bool:
    """True when the *listening* address is loopback-only.

    ``0.0.0.0`` and any non-loopback address bind the control plane to the
    network, so they count as non-loopback (they demand a bearer token).
    """
    return _is_loopback_host(bind_host)


def _origin_is_local(
    origin: str | None, bind_host: str = "127.0.0.1", bind_port: int = 8000
) -> bool:
    """True when a state-change request may proceed on origin geometry alone.

    Tightened rules:
    - Missing Origin/Referer (curl, non-browser tooling, our own tests) is
      accepted ONLY when the listener is bound to loopback.
    - A present Origin/Referer is accepted only when it is the exact same origin
      as the listener (http/https + same host family + same port), or it is one
      of the known dev-frontend origins.
    - The old equivalences are removed: an Origin whose host equals the Host
      header hostname (the DNS-rebinding channel) and any loopback host on an
      arbitrary port no longer pass.
    """
    if not origin:
        return _bind_is_loopback(bind_host)
    norm = origin.strip().rstrip("/")
    if norm in _KNOWN_FRONTEND_ORIGINS:
        return True
    # A Referer fallback may carry a path (e.g. http://localhost:5173/chat) that
    # still belongs to a known dev frontend source.
    if any(norm.startswith(k + "/") for k in _KNOWN_FRONTEND_ORIGINS):
        return True
    try:
        parts = urllib.parse.urlsplit(norm)
    except ValueError:
        return False
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False
    origin_host = (parts.hostname or "").strip().lower()
    if origin_host in ("", "null"):
        return False
    origin_port = parts.port or (443 if scheme == "https" else 80)
    if origin_port != bind_port:
        return False
    if _bind_is_loopback(bind_host):
        # Keep localhost<->127.0.0.1 interchangeable for real local use (the
        # built frontend is served at either), while rejecting any non-loopback
        # or cross-port origin.
        return _is_loopback_host(origin_host)
    # Non-loopback bind: bearer-token auth (checked in the middleware) is the
    # real gate. Reaching here means a well-formed http(s) origin on the bound
    # port; requests without a token never get this far on a remote bind anyway.
    return True


class _LocalOriginMiddleware:
    """Reject cross-origin state-changing requests (POST/PUT/PATCH/DELETE).

    On a non-loopback bind a valid ``Authorization: Bearer $EASYCODE_WEB_TOKEN``
    is additionally required. If ``EASYCODE_WEB_TOKEN`` is unset the control
    plane is deliberately **fail-closed** — every state-change request is
    rejected and a warning is logged at startup (security first: never expose
    the credentialed control plane to the network without an explicit token).
    """

    def __init__(self, app, bind_host: str = "127.0.0.1", bind_port: int = 8000):
        self.app = app
        self.bind_host = bind_host
        self.bind_port = bind_port
        if not _bind_is_loopback(bind_host) and not os.environ.get("EASYCODE_WEB_TOKEN"):
            logger.warning(
                "Web control plane bound to non-loopback host %r without "
                "EASYCODE_WEB_TOKEN set; state-changing requests are FAIL-CLOSED "
                "(all rejected). To use the control plane over a non-loopback "
                "bind, set EASYCODE_WEB_TOKEN and send 'Authorization: Bearer "
                "<token>' on every state-change request.",
                bind_host,
            )

    @staticmethod
    def _token_ok(headers: dict[bytes, bytes]) -> bool:
        expected = os.environ.get("EASYCODE_WEB_TOKEN")
        if not expected:
            return False
        auth = headers.get(b"authorization")
        if not auth:
            return False
        auth = auth.decode("latin-1")
        scheme, _, cred = auth.partition(" ")
        if scheme.lower() != "bearer" or not cred.strip():
            return False
        return hmac.compare_digest(cred.strip(), expected)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in ("POST", "PUT", "PATCH", "DELETE"):
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            origin = headers.get(b"origin")
            referer = headers.get(b"referer")
            source = None
            if origin:
                source = origin.decode("latin-1")
            elif referer:
                source = referer.decode("latin-1")
            if not _origin_is_local(source, self.bind_host, self.bind_port):
                response = JSONResponse(
                    {"detail": "cross-origin request rejected"}, status_code=403
                )
                await response(scope, receive, send)
                return
            if not _bind_is_loopback(self.bind_host) and not self._token_ok(headers):
                response = JSONResponse({"detail": "bearer token required"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
