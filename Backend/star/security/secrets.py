"""Secret management, active scanner, and vault for Phase 11.

Blueprint §11 non-negotiables:
* Never hard-code secrets
* Active scanning & redaction of credentials in arguments, results, and logs
* API keys and tokens are resolved lazily and masked in all public outputs
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping

from Backend.star.observability.logging import star_logger

_log = star_logger("security.secrets")

#: Known secret environment variable names
KNOWN_SECRET_ENV_KEYS: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "YOUTUBE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "ELEVENLABS_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "GITHUB_TOKEN",
    "STAR_SECRET_KEY",
)

#: Common credential patterns
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"sk-[a-zA-Z0-9_\-]{20,}")),
    ("anthropic_key", re.compile(r"sk-ant-[a-zA-Z0-9_\-]{20,}")),
    ("google_key", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"gh[pous]_[a-zA-Z0-9]{36,}")),
    ("bearer_token", re.compile(r"(?i)bearer\s+[a-zA-Z0-9\-_.~+/=]{20,}")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("jwt_token", re.compile(r"eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}")),
)

#: Sensitive parameter name keywords
SENSITIVE_KEY_NAMES: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "auth_token",
        "private_key",
        "credentials",
        "secret_key",
    }
)


class SecretVault:
    """Secure vault resolving secrets lazily from the environment."""

    def __init__(self, custom_secrets: Mapping[str, str] | None = None) -> None:
        self._custom = dict(custom_secrets or {})

    def get_secret(self, key: str, default: str | None = None) -> str | None:
        """Fetch a secret value without logging it."""
        if key in self._custom:
            return self._custom[key]
        return os.environ.get(key, default)

    def has_secret(self, key: str) -> bool:
        """Check whether a secret is present."""
        return bool(self.get_secret(key))

    def register_secret(self, key: str, value: str) -> None:
        """Register an in-memory secret (does not write to disk/env)."""
        self._custom[key] = value

    def public_dict(self) -> dict[str, str]:
        """Never leak secret values: return <set> or <unset> only."""
        result: dict[str, str] = {}
        for key in sorted(set(KNOWN_SECRET_ENV_KEYS) | set(self._custom.keys())):
            result[key] = "<set>" if self.has_secret(key) else "<unset>"
        return result


class SecretScanner:
    """Active detector and redactor for credentials across payloads."""

    def is_sensitive_key(self, key: str) -> bool:
        """Check if an argument key implies sensitive credential content."""
        k = str(key or "").lower().replace("-", "_")
        return any(sensitive in k for sensitive in SENSITIVE_KEY_NAMES)

    def scan_text(self, text: str) -> list[tuple[str, str]]:
        """Identify credential matches in free text: returns list of (kind, masked_sample)."""
        if not text or not isinstance(text, str):
            return []
        found: list[tuple[str, str]] = []
        for kind, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                val = match.group(0)
                masked = val[:4] + "..." + val[-4:] if len(val) > 8 else "***"
                found.append((kind, masked))
        return found

    def redact_text(self, text: str) -> str:
        """Redact known secret patterns from string."""
        if not text or not isinstance(text, str):
            return text
        redacted = text
        for kind, pattern in SECRET_PATTERNS:
            redacted = pattern.sub(f"[REDACTED:{kind.upper()}]", redacted)
        return redacted

    def scan_payload(self, data: Any) -> tuple[bool, list[str]]:
        """Recursively scan an arbitrary payload for credential leaks."""
        kinds: list[str] = []

        def _walk(item: Any, key_name: str = "") -> None:
            if key_name and self.is_sensitive_key(key_name):
                if item and not str(item).startswith("[REDACTED"):
                    kinds.append(f"key:{key_name}")
            if isinstance(item, str):
                for kind, _ in self.scan_text(item):
                    kinds.append(kind)
            elif isinstance(item, dict):
                for k, v in item.items():
                    _walk(v, str(k))
            elif isinstance(item, (list, tuple, set)):
                for elem in item:
                    _walk(elem, key_name)

        _walk(data)
        return bool(kinds), list(set(kinds))

    def redact_payload(self, data: Any) -> Any:
        """Recursively scrub credentials from dicts, lists, and strings."""
        if isinstance(data, dict):
            out: dict[str, Any] = {}
            for k, v in data.items():
                if self.is_sensitive_key(str(k)):
                    out[k] = "[REDACTED]"
                else:
                    out[k] = self.redact_payload(v)
            return out
        if isinstance(data, list):
            return [self.redact_payload(item) for item in data]
        if isinstance(data, tuple):
            return tuple(self.redact_payload(item) for item in data)
        if isinstance(data, str):
            return self.redact_text(data)
        return data


# Global singleton scanner
_DEFAULT_SCANNER = SecretScanner()


def redact_secrets(data: Any) -> Any:
    return _DEFAULT_SCANNER.redact_payload(data)


def contains_secrets(data: Any) -> bool:
    has_secret, _ = _DEFAULT_SCANNER.scan_payload(data)
    return has_secret
