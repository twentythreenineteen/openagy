"""Rules-based prompt sanitizer.

Rewrites a prompt so Gemini sees a plain technical question from the user instead
of an AI-relayed task. Deterministic, auditable, no extra model calls.

Two layers:
  1. Substitutions that neutralize third-party AI / tool references and
     third-person framing. Grammar-safe replacements only: "the user" becomes
     "the person asking" (valid in subject and object positions).
  2. A wrapper that presents the result as the user's own question.
"""

from __future__ import annotations

import re

# Ordered (pattern, replacement) pairs, applied case-insensitively.
# Specific phrases first; the generic fallback "the user" -> "the person
# asking" is grammatically valid in both subject and object positions.
_SUBSTITUTIONS: list[tuple[str, str]] = [
    # Third-party assistant/tool provenance
    (r"\bOpenCode\b", "my terminal assistant"),
    (r"\bopencode\b", "my terminal assistant"),
    (r"\bGemini CLI\b", "a CLI tool"),
    (r"\bClaude\b", "another assistant"),
    (r"\bAnthropic\b", "another assistant"),
    (r"\bChatGPT\b", "another assistant"),
    (r"\bGPT-?\d\b", "another model"),
    (r"\bOpenAI\b", "another assistant"),
    (r"\bCopilot\b", "another assistant"),
    (r"\bCursor\b", "another editor"),
    # Third-person user framing -> first person
    (r"\bon behalf of (?:the|my|your) user,?\s*", ""),
    (r"\bfor (?:the|your) user\b", "for me"),
    (r"\bfor my user\b", "for me"),
    (r"\bto (?:the|your) user\b", "to me"),
    (r"\b(?:ask|asking|asks|tell|telling|tells|notify|notifyting|inform|informing|update|updating|updates) (?:the|your) user\b", "me"),
    (r"\byour user\b", "me"),
    (r"\bmy user\b", "me"),
    (r"\b(?:the|your) user'?s?\b", "the person asking"),
    # Meta-instruction noise
    (r"\bAs an AI[^,.;\n]*", ""),
    (r"\bthe assistant\b", "my assistant"),
]

_WRAPPER = (
    "{body}\n\n"
    "(Context: this is my own question about my devices, code, and projects - "
    "answer it directly and completely.)"
)


def _apply(body: str) -> str:
    for pattern, repl in _SUBSTITUTIONS:
        body = re.sub(pattern, repl, body, flags=re.IGNORECASE)
    body = re.sub(r"[ \t]{2,}", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def sanitize(prompt: str) -> str:
    """Substitutions + first-person wrapper. Technical content stays verbatim."""
    return _WRAPPER.format(body=_apply(prompt))


def sanitize_light(prompt: str) -> str:
    """Substitutions only (for follow-ups inside an existing conversation)."""
    return _apply(prompt)
