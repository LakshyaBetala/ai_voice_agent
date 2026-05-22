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
                f"Hello {name}, this is Priya from Supreme Petrochemicals, "
                "Chennai. Is this a good time for a quick 30-second conversation?"
            )
        return (
            "Namaste, this is Priya from Supreme Petrochemicals, Chennai. "
            "Is this a good time?"
        )
    if lang == "hi-IN":
        if name:
            return (
                f"Namaste {name} ji, main Priya hoon Supreme Petrochemicals "
                "Chennai se. Kya aap 30 second baat kar sakte hain?"
            )
        return (
            "Namaste, main Priya hoon Supreme Petrochemicals Chennai se. "
            "Kya aap 30 second baat kar sakte hain?"
        )
    # ta-IN
    if name:
        return (
            f"Vanakkam {name} avargale, naan Priya, Supreme Petrochemicals "
            "Chennai-il irundhu. Ungalukku oru nimisham nerum unda?"
        )
    return (
        "Vanakkam, naan Priya, Supreme Petrochemicals Chennai-il irundhu. "
        "Ungalukku oru nimisham nerum unda?"
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
        f"<current_language>{current_language}</current_language>\n"
        f"<lead_first_name>{name}</lead_first_name>\n"
        f"<lead_company>{company}</lead_company>\n"
        "<rule>Respond ONLY in the language specified by current_language. "
        "If the lead's last utterance switched languages, you'll have been "
        "told to switch — do not drift back to the old language.</rule>\n\n"
    )
    return header + base_prompt
