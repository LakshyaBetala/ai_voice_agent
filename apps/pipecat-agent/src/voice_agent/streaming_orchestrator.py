"""Streaming turn orchestrator — LLM tokens flow into sentence-by-sentence TTS.

This replaces run_turn() for the real telephony path. Instead of waiting
for the FULL LLM response before starting TTS, we:

  1. STT (same as before)
  2. Start streaming Gemini + slot extraction in parallel
  3. Accumulate LLM tokens until a sentence boundary (।, ., ?, !)
  4. TTS each sentence independently (phrase cache checked per sentence)
  5. YIELD audio as each sentence completes → Exotel plays immediately

Result: lead hears Priya's first sentence ~1.5s after they stop talking
(vs 8-10s in the sequential orchestrator).

The old run_turn() still works for the local harness and tests. This
module adds a STREAMING alternative consumed by the WS handler.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional, Protocol

from .conversation_state import ConversationState, Phase, system_prompt_addendum
from .language_state import Lang, LanguageState, STTUtterance, Transition
from .pain_library import pick_pain_hypothesis
from .phrase_cache import PINNED_VOICE_ID, load_or_synthesize_phrase
from .qualification import QualificationSlots, extract_slots
from .prompts import build_system_message, load_priya_prompt
from .sarvam_stt import STTResult as _STTResult


# -- Protocols (same as turn_orchestrator but with streaming LLM) -----------

class STTAdapter(Protocol):
    async def transcribe(self, audio: bytes) -> "STTResultLike": ...


class STTResultLike(Protocol):
    transcript: str
    language_code: str
    confidence: float


class TTSAdapter(Protocol):
    async def synth(self, text: str, lang: str) -> bytes: ...


class StreamingLLMAdapter(Protocol):
    async def stream_respond(
        self, system_message: str, user_message: str
    ) -> AsyncIterator[str]:
        """Yield text chunks as the LLM generates them."""
        ...

    async def extract(self, prompt: str) -> str: ...


class R2Reader(Protocol):
    async def get(self, key: str) -> bytes | None: ...


class R2Writer(Protocol):
    async def put(self, key: str, body: bytes, content_type: str) -> None: ...


# -- Events yielded to the caller ------------------------------------------

@dataclass
class AudioChunkEvent:
    """One sentence of Priya's response, synthesized and ready to play."""
    audio: bytes
    text: str
    sentence_idx: int
    used_cache: bool


@dataclass
class TurnCompleteEvent:
    """Final event — all sentences done, slots extracted."""
    lead_text: str
    lead_lang: str
    lead_confidence: float
    priya_full_text: str
    language_transition: Transition
    slots: QualificationSlots
    latency_ms: dict[str, int]
    total_sentences: int
    cache_hits: int


StreamEvent = AudioChunkEvent | TurnCompleteEvent


@dataclass
class StreamingDependencies:
    stt: STTAdapter
    tts: TTSAdapter
    llm: StreamingLLMAdapter
    r2_reader: R2Reader
    r2_writer: R2Writer
    voice_id: str = PINNED_VOICE_ID


# -- Sentence splitting ----------------------------------------------------

_SENTENCE_BOUNDARY = re.compile(r'(?<=[।.?!])\s+')

def split_sentences(text: str) -> list[str]:
    parts = _SENTENCE_BOUNDARY.split(text.strip())
    return [p.strip() for p in parts if p.strip()]


# -- Main streaming entry point ---------------------------------------------

