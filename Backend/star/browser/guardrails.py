"""Browser guardrails — the URL policy the browser agent cannot talk its way past.

Blueprint non-negotiables this module exists for:

* **#2 every OS/network action passes the safety layer** — the agent never opens a
  socket or a browser window without a :class:`UrlVerdict` saying ``ok=True``.
* **#3 dry-run default** — verdicts are computed for simulated runs too, so a
  dry-run still *reports* what would have been refused.

The policy is deliberately boring:

``http``/``https`` only · no embedded credentials · no private, loopback,
link-local, multicast or reserved addresses (unless explicitly allowed) ·
host allow/deny lists · port allow-list · length cap · optional DNS check of
every resolved address (SSRF hardening).
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.parse
from dataclasses import dataclass
from typing import Any

from Backend.star.config.settings import Settings
from Backend.star.observability.events import EventPhase
from Backend.star.observability.logging import star_logger

__all__ = [
    "UrlVerdict",
    "BrowserGuardrails",
    "find_urls",
    "is_private_name",
    "MAX_URL_LENGTH",
]

_log = star_logger("star2.browser.guardrails")

MAX_URL_LENGTH = 2048

#: things that look like a URL inside free text (voice transcripts are messy)
_URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>\"'()\[\]]+", re.IGNORECASE)
_BARE_HOST_RE = re.compile(r"^(?!-)[a-z0-9\u00a1-\uffff-]+(?:\.[a-z0-9\u00a1-\uffff-]+)+$", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)

#: hostnames that mean "this machine" / "the cloud metadata service" without
#: being IP literals — the DNS check is off by default, so they are listed here
_PRIVATE_HOSTNAMES: frozenset[str] = frozenset(
    {"localhost", "ip6-localhost", "ip6-loopback", "metadata", "metadata.google.internal", "instance-data"}
)
_PRIVATE_SUFFIXES: tuple[str, ...] = (".localhost", ".local", ".internal", ".lan", ".home", ".corp")


def find_urls(text: str) -> list[str]:
    """Pull candidate URLs out of free text, normalised, de-duplicated, in order."""
    out: list[str] = []
    for match in _URL_RE.findall(text or ""):
        url = match.rstrip(".,;:!?)")
        if url.lower().startswith("www."):
            url = f"https://{url}"
        if url not in out:
            out.append(url)
    return out


@dataclass(frozen=True, slots=True)
class UrlVerdict:
    """The answer to "may the browser touch this?" — plus *why*."""

    ok: bool
    url: str = ""
    scheme: str = ""
    host: str = ""
    port: int = 0
    reason: str = ""
    rule: str = ""            # empty | scheme | userinfo | host | port | length | private_host |
                              # denylist | allowlist | dns | idna
    risk: str = "low"         # low | medium — feeds classify_risk, never a deny on its own

    def public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "url": self.url,
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "reason": self.reason,
            "rule": self.rule,
            "risk": self.risk,
        }


class BrowserGuardrails:
    """Stateless URL policy. Cheap enough to call on every step of every run."""

    def __init__(self, settings: Settings | None = None, *, bus: Any = None) -> None:
        self.settings = settings or Settings()
        self.cfg = self.settings.browser
        self.bus = bus
        self.stats: dict[str, int] = {"checks": 0, "allowed": 0, "refused": 0}

    # ── the verdict ───────────────────────────────────────────────────────
    def check_url(self, url: str) -> UrlVerdict:
        raw = (url or "").strip().strip("<>\"'")
        self.stats["checks"] += 1
        verdict = self._evaluate(raw)
        self.stats["allowed" if verdict.ok else "refused"] += 1
        if self.bus is not None and not verdict.ok:
            # ``safety.blocked`` is the existing vocabulary for "policy said no"
            self.bus.emit(
                "safety.blocked",
                phase=EventPhase.SECURITY,
                layer="browser_guardrails",
                rule=verdict.rule,
                reason=verdict.reason,
                host=verdict.host,
                scheme=verdict.scheme,
            )
        return verdict

    def _evaluate(self, raw: str) -> UrlVerdict:
        if not raw:
            return UrlVerdict(False, reason="no url given", rule="empty")
        if len(raw) > MAX_URL_LENGTH:
            return UrlVerdict(False, url=raw[:120] + "…", reason=f"url longer than {MAX_URL_LENGTH} chars", rule="length")

        candidate = raw
        if "://" not in candidate and not _SCHEME_RE.match(candidate):
            # "example.com/page" and "www.example.com" are what people actually say
            head = candidate.split("/", 1)[0].split("?", 1)[0]
            if _BARE_HOST_RE.match(head) or _is_ip_literal(head):
                candidate = f"https://{candidate}"
            else:
                return UrlVerdict(False, url=raw, reason=f"'{raw}' is not a URL (no scheme, no dotted host)", rule="host")

        try:
            parts = urllib.parse.urlsplit(candidate)
        except ValueError as exc:
            return UrlVerdict(False, url=raw, reason=f"unparsable url: {exc}", rule="host")

        scheme = (parts.scheme or "").lower()
        if scheme not in self.cfg.allowed_schemes:
            return UrlVerdict(
                False,
                url=candidate,
                scheme=scheme,
                reason=f"scheme '{scheme or 'none'}' is not allowed ({', '.join(self.cfg.allowed_schemes)} only)",
                rule="scheme",
            )
        if parts.username or parts.password:
            return UrlVerdict(
                False, url=candidate, scheme=scheme,
                reason="embedded credentials in the URL are refused (they would land in logs/history)",
                rule="userinfo",
            )

        host = (parts.hostname or "").strip().rstrip(".").lower()
        if not host:
            return UrlVerdict(False, url=candidate, scheme=scheme, reason="url has no host", rule="host")
        try:
            host.encode("idna")
        except (UnicodeError, UnicodeDecodeError):
            return UrlVerdict(False, url=candidate, scheme=scheme, host=host, reason="host is not valid IDNA", rule="idna")

        port = parts.port or (443 if scheme == "https" else 80)

        if not self._host_allowed(host):
            return UrlVerdict(
                False, url=candidate, scheme=scheme, host=host, port=port,
                reason=f"host '{host}' is on the browser deny list", rule="denylist",
            )
        if self.cfg.host_allowlist and not self._host_in_list(host, self.cfg.host_allowlist):
            return UrlVerdict(
                False, url=candidate, scheme=scheme, host=host, port=port,
                reason=f"host '{host}' is not in the browser allow list", rule="allowlist",
            )

        ip_verdict = self._check_address(host, candidate, scheme, port)
        if ip_verdict is not None:
            return ip_verdict

        if parts.port is not None and parts.port not in self.cfg.allowed_ports:
            return UrlVerdict(
                False, url=candidate, scheme=scheme, host=host, port=port,
                reason=f"port {parts.port} is not in the allowed list {tuple(self.cfg.allowed_ports)}",
                rule="port",
            )

        risk = "medium" if (_is_ip_literal(host) or parts.port is not None) else "low"
        return UrlVerdict(True, url=candidate, scheme=scheme, host=host, port=port, risk=risk, reason="allowed")

    # ── helpers ───────────────────────────────────────────────────────────
    def _host_allowed(self, host: str) -> bool:
        return not self._host_in_list(host, self.cfg.host_denylist)

    @staticmethod
    def _host_in_list(host: str, entries: tuple[str, ...]) -> bool:
        for entry in entries:
            entry = entry.strip().lower().lstrip(".")
            if not entry:
                continue
            if host == entry or host.endswith("." + entry):
                return True
        return False

    def _check_address(self, host: str, url: str, scheme: str, port: int) -> UrlVerdict | None:
        """Refuse non-public addresses; optionally DNS-check every resolved IP."""
        if not self.cfg.allow_private_hosts and _is_private_name(host):
            return UrlVerdict(
                False, url=url, scheme=scheme, host=host, port=port,
                reason=f"'{host}' means this machine or a cloud metadata service — set STAR_BROWSER_ALLOW_PRIVATE=true to permit it",
                rule="private_host",
            )
        if _is_ip_literal(host):
            if self._is_public(host):
                return None
            if not self.cfg.allow_private_hosts:
                return UrlVerdict(
                    False, url=url, scheme=scheme, host=host, port=port,
                    reason=f"'{host}' is a private/loopback/reserved address — set STAR_BROWSER_ALLOW_PRIVATE=true to permit it",
                    rule="private_host",
                )
            return None

        if not self.cfg.resolve_hosts:
            return None
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, OSError) as exc:
            return UrlVerdict(
                False, url=url, scheme=scheme, host=host, port=port,
                reason=f"cannot resolve '{host}' ({exc})", rule="dns",
            )
        for info in infos:
            address = str(info[4][0])
            if not self._is_public(address) and not self.cfg.allow_private_hosts:
                return UrlVerdict(
                    False, url=url, scheme=scheme, host=host, port=port,
                    reason=f"'{host}' resolves to the non-public address {address} — refused (SSRF guard)",
                    rule="dns",
                )
        return None

    @staticmethod
    def _is_public(address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return True                     # not an IP literal → nothing to judge here
        return not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )

    # ── introspection ─────────────────────────────────────────────────────
    def describe(self) -> dict[str, Any]:
        return {
            "allowed_schemes": list(self.cfg.allowed_schemes),
            "allowed_ports": list(self.cfg.allowed_ports),
            "allow_private_hosts": self.cfg.allow_private_hosts,
            "resolve_hosts": self.cfg.resolve_hosts,
            "host_allowlist": list(self.cfg.host_allowlist),
            "host_denylist": list(self.cfg.host_denylist),
            "timeout_s": self.cfg.timeout_s,
            "max_bytes": self.cfg.max_bytes,
            "max_steps": self.cfg.max_steps,
            "auto_open": self.cfg.auto_open,
            **self.stats,
        }

    def health(self) -> dict[str, Any]:
        problems: list[str] = []
        if self.cfg.allow_private_hosts:
            problems.append("private/loopback hosts are allowed")
        if "*" in self.cfg.allowed_schemes or "file" in self.cfg.allowed_schemes:
            problems.append("dangerous URL scheme allowed")
        if self.cfg.auto_open:
            problems.append("auto-open launches a real browser window")
        return {"status": "degraded" if problems else "ok", "detail": {"problems": problems, **self.stats}}


def is_private_name(host: str) -> bool:
    """Public helper: does this hostname mean "not the internet"?"""
    return _is_private_name(host)


def _is_private_name(host: str) -> bool:
    lowered = host.lower().rstrip(".")
    if lowered in _PRIVATE_HOSTNAMES:
        return True
    return any(lowered.endswith(suffix) for suffix in _PRIVATE_SUFFIXES)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
