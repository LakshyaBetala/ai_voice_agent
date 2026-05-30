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
    end_call: bool = False  # True → WS handler hangs up after audio finishes
    lead_intent: str = "normal"  # classify_lead_intent result, for call logs


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

    # ---- 3. Language flip — no bridge phrase, just switch silently ------

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

    intent = classify_lead_intent(stt_result.transcript, ctx.conversation_state)
    if intent == "reject":
        ctx.conversation_state.reject_count += 1
    elif intent == "offtopic":
        ctx.conversation_state.off_topic_count += 1
    elif intent == "backchannel":
        ctx.conversation_state.backchannel_count += 1
    elif intent == "normal":
        ctx.conversation_state.backchannel_count = 0
    end_call = should_end_call(intent, ctx.conversation_state)

    user_msg = _format_user_message(
        stt_result.transcript, prior_slots, ctx.conversation_state,
        lang=transition.current_language.value, intent=intent,
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
        end_call=end_call,
        lead_intent=intent,
    )


# -- Helpers (same as turn_orchestrator) ------------------------------------

_PROMPT_CACHE: str = ""

def _cached_prompt() -> str:
    global _PROMPT_CACHE
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


_CLOSE_WORDS = [
    "bhej do", "bhej de", "bhej dena", "bhej dijiye",
    "send karo", "send kar do", "send kar dena",
    "whatsapp karo", "whatsapp pe bhej", "whatsapp bhej",
    "quote bhejo", "quote bhej do",
    "anuppunga", "anuppu", "send pannunga",
    "theek hai", "okay done", "kar dijiye",
    "bye", "thank you", "thanks", "bye bye",
    "dekh leta", "dekh lunga", "dekhta hoon", "dekhti hoon",
]
_REJECT_WORDS = [
    "not interested", "interested nahi", "nahi chahiye", "nahin chahiye",
    "zaroorat nahi", "zarurat nahi", "zaroorat nahin", "mat karo",
    "band karo", "rehne do", "interest nahi", "call mat", "pareshan mat",
    "venam", "thevai illa",  # Tamil: don't want / not needed
]
# Clearly wrong person / wrong number → end politely, no probe.
_WRONG_WORDS = [
    "galat number", "wrong number", "kaun bol", "kaun hai", "personal call",
]
# Off-topic / not a business prospect → probe ONCE for a real requirement,
# then end. ("can't sell chemicals to a tiger" — but try first.)
_OFFTOPIC_WORDS = [
    "student", "padhta", "padhai", "college", "school",
    "pizza", "khana", "biryani", "time pass", "timepass", "bored",
]
_ABUSE_WORDS = [
    "chutiya", "bhosdi", "madarchod", "behenchod", "gaand", "lavda",
    "randi", "harami", "kutte", "saala kutta", "fuck", "bastard",
]
# Lead didn't catch what Priya said — she should REPHRASE, not parrot.
# Covers Hindi, Tamil, English, and code-mix re-ask phrases. Checked BEFORE
# close/normal so a clarification request never gets read as agreement.
_CLARIFY_WORDS = [
    # English
    "didn't get", "didnt get", "did not get", "couldn't hear", "couldnt hear",
    "could not hear", "say again", "come again", "what was that", "what did you say",
    "pardon", "repeat please", "please repeat", "one more time", "again sir",
    "sorry sir", "sorry didn't", "sorry didnt",
    # Hindi
    "phir se", "phir bolo", "phir boliye", "dobara", "dubara", "kya bola",
    "kya kaha", "kya kaha sir", "samajh nahi", "samjha nahi", "samjhi nahi",
    "nahi suna", "nahi sunai", "suna nahi", "sunai nahi",
    "thoda dheere", "dheere boliye", "aaram se boliye",
    # Tamil / Tanglish
    "enna sonninga", "enna sonneenga", "enna sonneenge", "enna sonnel",
    "puriyala", "puriyalai", "puriyale", "kekkala", "kekkalai",
    "thirumba sollunga", "thirumba sollu", "innum oru thadava",
    "konjam meadhu", "meadhu sollunga", "slow ah sollunga",
]


