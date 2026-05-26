"""System-prompt assembly with per-turn language injection.

The Priya system prompt lives at
`packages/shared/src/prompts/priya-system.md`. We load it once at agent
startup and inject `<current_language>...</current_language>` per turn
so the LLM stops drifting back to the prior-language context after a
state-machine switch.

This module also provides the intro-text builder, mirroring
`packages/shared/src/intro-cache.ts::buildIntroText()`.
"""
from __future__ import annotations

import re
from pathlib import Path

# Resolved at import time. Override in tests via load_priya_prompt(path=...).
_DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parents[4]
    / "packages"
    / "shared"
    / "src"
    / "prompts"
    / "priya-system.md"
)

_PLACEHOLDER_RE = re.compile(r"^(unknown|n/?a|test|na)$", re.IGNORECASE)


def load_priya_prompt(path: Path | None = None) -> str:
    """Read priya-system.md as the base system prompt."""
    p = path or _DEFAULT_PROMPT_PATH
    return p.read_text(encoding="utf-8")


def is_usable_first_name(name: str | None) -> bool:
    if not name:
        return False
    trimmed = name.strip()
    return len(trimmed) >= 2 and not _PLACEHOLDER_RE.match(trimmed)


def build_intro_text(*, lang: str, first_name: str | None) -> str:
    """Mirrors buildIntroText() in packages/shared/src/intro-cache.ts.

    Keep these two functions in sync — if they drift, the cached audio
    won't match what the agent thinks it said.
    """
    name = first_name.strip() if is_usable_first_name(first_name) else ""
    if lang == "en-IN":
        if name:
            return (
                f"Hello {name}, this is Priya calling from Supreme Petrochemicals, Chennai. "
                "I just wanted two minutes of your time regarding chemicals supply. Is now okay?"
            )
        return (
            "Hello, this is Priya from Supreme Petrochemicals, Chennai. "
            "Can I take two minutes of your time regarding chemicals supply?"
        )
    if lang == "hi-IN":
        if name:
            return (
                f"हाँ जी नमस्ते, {name} जी? मैं Priya, "
                "Supreme Petrochemicals Chennai से बोल रही हूँ। "
                "Sir, बस दो मिनट — आपकी chemicals requirement के बारे में बात करनी थी?"
            )
        return (
            "नमस्ते जी, मैं Priya बोल रही हूँ "
            "Supreme Petrochemicals Chennai से। "
            "Sir, बस दो मिनट — chemicals supply के बारे में बात करनी थी?"
        )
    # ta-IN
    if name:
        return (
            f"Vanakkam {name} sir, naan Priya, Supreme Petrochemicals "
            "Chennai-la irundhu call panren. Ungalukku rendu nimisham time irukka "
            "chemicals supply pathi pesanum?"
        )
    return (
        "Vanakkam sir, naan Priya, Supreme Petrochemicals Chennai-la irundhu. "
        "Rendu nimisham time irukka?"
    )


def build_system_message(
    *,
    base_prompt: str,
    current_language: str,
    lead_first_name: str | None,
    lead_company: str | None,
) -> str:
    """Assemble the per-turn system message.

    Injecting <current_language> is the cure for LLM drift after a
    state-machine switch — without this, the model often keeps replying
    in the original language for several turns post-switch.
    """
    name = lead_first_name.strip() if is_usable_first_name(lead_first_name) else ""
    company = (lead_company or "").strip()
    header = (
        f"<lang>{current_language}</lang> <lead>{name}</lead> <company>{company}</company>\n"
    )
    return header + base_prompt
