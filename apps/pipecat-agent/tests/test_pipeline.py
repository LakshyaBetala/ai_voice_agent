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


def test_hard_cap_is_360_seconds_per_dual_billing_model():
    """360s hard cap supports CP3 dual-billing: 0-180s = 1 unit, 181-360s = 2 units.
    A converting conversation rolls into a billed second unit instead of being cut.
    If this changes, update priya-system.md AND PRICING-AND-PLAN.md."""
    assert HARD_CAP_SECONDS == 360
    # SOFT_CLOSE_SECONDS is the legacy alias for SOFT_CLOSE_1_SECONDS (170s).
    assert SOFT_CLOSE_SECONDS == 170


def test_billed_units_match_dual_billing_model():
    """0-180s = 1 unit, 181-360s = 2 units. Verifies the in-process mirror
    of the DB trigger in 20260522180100_billed_units.sql."""
    import time as _t
    from voice_agent.pipeline import make_initial_context

    ctx = make_initial_context(
        call_id="c1", tenant_id="t1", lead_id="L1",
        lead_first_name=None, lead_company=None, default_lang="en-IN",
    )
    # 100s elapsed → 1 unit
    ctx.started_at_monotonic = _t.monotonic() - 100
    assert ctx.billed_units() == 1

    # Just under 180s → still 1 unit (DB CHECK uses integer duration_sec)
    ctx.started_at_monotonic = _t.monotonic() - 179
    assert ctx.billed_units() == 1

    # Just past 180s → 2 units
    ctx.started_at_monotonic = _t.monotonic() - 200
    assert ctx.billed_units() == 2

    # At hard cap → still 2 (the cap is the ceiling)
    ctx.started_at_monotonic = _t.monotonic() - 360
    assert ctx.billed_units() == 2


def test_two_stage_soft_close():
    """170s and 350s soft-closes are distinct; only the final one means 'wrap NOW'."""
    import time as _t
    from voice_agent.pipeline import make_initial_context, SOFT_CLOSE_1_SECONDS, SOFT_CLOSE_2_SECONDS

    ctx = make_initial_context(
        call_id="c1", tenant_id="t1", lead_id="L1",
        lead_first_name=None, lead_company=None, default_lang="en-IN",
    )
    ctx.started_at_monotonic = _t.monotonic() - (SOFT_CLOSE_1_SECONDS + 1)
    assert ctx.should_soft_close() is True
    assert ctx.should_soft_close_final() is False
    assert ctx.should_hard_stop() is False

    ctx.started_at_monotonic = _t.monotonic() - (SOFT_CLOSE_2_SECONDS + 1)
    assert ctx.should_soft_close() is True
    assert ctx.should_soft_close_final() is True
    assert ctx.should_hard_stop() is False
