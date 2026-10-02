"""STAR 2.0 configuration layer (env-driven, secret-free)."""

from __future__ import annotations

from Backend.star.config.settings import Settings, get_settings, reset_settings

__all__ = ["Settings", "get_settings", "reset_settings"]