async def run_turn_streaming(
    *,
    ctx,  # CallContext
    audio_in: bytes,
    deps: StreamingDependencies,
    prior_slots: QualificationSlots,
) -> AsyncIterator[StreamEvent]:
    """Stream audio chunks as LLM generates sentences.

    Yields AudioChunkEvent per sentence, then one final TurnCompleteEvent.
    The Exotel WS handler plays each AudioChunkEvent immediately.
    """
    timings: dict[str, int] = {}
    t0 = time.monotonic()

    # ---- 1. STT -----------------------------------
    stt_t0 = time.monotonic()
    stt_result = await deps.stt.transcribe(audio_in)
    timings["stt_ms"] = int((time.monotonic() - stt_t0) * 1000)

    raw_transcript = (stt_result.transcript or "").strip()

    is_echo = False
    if raw_transcript and ctx.conversation_state.recent_priya_turns:
        for prev in ctx.conversation_state.recent_priya_turns[-3:]:
            overlap = _text_overlap(raw_transcript, prev)
            if overlap > 0.4:
                is_echo = True
                break

    if not raw_transcript or is_echo:
        stt_result = _STTResult(
            transcript="(silence)",
            language_code=stt_result.language_code or "hi-IN",
            confidence=0.0,
            request_id=getattr(stt_result, 'request_id', ''),
        )

    # ---- 2. Language + Phase -------------------------------------------
    lang = _coerce_lang(stt_result.language_code)
    transition = ctx.language_state.update(
        STTUtterance(
            text=stt_result.transcript,
            lang=lang,
            confidence=stt_result.confidence,
        )
    )
    ctx.conversation_state.advance_phase_if_due(
        elapsed_sec=ctx.elapsed(),
        buying_confidence=prior_slots.buying_confidence,
    )

    # ---- 3. Bridge audio on language flip (yield first) ----------------
    if transition.switched and transition.bridge_phrase:
        bridge = await load_or_synthesize_phrase(
            text=transition.bridge_phrase,
            lang=transition.current_language.value,
            r2_reader=deps.r2_reader,
            r2_writer=deps.r2_writer,
            synthesize=lambda t, l: deps.tts.synth(t, l),
            voice_id=deps.voice_id,
        )
        yield AudioChunkEvent(
            audio=bridge.audio, text=transition.bridge_phrase,
            sentence_idx=-1, used_cache=bridge.used_cache,
        )

    # ---- 4. Build system message (same as sequential) ------------------
    base_prompt = _cached_prompt()
    system_msg = build_system_message(
        base_prompt=base_prompt,
        current_language=transition.current_language.value,
        lead_first_name=ctx.lead_first_name,
        lead_company=ctx.lead_company,
    )
    system_msg += "\n\n" + system_prompt_addendum(ctx.conversation_state)

    pain = _pain_hypothesis_for_turn(ctx, prior_slots, transition.current_language.value)
    if pain:
        system_msg += f"\n\n<pain_hypothesis>{pain}</pain_hypothesis>"

    context_summary = _build_context_summary(prior_slots, ctx)
    if context_summary:
        system_msg += f"\n\n<call_context>{context_summary}</call_context>"

    user_msg = _format_user_message(
        stt_result.transcript, prior_slots, ctx.conversation_state
    )

    # ---- 5. Start streaming LLM + slot extraction in parallel ----------
    llm_t0 = time.monotonic()
    slots_task = asyncio.create_task(
        extract_slots(
            transcript=[
                {"speaker": "lead", "text": stt_result.transcript},
                *_recent_priya_turns_as_transcript(ctx.conversation_state),
            ],
            prior_slots=prior_slots,
            llm=deps.llm.extract,
        )
    )

    # Accumulate LLM tokens, split by sentence, TTS + yield each sentence
    sentence_buffer = ""
    full_text_parts: list[str] = []
    sentence_idx = 0
    cache_hits = 0
    first_sentence_done = False

    async for chunk in deps.llm.stream_respond(system_msg, user_msg):
        sentence_buffer += chunk

        # Check for sentence boundary
        sentences = split_sentences(sentence_buffer)
        if len(sentences) > 1:
            # All but last are complete sentences → TTS + yield
            for complete_sentence in sentences[:-1]:
                if not first_sentence_done:
                    timings["llm_first_sentence_ms"] = int(
                        (time.monotonic() - llm_t0) * 1000
                    )
                    first_sentence_done = True

                tts_t0 = time.monotonic()
                phrase_result = await load_or_synthesize_phrase(
                    text=complete_sentence,
                    lang=transition.current_language.value,
                    r2_reader=deps.r2_reader,
                    r2_writer=deps.r2_writer,
                    synthesize=lambda t, l: deps.tts.synth(t, l),
                    voice_id=deps.voice_id,
                )
                if sentence_idx == 0:
                    timings["tts_first_sentence_ms"] = int(
                        (time.monotonic() - tts_t0) * 1000
                    )
                if phrase_result.used_cache:
                    cache_hits += 1

                full_text_parts.append(complete_sentence)
                yield AudioChunkEvent(
                    audio=phrase_result.audio,
                    text=complete_sentence,
                    sentence_idx=sentence_idx,
                    used_cache=phrase_result.used_cache,
                )
                sentence_idx += 1

            sentence_buffer = sentences[-1]  # keep incomplete tail

    # Flush remaining buffer
    if sentence_buffer.strip():
        if not first_sentence_done:
            timings["llm_first_sentence_ms"] = int(
                (time.monotonic() - llm_t0) * 1000
            )
        tts_t0 = time.monotonic()
        phrase_result = await load_or_synthesize_phrase(
            text=sentence_buffer.strip(),
            lang=transition.current_language.value,
            r2_reader=deps.r2_reader,
            r2_writer=deps.r2_writer,
            synthesize=lambda t, l: deps.tts.synth(t, l),
            voice_id=deps.voice_id,
        )
        if sentence_idx == 0:
            timings["tts_first_sentence_ms"] = int(
                (time.monotonic() - tts_t0) * 1000
            )
        if phrase_result.used_cache:
            cache_hits += 1

        full_text_parts.append(sentence_buffer.strip())
        yield AudioChunkEvent(
            audio=phrase_result.audio,
            text=sentence_buffer.strip(),
            sentence_idx=sentence_idx,
            used_cache=phrase_result.used_cache,
        )
        sentence_idx += 1

    timings["llm_ms"] = int((time.monotonic() - llm_t0) * 1000)

    # ---- 6. Wait for slot extraction -----------------------------------
    new_slots = await slots_task

    # ---- 7. Update conversation state ----------------------------------
    priya_full = " ".join(full_text_parts)
    ctx.conversation_state.record_priya_turn(priya_full)
    ctx.phrase_cache_hits += cache_hits
    ctx.turn_idx += 1

    timings["total_ms"] = int((time.monotonic() - t0) * 1000)

    yield TurnCompleteEvent(
        lead_text=stt_result.transcript,
        lead_lang=transition.current_language.value,
        lead_confidence=stt_result.confidence,
        priya_full_text=priya_full,
        language_transition=transition,
        slots=new_slots,
        latency_ms=timings,
        total_sentences=sentence_idx,
        cache_hits=cache_hits,
    )


