"""
Pytest configuration for module tests.

Behavioral tests use inheritance from amplifier-core base classes.
See tests/test_behavioral.py for the inherited tests.

The amplifier-core pytest plugin provides fixtures automatically:
- module_path: Detected path to this module
- module_type: Detected type (provider, tool, hook, etc.)
- provider_module, tool_module, etc.: Mounted module instances
"""

import copy

import pytest


@pytest.fixture
def media_metadata():
    """Synthetic canonical refs, not a store/admission or hydration fixture."""
    ref = {
        "v": 1,
        "session_id": "test-session",
        "sha256": "a" * 64,
        "mime": "image/png",
        "bytes": 123,
        "width": 10,
        "height": 20,
        "provenance": {
            "kind": "tool",
            "session_id": "test-session",
            "id": "call-media",
            "created_at": "2026-01-01T00:00:00Z",
            "expires_at": None,
        },
    }
    other = copy.deepcopy(ref)
    other["sha256"] = "b" * 64
    return {
        "experimental.media.v1": {"refs": [ref, other, copy.deepcopy(ref)]},
        "other-owner": {"keep": ["unchanged"]},
        "timestamp": "2026-01-01T00:00:00Z",
    }
