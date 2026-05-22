"""Tests for pipeline-level orchestration helpers (pure logic only).

The full Pipecat assembly is integration-tested on staging, not here.
"""
from __future__ import annotations

import time

import pytest

from voice_agent.language_state import Lang
from voice_agent.pipeline import (
    HARD_CAP_SECONDS,
    SOFT_CLOSE_SECONDS,
    make_initial_context,
    render_intro_text,
    render_system_message_for_turn,
)


def _ctx(lang: str = "en-IN", name: str | None = "Ravi", company: str | None = "Acme"):
    return make_initial_context(
        call_id="c1",
        tenant_id="t1",
        lead_id="L1",
        lead_first_name=name,
        lead_company=company,
        default_lang=lang,
    )


def test_initial_context_starts_in_default_language():
    ctx = _ctx(lang="hi-IN")
    assert ctx.language_state.current == Lang.HI
    assert ctx.turn_idx == 0
    assert ctx.used_intro_cache is False


def test_system_message_reflects_current_language_after_switch():
    ctx = _ctx(lang="en-IN")
    msg1 = render_system_message_for_turn(ctx)
    assert "<current_language>en-IN</current_language>" in msg1

    # Simulate state machine flipping to Hindi
    ctx.language_state.current = Lang.HI
    msg2 = render_system_message_for_turn(ctx)
    assert "<current_language>hi-IN</current_language>" in msg2
    assert "<current_language>en-IN</current_language>" not in msg2


def test_intro_text_uses_first_name_and_default_language():
    ctx = _ctx(lang="en-IN", name="Ravi")
    txt = render_intro_text(ctx)
    assert "Hello Ravi" in txt


def test_intro_text_falls_back_when_name_unusable():
    ctx = _ctx(lang="hi-IN", name="Unknown")
    txt = render_intro_text(ctx)
    assert "Unknown" not in txt
    assert "Priya" in txt


def test_soft_close_and_hard_stop_thresholds():
    ctx = _ctx()
    # Just-created context: nowhere near the caps.
    assert ctx.should_soft_close() is False
    assert ctx.should_hard_stop() is False

    # Fast-forward by mutating the start time.
    ctx.started_at_monotonic = time.monotonic() - SOFT_CLOSE_SECONDS - 1
    assert ctx.should_soft_close() is True
    assert ctx.should_hard_stop() is False

    ctx.started_at_monotonic = time.monotonic() - HARD_CAP_SECONDS - 1
    assert ctx.should_hard_stop() is True


def test_hard_cap_is_180_seconds_per_contract():
    """The 180s cap is a contractual SPC constraint, not a tunable.
    If this changes, update priya-system.md AND PRICING-AND-PLAN.md."""
    assert HARD_CAP_SECONDS == 180
    assert SOFT_CLOSE_SECONDS == 170
