"""Shared fixtures."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load the integration from custom_components/."""
    return


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_token(**claims) -> str:
    """A session JWT shaped like the site's: unsigned here, since nothing verifies it."""

    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    payload = {"phone": "+380991112233", "passwordExists": True, "sessionId": "s1", **claims}
    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(payload)}.signature"