# -- Helpers (same as turn_orchestrator) ------------------------------------

_PROMPT_CACHE: str = ""

def _cached_prompt() -> str:
    global _PROMPT_CACHE
    if not _PROMPT_CACHE:
        _PROMPT_CACHE = load_priya_prompt()
    return _PROMPT_CACHE


def _text_overlap(a: str, b: str) -> float:
    """Fraction of words in `a` that also appear in `b`. Used for echo detection."""
    if not a or not b:
        return 0.0
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a:
        return 0.0
    return len(words_a & words_b) / len(words_a)


def _build_context_summary(slots, ctx) -> str:
    """Build a running summary of what's known so far. Prevents re-asking."""
    parts = []
    if slots.product_interest:
        parts.append(f"Products discussed: {slots.product_interest}")
    if slots.volume_monthly_kg and slots.volume_monthly_kg > 0:
        parts.append(f"Volume: {slots.volume_monthly_kg} kg/month")
    if slots.current_supplier:
        parts.append(f"Current supplier: {slots.current_supplier}")
    if slots.pain_point:
        parts.append(f"Pain: {slots.pain_point}")
    if slots.decision_role:
        parts.append(f"Role: {slots.decision_role}")
    contact = slots.slot_confidence.get("contact_info")
    if contact:
        parts.append(f"Contact captured: yes")
    if parts:
        parts.append("DO NOT re-ask anything already captured above.")
    return " | ".join(parts) if parts else ""


def _coerce_lang(code: str) -> Lang | None:
    try:
        return Lang(code)
    except ValueError:
        return None


def _pain_hypothesis_for_turn(ctx, slots, lang: str) -> Optional[str]:
    if ctx.conversation_state.phase != Phase.DISCOVER:
        return None
    try:
        lang_enum = Lang(lang)
    except ValueError:
        return None
    return pick_pain_hypothesis(
        product_interest=slots.product_interest,
        lang=lang_enum,
        turn_idx=ctx.turn_idx,
    )


def _format_user_message(lead_text, slots, conv):
    turn = len(conv.recent_priya_turns)
    is_silence = "silence" in lead_text.lower() or not lead_text.strip()

    parts = []

    if turn == 0:
        parts.append('[Turn 1. You ALREADY said namaste and introduced yourself. DO NOT introduce yourself again. Just respond to what the lead said.]')
    else:
        parts.append(f'[Turn {turn + 1}. NO greeting. NO intro.]')

    if is_silence:
        if turn < 2:
            parts.append('Lead silent. Say: "Sir, awaaz nahi aa rahi. Sun pa rahe hain?"')
        else:
            parts.append('Lead silent. Say: "Sir, connection weak hai. Kal call karun?"')
    else:
        parts.append(f'Lead: "{lead_text}"')
        if slots.product_interest:
            parts.append(f"Known: {slots.product_interest}")

    if slots.buying_confidence >= 0.7:
        parts.append("HIGH signal. Close: quote/WhatsApp.")
    elif slots.buying_confidence >= 0.4:
        parts.append("Medium. Build value.")

    if conv.consecutive_close_attempts >= 2:
        parts.append("Lead rejected twice. Say goodbye and END.")

    parts.append('BANNED WORDS: आवश्यकता, उत्पाद, सहायता, कृपया. Use English instead: requirement, products, help, please.')
    parts.append("Respond naturally. Not a prospect = exit gracefully.")
    return "\n".join(parts)


def _recent_priya_turns_as_transcript(conv):
    return [{"speaker": "priya", "text": t} for t in conv.recent_priya_turns[-4:]]