# Pure acknowledgment tokens — the lead is listening, not answering.
_BACKCHANNEL_TOKENS: frozenset[str] = frozenset({
    "acha", "achha", "accha", "achchha", "haan", "han", "haa", "hmm", "hm",
    "mm", "mmm", "ok", "okay", "okk", "theek", "thik", "sahi", "right",
    "ji", "sari", "seri", "aama", "yes", "yeah", "yep", "bilkul", "sun",
    "suno", "hmmm", "achaa",
})
# Harmless connectors allowed alongside an ack without changing the meaning
# ("theek hai", "haan ji", "haan boliye", "ok sir").
_BACKCHANNEL_CONNECTORS: frozenset[str] = frozenset({
    "hai", "ji", "haan", "na", "to", "sir", "madam", "boliye", "bolo",
    "kahiye", "batao", "bataiye",
})


def _is_backchannel(text: str) -> bool:
    """True when a SHORT utterance is only acknowledgment ("acha", "haan haan",
    "theek hai", "ok ji") — the lead is passively listening, not answering and
    not asking to close. Anything with real content (e.g. "theek hai bhej do")
    is NOT a backchannel."""
    cleaned = re.sub(r"[^\w\s]", " ", text.lower()).strip()
    words = cleaned.split()
    if not words or len(words) > 4:
        return False
    if not all(w in _BACKCHANNEL_TOKENS or w in _BACKCHANNEL_CONNECTORS for w in words):
        return False
    return any(w in _BACKCHANNEL_TOKENS for w in words)


def classify_lead_intent(lead_text: str, conv) -> str:
    """Coarse intent for end-of-call decisions. One of:
    silence | backchannel | close | reject | wrong | abuse | offtopic | normal.
    """
    t = lead_text.lower().strip()
    if not t or "silence" in t:
        return "silence"
    if any(w in t for w in _ABUSE_WORDS):
        return "abuse"
    # Check backchannel BEFORE close: a lone "theek hai"/"ok"/"acha" is the lead
    # listening, NOT asking to end the call. (Bug fix: these used to hang up.)
    if _is_backchannel(t):
        return "backchannel"
    # Check clarify BEFORE close: "thoda dheere boliye" contains "boliye" which
    # otherwise reads like a quote-send cue; same for re-ask phrases that touch
    # close keywords by accident.
    if any(w in t for w in _CLARIFY_WORDS):
        return "clarify"
    if any(w in t for w in _CLOSE_WORDS):
        return "close"
    if any(w in t for w in _OFFTOPIC_WORDS):
        return "offtopic"
    if any(w in t for w in _REJECT_WORDS):
        return "reject"
    if any(w in t for w in _WRONG_WORDS):
        return "wrong"
    return "normal"


def should_end_call(intent: str, conv) -> bool:
    """Decide whether Priya hangs up after this turn.

    We give the lead a chance before ending: a rejection first gets a
    referral ask, an off-topic turn first gets one requirement probe.
    """
    if intent in ("close", "abuse", "wrong"):
        return True
    if intent == "reject" and conv.reject_count >= 2:
        return True
    if intent == "offtopic" and conv.off_topic_count >= 2:
        return True
    if conv.should_force_end():
        return True
    return False


