import pytest

from api.v1 import moderation


def test_get_mode_interprets_truthy_values(monkeypatch):
    monkeypatch.setenv("CONTENT_MODERATION_MODE", "  TrUe ")
    assert moderation._get_mode() == "block"


def test_get_mode_returns_normalized_mode(monkeypatch):
    monkeypatch.setenv("CONTENT_MODERATION_MODE", " Audit ")
    assert moderation._get_mode() == "audit"


def test_get_blocklist_includes_defaults_and_extra_terms(monkeypatch):
    monkeypatch.delenv("CONTENT_MODERATION_INCLUDE_DEFAULTS", raising=False)
    monkeypatch.setenv("CONTENT_MODERATION_BLOCKLIST", " Alpha ,  Beta  ")

    blocklist = moderation._get_blocklist()

    assert blocklist[: len(moderation.DEFAULT_BLOCKLIST)] == [
        term.lower() for term in moderation.DEFAULT_BLOCKLIST
    ]
    assert blocklist[-2:] == ["alpha", "beta"]


def test_get_blocklist_deduplicates_and_respects_defaults_flag(monkeypatch):
    monkeypatch.setenv("CONTENT_MODERATION_INCLUDE_DEFAULTS", "0")
    monkeypatch.setenv("CONTENT_MODERATION_BLOCKLIST", " Foo , foo, Bar ")

    blocklist = moderation._get_blocklist()

    assert blocklist == ["foo", "bar"]


@pytest.mark.parametrize(
    "content,expected",
    [
        ("plain text", ["plain text"]),
        ({"text": ["part", {"text": "nested"}]}, ["part", "nested"]),
        (
            [
                "alpha",
                {"type": "text", "text": "beta"},
                {"type": "other", "text": ["gamma", {"text": "delta"}]},
            ],
            ["alpha", "beta", "gamma", "delta"],
        ),
    ],
)
def test_iter_text_fragments_handles_various_structures(content, expected):
    assert list(moderation._iter_text_fragments(content)) == expected


def test_evaluate_messages_allows_when_disabled(monkeypatch):
    monkeypatch.setenv("CONTENT_MODERATION_MODE", "disabled")

    decision = moderation.evaluate_messages_for_policy([
        {"role": "user", "content": "anything"}
    ])

    assert decision.allowed is True
    assert decision.reason is None


def test_evaluate_messages_allows_when_blocklist_empty(monkeypatch):
    monkeypatch.setenv("CONTENT_MODERATION_MODE", "block")
    monkeypatch.setenv("CONTENT_MODERATION_INCLUDE_DEFAULTS", "false")
    monkeypatch.delenv("CONTENT_MODERATION_BLOCKLIST", raising=False)

    decision = moderation.evaluate_messages_for_policy([
        {"role": "user", "content": "still allowed"}
    ])

    assert decision.allowed is True


def test_evaluate_messages_blocks_on_nested_fragment(monkeypatch):
    monkeypatch.setenv("CONTENT_MODERATION_MODE", "block")
    monkeypatch.setenv("CONTENT_MODERATION_INCLUDE_DEFAULTS", "0")
    monkeypatch.setenv("CONTENT_MODERATION_BLOCKLIST", "danger")

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "safe"},
                {"type": "other", "text": ["The danger is here"]},
            ],
        }
    ]

    decision = moderation.evaluate_messages_for_policy(messages)

    assert decision.allowed is False
    assert decision.matched_term == "danger"
    assert decision.flagged_text == "The danger is here"
    assert "danger" in (decision.reason or "").lower()
