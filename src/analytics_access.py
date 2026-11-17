"""Explicit, reversible HTTP compatibility policy. Contains no credentials."""

from __future__ import annotations

import json
import os
from pathlib import Path

from src.db import _load_env

POLICY_PATH = Path(__file__).resolve().parents[1] / ".analytics-access.json"


def allow_http_analytics() -> bool:
    """Environment takes precedence; absent/invalid policy keeps HTTPS required."""
    _load_env()
    override = os.getenv("ANALYTICS_ALLOW_HTTP")
    if override is not None:
        return override.strip().lower() in {"true", "1", "yes", "on"}
    try:
        with POLICY_PATH.open("rb") as source:
            raw = source.read(4097)
        if len(raw) > 4096:
            return False
        policy = json.loads(raw)
    except (OSError, ValueError, UnicodeError):
        return False
    return isinstance(policy, dict) and policy.get("allow_http") is True
