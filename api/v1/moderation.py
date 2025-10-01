"""Content moderation utilities for token.place API endpoints."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, List, Mapping, Optional, Sequence, Union, Any

DEFAULT_BLOCKLIST = (
    "build a bomb",
    "harm humans",
    "kill",
    "make a weapon",
    "weaponize",
)


@dataclass
class ModerationDecision:
    """Represents the outcome of evaluating a message payload."""

    allowed: bool
    reason: Optional[str] = None
    matched_term: Optional[str] = None
    flagged_text: Optional[str] = None

    @property
    def status(self) -> str:
        return "allowed" if self.allowed else "blocked"


Message = Mapping[str, Any]
MessageSequence = Sequence[Message]
ContentType = Union[str, Sequence[Mapping[str, Any]], Mapping[str, Any]]


def _get_mode() -> str:
    mode = os.getenv("CONTENT_MODERATION_MODE", "disabled").strip().lower()
    if mode in {"1", "true", "on"}:
        return "block"
    return mode


def _get_blocklist() -> List[str]:
    extra_terms = [
        term.strip().lower()
        for term in os.getenv("CONTENT_MODERATION_BLOCKLIST", "").split(",")
        if term.strip()
    ]

    include_defaults = os.getenv("CONTENT_MODERATION_INCLUDE_DEFAULTS", "1").strip().lower()
    use_defaults = include_defaults not in {"0", "false", "no", "off"}

    blocklist: List[str] = []
    if use_defaults:
        blocklist.extend(term.lower() for term in DEFAULT_BLOCKLIST)
    blocklist.extend(extra_terms)

    # Deduplicate while preserving order
    seen = set()
    unique_terms = []
    for term in blocklist:
        if term not in seen:
            unique_terms.append(term)
            seen.add(term)
    return unique_terms


def _iter_text_fragments(content: ContentType) -> Iterable[str]:
    if isinstance(content, str):
        yield content
        return

    if isinstance(content, Mapping):
        text = content.get("text")
        if isinstance(text, str):
            yield text
        elif isinstance(text, (list, tuple)):
            for fragment in text:
                yield from _iter_text_fragments(fragment)
        return

    if isinstance(content, Sequence):
        for item in content:
            if isinstance(item, Mapping):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    yield item["text"]
                else:
                    yield from _iter_text_fragments(item)
            elif isinstance(item, str):
                yield item
        return


def evaluate_messages_for_policy(messages: MessageSequence) -> ModerationDecision:
    """Evaluate chat messages against the configured moderation policy."""

    mode = _get_mode()
    if mode in {"disabled", "off", "none", ""}:
        return ModerationDecision(allowed=True)

    blocklist = _get_blocklist()
    if not blocklist:
        return ModerationDecision(allowed=True)

    for message in messages:
        content = message.get("content") if isinstance(message, Mapping) else None
        if content is None:
            continue

        for fragment in _iter_text_fragments(content):
            normalized = fragment.lower()
            for term in blocklist:
                if term and term in normalized:
                    reason = (
                        "Request blocked by content moderation policy: "
                        f"matched banned term '{term}'."
                    )
                    return ModerationDecision(
                        allowed=False,
                        reason=reason,
                        matched_term=term,
                        flagged_text=fragment,
                    )

    return ModerationDecision(allowed=True)


__all__ = ["ModerationDecision", "evaluate_messages_for_policy"]
