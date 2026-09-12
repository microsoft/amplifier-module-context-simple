"""Tests for honoring an explicitly-configured max_tokens as a budget ceiling.

Regression coverage for the bug where max_tokens was silently ignored whenever a
provider advertised a context window. On large-context models (e.g. a 1M-token
provider) the provider-derived budget was hundreds of thousands of tokens, so the
compaction threshold was never reached and context grew unbounded -- even when an
operator had explicitly set max_tokens to bound it.

The fix: when max_tokens is set explicitly (max_tokens_explicit=True), it acts as a
hard ceiling -- budget = min(provider_budget, max_tokens). When it is NOT explicit,
the provider window wins (historical behavior preserved).
"""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from amplifier_module_context_simple import SimpleContextManager, mount

# Provider-derived budget for a 1M window with the default 0.5 output reserve:
#   1_000_000 - int(128_000 * 0.5) - 4096 (safety margin) == 931_904
PROVIDER_BUDGET_1M = 1_000_000 - int(128_000 * 0.5) - 4096


@dataclass
class _ModelInfo:
    context_window: int
    max_output_tokens: int


class _ModelInfoProvider:
    """Provider exposing get_model_info() -- the modern budget path."""

    def __init__(self, context_window: int, max_output_tokens: int):
        self._info = _ModelInfo(context_window, max_output_tokens)

    def get_model_info(self):
        return self._info


class _DefaultsProvider:
    """Provider exposing only get_info().defaults -- the legacy budget path."""

    def __init__(self, context_window: int, max_output_tokens: int):
        self._defaults = {
            "context_window": context_window,
            "max_output_tokens": max_output_tokens,
        }

    def get_info(self):
        return SimpleNamespace(defaults=self._defaults)


class _FakeCoordinator:
    def __init__(self):
        self.hooks = None
        self.mounted: dict[str, object] = {}

    async def mount(self, name, obj):
        self.mounted[name] = obj


def test_explicit_max_tokens_caps_provider_budget():
    ctx = SimpleContextManager(max_tokens=300_000, max_tokens_explicit=True)
    provider = _ModelInfoProvider(context_window=1_000_000, max_output_tokens=128_000)
    assert ctx._calculate_budget(None, provider) == 300_000


def test_non_explicit_max_tokens_uses_full_provider_budget():
    # Default (not explicit): provider window wins -- historical behavior preserved.
    ctx = SimpleContextManager()
    provider = _ModelInfoProvider(context_window=1_000_000, max_output_tokens=128_000)
    assert ctx._calculate_budget(None, provider) == PROVIDER_BUDGET_1M


def test_explicit_cap_never_inflates_above_provider_budget():
    # The cap only lowers the budget; it never raises it above the provider value.
    ctx = SimpleContextManager(max_tokens=5_000_000, max_tokens_explicit=True)
    provider = _ModelInfoProvider(context_window=1_000_000, max_output_tokens=128_000)
    assert ctx._calculate_budget(None, provider) == PROVIDER_BUDGET_1M


def test_cap_applies_to_legacy_defaults_path():
    ctx = SimpleContextManager(max_tokens=250_000, max_tokens_explicit=True)
    provider = _DefaultsProvider(context_window=1_000_000, max_output_tokens=128_000)
    assert ctx._calculate_budget(None, provider) == 250_000


@pytest.mark.asyncio
async def test_mount_marks_explicit_only_when_configured():
    coord = _FakeCoordinator()
    await mount(coord, {"max_tokens": 123_456})
    ctx = coord.mounted["context"]
    assert ctx.max_tokens_explicit is True
    assert ctx.max_tokens == 123_456


@pytest.mark.asyncio
async def test_mount_no_explicit_flag_when_max_tokens_absent():
    coord = _FakeCoordinator()
    await mount(coord, {})
    ctx = coord.mounted["context"]
    assert ctx.max_tokens_explicit is False
