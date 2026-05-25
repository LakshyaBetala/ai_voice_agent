"""Conversation phase machine + anti-AI sound enforcement.

Why this exists
---------------
Latency and language are necessary but not sufficient. The "this is a robot"
moment comes from three behaviors that humans don't do:

  1. Repeating the same acknowledgment ("Got it. Got it. Got it.").
  2. Zero filler words across the whole call (no "ji", "haan", "achha").
  3. Paraphrasing yourself (Priya saying the same idea two turns apart).

This module encodes those as hard state. The system prompt receives the
current state every turn and the LLM is instructed to avoid the recorded
patterns. A post-turn audit catches anything that slipped through.

Phases drive a second behavior: Priya doesn't ask qualifying questions
during the CONNECT phase (it's rapport time), and doesn't pitch during
DISCOVER (it's listening time). The phase machine advances on time +
extracted-slot count + buying_confidence; never goes backwards.

EXTENSION phase is the dual-billing seam: at 170s, if buying_confidence
is high, Priya gets another 180s of runway (call bills as 2 units).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Phase(str, Enum):
    GREETING = "greeting"
    CONNECT = "connect"
    DISCOVER = "discover"
    QUALIFY = "qualify"
    CLOSE = "close"
    EXTENSION = "extension"


# Phase transition thresholds (seconds elapsed).
# Aligned with credit boundaries: 150s (1 credit), 300s (2 credits), 450s (3 credits).
GREETING_END_SEC = 8.0
CONNECT_END_SEC = 35.0
DISCOVER_END_SEC = 70.0
QUALIFY_END_SEC = 130.0   # Qualify before 1st credit boundary
CLOSE_END_SEC = 140.0     # Soft close at 140s → hard boundary at 150s
EXTENSION_END_SEC = 290.0  # Extension before 2nd credit boundary

# Minimum buying_confidence at 170s soft-close to enter EXTENSION instead
# of wrapping up. Below this, we go to CLOSE and end gracefully.
EXTENSION_CONFIDENCE_FLOOR = 0.6

# Anti-repetition rolling window: last N Priya turns kept verbatim in
# context with rule "do not paraphrase your own recent turns".
RECENT_TURNS_WINDOW = 4

# Minimum fillers per N turns. If Priya goes 3 turns without "ji/haan/
# achha/right/okay", the audit flags it.
MIN_FILLERS_PER_WINDOW = 1
FILLER_AUDIT_WINDOW = 3

# Fillers that count for the audit. Multilingual on purpose — Priya uses
# Hindi fillers in English turns and vice versa (human bilinguals do this).
FILLERS: frozenset[str] = frozenset({
    "ji", "haan", "achha", "acha", "theek", "sahi", "bilkul",
    "right", "okay", "ok", "sure", "got it", "i see", "mhm", "hmm",
    "sari", "aama", "seri",
})


@dataclass
class ConversationState:
    """Per-call conversation state. One instance lives for the call."""

    phase: Phase = Phase.GREETING

    # Acknowledgments Priya has already used this call. The LLM is told to
    # avoid these and pick a different one.
    used_acknowledgments: set[str] = field(default_factory=set)

    # Recent Priya turns (verbatim) for self-repetition prevention.
    recent_priya_turns: list[str] = field(default_factory=list)

    # Rolling window of "did the last K turns contain at least one filler?"
    # Used to nudge the LLM if Priya is sounding too formal.
    filler_window: list[bool] = field(default_factory=list)

    # Tracking for the soft-close: number of consecutive turns Priya has
    # tried to close vs the lead extended the conversation. Prevents the
    # robot loop of "alright, anything else?" / "no" / "alright, anything else?"
    consecutive_close_attempts: int = 0

    # Phase entry timestamps for telemetry + debugging.
    phase_entered_at: dict[Phase, float] = field(default_factory=dict)

    def advance_phase_if_due(
        self,
        *,
        elapsed_sec: float,
        buying_confidence: float,
    ) -> Phase:
        """Compute the phase Priya should be in right now. Monotonic — never goes back.

        - <8s: GREETING
        - 8-35s: CONNECT (rapport, no product questions)
        - 35-70s: DISCOVER (pain hypothesis floated)
        - 70-150s: QUALIFY (slot-filling questions interleaved with value statements)
        - 150-170s: CLOSE (commit-question based on score)
        - 170-350s: EXTENSION (only if buying_confidence >= 0.6 at 170s)
        - >350s: CLOSE again (final wrap), 360s = hard stop in pipeline.py
        """
        if elapsed_sec < GREETING_END_SEC:
            target = Phase.GREETING
        elif elapsed_sec < CONNECT_END_SEC:
            target = Phase.CONNECT
        elif elapsed_sec < DISCOVER_END_SEC:
            target = Phase.DISCOVER
        elif elapsed_sec < QUALIFY_END_SEC:
            target = Phase.QUALIFY
        elif elapsed_sec < CLOSE_END_SEC:
            target = Phase.CLOSE
        elif elapsed_sec < EXTENSION_END_SEC:
            # 170-350s: extend only if real buying signal present.
            if (
                self.phase == Phase.EXTENSION
                or buying_confidence >= EXTENSION_CONFIDENCE_FLOOR
            ):
                target = Phase.EXTENSION
            else:
                target = Phase.CLOSE
        else:
            target = Phase.CLOSE

        # Monotonic advance: never go back to an earlier phase. The one
        # exception is GREETING → anything (initial transition).
        if _phase_rank(target) > _phase_rank(self.phase):
            self.phase = target
            self.phase_entered_at.setdefault(target, elapsed_sec)
        return self.phase

    def record_priya_turn(self, text: str) -> None:
        """Called after Priya speaks. Updates ack tracker + recent buffer + filler audit."""
        ack = _extract_leading_ack(text)
        if ack:
            self.used_acknowledgments.add(ack)

        self.recent_priya_turns.append(text)
        if len(self.recent_priya_turns) > RECENT_TURNS_WINDOW:
            self.recent_priya_turns = self.recent_priya_turns[-RECENT_TURNS_WINDOW:]

        self.filler_window.append(_contains_filler(text))
        if len(self.filler_window) > FILLER_AUDIT_WINDOW:
            self.filler_window = self.filler_window[-FILLER_AUDIT_WINDOW:]

    def filler_audit_failing(self) -> bool:
        """True when the last N Priya turns had fewer than required fillers.

        When True, the next system prompt nudges: "Add a natural filler word
        like 'ji', 'haan', 'achha' to your next response."
        """
        if len(self.filler_window) < FILLER_AUDIT_WINDOW:
            return False
        return sum(self.filler_window) < MIN_FILLERS_PER_WINDOW

    def note_close_attempt(self, lead_extended: bool) -> None:
        """Called in CLOSE/EXTENSION when Priya attempts a wrap-up.

        If the lead keeps talking (lead_extended=True), reset the counter.
        If Priya tries to close 3 turns in a row and the lead is silent,
        we force-end to avoid the robot loop.
        """
        if lead_extended:
            self.consecutive_close_attempts = 0
        else:
            self.consecutive_close_attempts += 1

    def should_force_end(self) -> bool:
        """Stop the robot loop of 'anything else?' / 'no' / 'anything else?'"""
        return self.consecutive_close_attempts >= 3


def _phase_rank(p: Phase) -> int:
    return {
        Phase.GREETING: 0,
        Phase.CONNECT: 1,
        Phase.DISCOVER: 2,
        Phase.QUALIFY: 3,
        Phase.CLOSE: 4,
        Phase.EXTENSION: 5,
    }[p]


_ACK_PATTERNS: tuple[str, ...] = (
    "got it", "understood", "makes sense", "i see", "i understand",
    "achha", "acha", "theek hai", "sahi", "bilkul", "haan ji",
    "sari", "aama", "puriyudhu",
    "right", "okay", "sure", "alright",
)


def _extract_leading_ack(text: str) -> str | None:
    """Pull the leading acknowledgment from a Priya turn, normalized.

    "Got it. So you handle 500kg per month?" → "got it"
    "Achha, and what about delivery times?" → "achha"
    """
    lower = text.lower().strip()
    for ack in _ACK_PATTERNS:
        if lower.startswith(ack):
            return ack
    return None


def _contains_filler(text: str) -> bool:
    """True if the Priya turn contains at least one filler word/phrase."""
    lower = " " + text.lower() + " "
    for f in FILLERS:
        # Pad with spaces to avoid matching "okay" inside "okayed".
        if f" {f} " in lower or f" {f}." in lower or f" {f}," in lower:
            return True
    return False


def system_prompt_addendum(state: ConversationState) -> str:
    """Per-turn dynamic addendum injected into Priya's system prompt.

    This is the bridge between the state machine and the LLM. The base
    priya-system.md describes "be human-like"; this addendum tells the
    LLM the *specific current constraints* that change every turn.
    """
    parts: list[str] = []

    parts.append(
        "<response_format>\n"
        "CRITICAL — Reply in ONE short sentence, max 15 words.\n"
        "Lead with a filler ('haan ji', 'achha', 'right', 'sari').\n"
        "Never ask two questions. Never list products. Never explain offerings unprompted.\n"
        "</response_format>"
    )

    # SPC product catalog — Priya KNOWS these. Never defer to a specialist
    # for any chemical listed here. Confirm confidently: "Haan ji, toluene
    # hum supply karte hain." Then immediately ask about volume or timeline.
    parts.append(
        "<spc_catalog>\n"
        "YOU CONFIDENTLY SUPPLY ALL OF THESE (250+ products). Confirm instantly, ask volume:\n"
        "Acids: acetic, hydrochloric (HCl), nitric, phosphoric, sulphuric, citric, chromic, oxalic, boric, formic, benzoic\n"
        "Solvents: toluene, xylene, acetone, MEK, benzene, styrene, cyclohexane, DMF, ethyl acetate\n"
        "Alcohols: IPA, methanol, ethanol, butanol, benzyl alcohol, glycerine, MEA, TEA\n"
        "Glycols: ethylene glycol, propylene glycol, diethylene glycol, PEG 200-4000\n"
        "Plasticizers: DOP, DBP, DOTP, chlorinated paraffin wax, methylene chloride\n"
        "Powders: caustic soda, soda ash, TiO2, melamine, EDTA, bleaching powder, borax, zinc oxide\n"
        "Specialty: surfactants, paraffin wax, ammonia liquor, MTO, silicone emulsion, formaldehyde\n"
        "When lead mentions ANY of these → confirm: 'Haan ji, [product] hum supply karte hain.' → ask volume.\n"
        "</spc_catalog>"
    )

    # Conversion aggression — Priya LEADS, never passively waits.
    parts.append(
        "<conversion_rule>\n"
        "YOU lead the conversation. Never wait passively. Every response must either:\n"
        "1. Confirm a product and ask volume ('Haan ji, toluene supply karte hain. Monthly kitna chahiye?')\n"
        "2. Ask a qualifying question ('Aap currently kahan se lete hain?')\n"
        "3. Push toward a close ('Main 4 ghante mein quote bhej deti hoon, email diyiye')\n"
        "Never say 'how can I help you' or 'tell me more'. YOU already know what SPC offers — pitch it.\n"
        "</conversion_rule>"
    )

    parts.append(f"<current_phase>{state.phase.value}</current_phase>")

    if state.used_acknowledgments:
        used = ", ".join(sorted(state.used_acknowledgments))
        parts.append(
            f"<acks_already_used>{used}</acks_already_used>"
            "\nDo not start your next response with any of these. Pick a different "
            "acknowledgment or skip it entirely."
        )

    if state.recent_priya_turns:
        joined = "\n---\n".join(state.recent_priya_turns)
        parts.append(
            f"<your_recent_turns>\n{joined}\n</your_recent_turns>"
            "\nDo not paraphrase any of these. Move the conversation forward."
        )

    if state.filler_audit_failing():
        parts.append(
            "<style_nudge>Your last few responses have been too formal. "
            "Include one natural filler ('ji', 'haan', 'achha', 'right', 'okay') "
            "in your next response.</style_nudge>"
        )

    if state.phase == Phase.CONNECT:
        parts.append(
            "<phase_directive>You are in rapport-building mode. Ask one open "
            "question about their business. Do NOT ask about budget, volume, "
            "or timeline yet. Listen and reflect back.</phase_directive>"
        )
    elif state.phase == Phase.DISCOVER:
        parts.append(
            "<phase_directive>Float a pain hypothesis matching their business. "
            "Use the pain_library entry passed in <pain_hypothesis>. Make it a "
            "soft guess, not an interrogation.</phase_directive>"
        )
    elif state.phase == Phase.QUALIFY:
        parts.append(
            "<phase_directive>Ask qualifying questions but interleave with "
            "value statements (credit terms, delivery promise, quality cert). "
            "Maximum 2 questions in a row.</phase_directive>"
        )
    elif state.phase == Phase.CLOSE:
        parts.append(
            "<phase_directive>Ask one commit question based on current "
            "buying_confidence (Hot=quote, Warm=sample, Cold=polite future "
            "contact). Then wrap up.</phase_directive>"
        )
    elif state.phase == Phase.EXTENSION:
        parts.append(
            "<phase_directive>The lead is engaged and buying confidence is "
            "high. You have ~3 more minutes. Stay in qualification + "
            "commit-question mode. This call now bills as 2 units.</phase_directive>"
        )

    return "\n\n".join(parts)
