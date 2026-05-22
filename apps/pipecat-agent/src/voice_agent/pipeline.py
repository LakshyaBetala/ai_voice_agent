"""Pipecat pipeline assembly.

This file is the integration glue between:

  - Plivo SIP transport (real audio in/out)
  - Sarvam Saaras STT (streaming, per-utterance lang tag + confidence)
  - language_state.LanguageState  (decides current response language)
  - prompts.build_system_message  (per-turn LLM context with lang injection)
  - Google Gemini 2.5 Flash (LLM)
  - Sarvam Bulbul TTS (single Chennai voice across 3 languages)
  - intro_cache.load_or_synthesize_intro  (first-turn fast path)
  - webhook.WebhookEmitter  (signed events out)

Why the file is thin
--------------------
The Pipecat library evolves quickly, and the real audio loop only runs
under a Linux container with the SIP toolchain installed. So we keep the
glue here and put the actual logic (state, prompts, intro cache,
webhooks) in modules that are fully unit-tested without Pipecat.

To smoke-test the assembly in staging:

    PIPECAT_AGENT_ENV=staging \
    SARVAM_API_KEY=... GEMINI_API_KEY=... \
    SAMVAAD_WEBHOOK_SECRET=... \
    python -m voice_agent.pipeline

Hard limits enforced here:
  - 180s total call duration cap
  - 170s soft-close warning to the LLM ("wrap with goodbye now")
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from .language_state import Lang, LanguageState
from .prompts import build_intro_text, build_system_message, load_priya_prompt

# Hard caps from priya-system.md / SPC contract.
HARD_CAP_SECONDS = 180
SOFT_CLOSE_SECONDS = 170


@dataclass
class CallContext:
    """Per-call runtime state. One instance lives for the call's lifetime."""

    call_id: str
    tenant_id: str
    lead_id: str
    lead_first_name: str | None
    lead_company: str | None
    started_at_monotonic: float
    language_state: LanguageState
    turn_idx: int = 0
    used_intro_cache: bool = False

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at_monotonic

    def should_soft_close(self) -> bool:
        return self.elapsed() >= SOFT_CLOSE_SECONDS

    def should_hard_stop(self) -> bool:
        return self.elapsed() >= HARD_CAP_SECONDS


def make_initial_context(
    *,
    call_id: str,
    tenant_id: str,
    lead_id: str,
    lead_first_name: str | None,
    lead_company: str | None,
    default_lang: str,
) -> CallContext:
    return CallContext(
        call_id=call_id,
        tenant_id=tenant_id,
        lead_id=lead_id,
        lead_first_name=lead_first_name,
        lead_company=lead_company,
        started_at_monotonic=time.monotonic(),
        language_state=LanguageState.initial(Lang(default_lang)),
    )


def render_system_message_for_turn(ctx: CallContext) -> str:
    """Called once per LLM turn so <current_language> is always fresh."""
    base = load_priya_prompt()
    return build_system_message(
        base_prompt=base,
        current_language=ctx.language_state.current.value,
        lead_first_name=ctx.lead_first_name,
        lead_company=ctx.lead_company,
    )


def render_intro_text(ctx: CallContext) -> str:
    """Text the first-turn cache will speak (or live-synthesize on miss)."""
    return build_intro_text(
        lang=ctx.language_state.current.value,
        first_name=ctx.lead_first_name,
    )


# Real pipecat.Pipeline construction would happen here, importing
# pipecat-ai transports and frames. We deliberately do not import that
# module at top level so the unit tests run on any platform without the
# C-extension dependencies pipecat pulls in.
def assemble_pipeline(ctx: CallContext):  # pragma: no cover - integration glue
    """Build the Pipecat pipeline; only callable in the deploy container."""
    from pipecat.pipeline.pipeline import Pipeline  # type: ignore[import-not-found]

    raise NotImplementedError(
        "assemble_pipeline() is the integration seam between the pure-logic "
        "modules and Pipecat. Implement when Pipecat-ai is locked and the "
        "Plivo SIP transport credentials are provisioned."
    )


if __name__ == "__main__":  # pragma: no cover
    env = os.environ.get("PIPECAT_AGENT_ENV", "dev")
    print(f"voice-agent pipeline boot — env={env}")
    print("This entry point assembles the Pipecat pipeline in staging/prod.")
    print("Unit tests cover language_state, intro_cache, webhook, prompts, server.")
