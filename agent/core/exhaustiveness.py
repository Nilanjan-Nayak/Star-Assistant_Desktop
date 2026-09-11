"""Helpers that make non-exhaustive ``match`` / ``if`` chains a type error."""

from __future__ import annotations

from typing import Never, NoReturn


def assert_never(value: Never) -> NoReturn:
    """Called from a ``match`` default. mypy errors if a variant was forgotten."""
    raise AssertionError(f"unhandled variant: {value!r}")