def _format_user_message(lead_text, slots, conv, *, lang: str = "hi-IN", intent: str = "normal"):
    turn = len(conv.recent_priya_turns)
    is_silence = intent == "silence"

    parts = ['[ROMAN SCRIPT ONLY. No Devanagari. No Tamil script.]']

    if turn == 0:
        parts.append('[Intro DONE. Do not introduce yourself.]')

    last_priya = conv.recent_priya_turns[-1] if conv.recent_priya_turns else ""
    if lang == "ta-IN":
        # Strong pin: examples in the system prompt skew Hindi, so the LLM
        # tends to drift back. Anchor the LLM to its last Tamil turn and
        # explicitly forbid Hindi-only words.
        parts.append(
            '[TANGLISH ONLY. Tamil grammar + English business words. '
            'ZERO Hindi words — no "achha", "bilkul", "haan ji", "theek hai", '
            '"karenge", "kijiye", "dijiye", "boliye", "bataiye". '
            'Use sarr/sariya/iruku/thaaren/panren/paesalaam/paathaachu. '
            'Open ONLY with "Vanakkam sarr" or "Hello sarr" — never "Vaango sarr" '
            'on an outbound call (vaango = "welcome in"; wrong context). '
            'You spoke Tamil last turn — stay in Tamil unless lead asks otherwise.]'
        )
        if last_priya:
            parts.append(f'[Your last reply (Tamil): "{last_priya}"]')
    elif lang == "en-IN":
        parts.append('[ENGLISH. Indian-cadence English only — no Hindi, no Tamil.]')
        if last_priya:
            parts.append(f'[Your last reply (English): "{last_priya}"]')
    else:
        parts.append(
            '[HINGLISH. Hindi grammar + English business words. '
            'No Tamil grammar (no "irukku", "tharen", "panren", "sariya").]'
        )
        if last_priya:
            parts.append(f'[Your last reply (Hindi): "{last_priya}"]')

    if is_silence:
        parts.append('Lead silent. Ask once gently: "Sir, sun pa rahe hain?"')
        return "\n".join(parts)

    parts.append(f'Lead: "{lead_text}"')

    if intent == "backchannel":
        if conv.backchannel_count >= 2:
            parts.append(
                'Lead is just listening passively (only said "' + lead_text.strip()
                + '"), not answering. STOP explaining. Ask ONE short, direct question to '
                'pull them in — e.g. abhi kaunsa chemical use karte hain, ya monthly kitna '
                'volume lagta hai. Do NOT repeat your previous question.'
            )
        else:
            parts.append(
                'Lead is acknowledging (listening), NOT answering and NOT closing. '
                'Do NOT repeat your last question. Move FORWARD: add ONE new useful point '
                'or ask the NEXT short question.'
            )
        return "\n".join(parts)

    if intent == "clarify":
        last_priya = conv.recent_priya_turns[-1] if conv.recent_priya_turns else ""
        parts.append(
            f'Lead did NOT catch you (said "{lead_text.strip()}"). '
            f'Your last line was: "{last_priya}". '
            'REPHRASE that idea — DO NOT repeat it verbatim. '
            'Use simpler/shorter words, add a "..." pause, spell tricky terms '
            'phonetically (kemicals, eth-a-naal, kot, price-uh, raate-uh). '
            'Drop one detail if packed. Stay in the SAME language the lead used. '
            'One short sentence, then a question if natural. NEVER copy your '
            'previous sentence word-for-word.'
        )
        return "\n".join(parts)

    if intent == "close":
        parts.append('Lead wants to CLOSE. Say ONLY: "Bilkul sir, isi number pe WhatsApp pe quote aa jayega. Thank you!" Then STOP.')
    elif intent == "reject":
        if conv.reject_count >= 2:
            parts.append('Still no. Warm exit: "Koi baat nahi sir, zarurat ho to SPC yaad rakhiyega. Good day!" Then STOP.')
        else:
            parts.append('Lead not interested. Don\'t push the sale — ask for a REFERRAL once: "Koi baat nahi sir. Aapke kisi jaan-pehchaan ko chemicals supply chahiye to bata dijiye, hum acchi service denge?"')
    elif intent == "abuse":
        parts.append('Lead abusive. Say ONLY: "Theek hai sir, good day." Nothing else.')
    elif intent == "wrong":
        parts.append('Wrong person / off-topic. Say ONLY: "Sorry sir, aapka time liya. Good day!" Then STOP.')
    elif intent == "offtopic":
        if conv.off_topic_count >= 2:
            parts.append('Still off-topic. End: "Koi baat nahi sir, good day!" STOP.')
        else:
            parts.append('Lead off-topic. Probe ONCE: "Sir, koi chemicals ya industrial supply ki requirement hai aapke business mein?"')
    else:
        if slots.product_interest:
            parts.append(f"Known: {slots.product_interest}")
        if slots.buying_confidence >= 0.7:
            parts.append("High interest → push for close.")
        elif conv.consecutive_close_attempts >= 2:
            parts.append("Two rejections → goodbye.")

    return "\n".join(parts)


def _recent_priya_turns_as_transcript(conv):
    return [{"speaker": "priya", "text": t} for t in conv.recent_priya_turns[-4:]]
