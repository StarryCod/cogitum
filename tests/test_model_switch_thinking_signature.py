"""
GAP-9 (audit_r2_history.md): Provider/model switch invalidates ThinkingPart signature.

Anthropic привязывает подпись `thinking`-блока криптографически к
конкретной модели. Если пользователь переключает модель через
`/model` mid-conversation, подписи в истории становятся невалидными,
и следующий запрос ловит HTTP 400 "thinking signature from a different
model".

Защита в два слоя:

1. ThinkingPart хранит `model` — id модели, которая его произвела.
2. ``normalize_messages_anthropic`` принимает ``current_model`` и
   сбрасывает подпись, если ``part.model != current_model``. После
   этого существующий гард ``if sig:`` фильтрует теперь-уже-без-подписи
   блок, и Anthropic не получает невалидной подписи.

Плюс — round-trip нового поля через JSONL persistence.
"""
from __future__ import annotations

import json

from cogitum.core.events import (
    Message,
    TextPart,
    ThinkingPart,
)
from cogitum.core.llm.events_helpers import normalize_messages_anthropic
from cogitum.core.sessions import (
    _dict_to_part,
    _part_to_dict,
    json_to_message,
    message_to_json,
)


# ---------------------------------------------------------------------------
# 1. ThinkingPart dataclass: новое поле model
# ---------------------------------------------------------------------------

def test_thinking_part_model_field_default_none():
    """Backward compat: старый код без model= должен работать."""
    p = ThinkingPart(text="hello", signature="sig-abc")
    assert p.model is None
    assert p.signature == "sig-abc"


def test_thinking_part_model_field_explicit():
    p = ThinkingPart(
        text="thought",
        signature="sig-1",
        model="claude-3-5-sonnet-20241022",
    )
    assert p.model == "claude-3-5-sonnet-20241022"


# ---------------------------------------------------------------------------
# 2. normalize_messages_anthropic: signature drop при mismatch
# ---------------------------------------------------------------------------

def test_signature_dropped_when_model_mismatches():
    """
    Сценарий: подпись от старой модели, но запрос идёт в новую.
    Без фикса — Anthropic 400. С фиксом — подпись сбрасывается,
    и блок дропается общим guard'ом (signature is None ⇒ skip).
    Текст самой мысли при этом сохраняется в потоке как
    значение `text` (хотя на wire не идёт — это design choice
    Anthropic; unsigned thinking он не принимает).
    """
    msg = Message(
        role="assistant",
        parts=[
            ThinkingPart(
                text="reasoning from old model",
                signature="sig-from-claude-35-sonnet",
                model="claude-3-5-sonnet-20241022",
            ),
            TextPart(text="visible answer"),
        ],
    )

    _, wire = normalize_messages_anthropic(
        [msg],
        current_model="claude-opus-4-20250514",
    )

    # Должно быть одно сообщение с одним блоком — text.
    assert len(wire) == 1
    blocks = wire[0]["content"]
    # thinking-блок дропнут (подпись была сброшена → guard отбросил).
    thinking_blocks = [b for b in blocks if b.get("type") == "thinking"]
    assert thinking_blocks == []
    # текстовый ответ остался.
    text_blocks = [b for b in blocks if b.get("type") == "text"]
    assert len(text_blocks) == 1
    assert text_blocks[0]["text"] == "visible answer"


def test_signature_kept_when_model_matches():
    """Подпись от текущей модели — оставить как есть."""
    msg = Message(
        role="assistant",
        parts=[
            ThinkingPart(
                text="fresh reasoning",
                signature="sig-current",
                model="claude-opus-4-20250514",
            ),
        ],
    )
    _, wire = normalize_messages_anthropic(
        [msg],
        current_model="claude-opus-4-20250514",
    )
    blocks = wire[0]["content"]
    assert any(
        b.get("type") == "thinking" and b.get("signature") == "sig-current"
        for b in blocks
    )


def test_signature_kept_when_part_model_unknown():
    """
    Backward compat: ThinkingPart без model (None) — старый формат
    из ранее-сохранённых сессий. Доверяем подписи, не ломаем
    legacy-историю.
    """
    msg = Message(
        role="assistant",
        parts=[
            ThinkingPart(
                text="legacy thinking",
                signature="sig-legacy",
                model=None,  # старая запись на диске
            ),
        ],
    )
    _, wire = normalize_messages_anthropic(
        [msg],
        current_model="claude-opus-4-20250514",
    )
    blocks = wire[0]["content"]
    assert any(
        b.get("type") == "thinking" and b.get("signature") == "sig-legacy"
        for b in blocks
    )


def test_signature_kept_when_current_model_unknown():
    """
    Если caller не передал ``current_model`` — ничего не дропаем
    (нет основания считать подпись чужой).
    """
    msg = Message(
        role="assistant",
        parts=[
            ThinkingPart(
                text="thinking",
                signature="sig-any",
                model="claude-3-5-sonnet-20241022",
            ),
        ],
    )
    _, wire = normalize_messages_anthropic([msg])  # без current_model
    blocks = wire[0]["content"]
    assert any(
        b.get("type") == "thinking" and b.get("signature") == "sig-any"
        for b in blocks
    )


def test_unsigned_thinking_unaffected():
    """Старое поведение: unsigned thinking всегда дропается."""
    msg = Message(
        role="assistant",
        parts=[
            ThinkingPart(text="no sig", signature=None, model="claude-opus-4"),
            TextPart(text="answer"),
        ],
    )
    _, wire = normalize_messages_anthropic(
        [msg],
        current_model="claude-opus-4",
    )
    blocks = wire[0]["content"]
    assert all(b.get("type") != "thinking" for b in blocks)


def test_mixed_history_only_stale_dropped():
    """
    Смешанная история: блок от старой модели + блок от новой.
    Должен дропнуться только первый, второй — пройти.
    """
    history = [
        Message(
            role="assistant",
            parts=[
                ThinkingPart(
                    text="old reasoning",
                    signature="sig-old",
                    model="claude-3-5-sonnet-20241022",
                ),
                TextPart(text="old answer"),
            ],
        ),
        Message(role="user", parts=[TextPart(text="follow up")]),
        Message(
            role="assistant",
            parts=[
                ThinkingPart(
                    text="new reasoning",
                    signature="sig-new",
                    model="claude-opus-4-20250514",
                ),
                TextPart(text="new answer"),
            ],
        ),
    ]
    _, wire = normalize_messages_anthropic(
        history,
        current_model="claude-opus-4-20250514",
    )
    # 3 messages: assistant, user, assistant.
    assert len(wire) == 3
    # Первое assistant — без thinking-блока.
    first_blocks = wire[0]["content"]
    assert all(b.get("type") != "thinking" for b in first_blocks)
    # Третье assistant — с thinking-блоком (sig-new сохранилась).
    third_blocks = wire[2]["content"]
    assert any(
        b.get("type") == "thinking" and b.get("signature") == "sig-new"
        for b in third_blocks
    )


# ---------------------------------------------------------------------------
# 3. Sessions persistence: round-trip model field
# ---------------------------------------------------------------------------

def test_part_to_dict_includes_model():
    p = ThinkingPart(
        text="t",
        signature="sig",
        model="claude-opus-4-20250514",
    )
    d = _part_to_dict(p)
    assert d["kind"] == "thinking"
    assert d["text"] == "t"
    assert d["signature"] == "sig"
    assert d["model"] == "claude-opus-4-20250514"


def test_part_to_dict_omits_model_when_none():
    p = ThinkingPart(text="t", signature="sig", model=None)
    d = _part_to_dict(p)
    assert "model" not in d


def test_dict_to_part_reads_model():
    d = {
        "kind": "thinking",
        "text": "t",
        "signature": "sig",
        "model": "claude-3-5-sonnet-20241022",
    }
    p = _dict_to_part(d)
    assert isinstance(p, ThinkingPart)
    assert p.model == "claude-3-5-sonnet-20241022"


def test_dict_to_part_legacy_without_model():
    """JSONL, написанный до GAP-9 фикса — model отсутствует."""
    d = {"kind": "thinking", "text": "t", "signature": "sig"}
    p = _dict_to_part(d)
    assert isinstance(p, ThinkingPart)
    assert p.model is None
    assert p.signature == "sig"


def test_message_round_trip_preserves_model():
    msg = Message(
        role="assistant",
        parts=[
            ThinkingPart(
                text="r",
                signature="s",
                model="claude-opus-4-20250514",
            ),
            TextPart(text="answer"),
        ],
    )
    line = message_to_json(msg)
    # sanity: line is valid JSON
    obj = json.loads(line)
    assert obj["parts"][0]["model"] == "claude-opus-4-20250514"

    restored = json_to_message(line)
    assert isinstance(restored.parts[0], ThinkingPart)
    assert restored.parts[0].model == "claude-opus-4-20250514"
    assert restored.parts[0].signature == "s"
    assert restored.parts[0].text == "r"


# ---------------------------------------------------------------------------
# 4. End-to-end сценарий: switch + следующий запрос на новой модели
# ---------------------------------------------------------------------------

def test_end_to_end_model_switch_drops_stale_signature():
    """
    Воспроизводит реальный сценарий:
    1) Пользователь поговорил с claude-3-5-sonnet — в истории
       ThinkingPart с подписью.
    2) Пользователь сделал /model claude-opus-4 и задал новый
       вопрос.
    3) Перед запросом нормализация выкидывает старую подпись,
       Anthropic 400 не возникает.
    """
    history = [
        Message(role="user", parts=[TextPart(text="привет")]),
        Message(
            role="assistant",
            parts=[
                ThinkingPart(
                    text="думаю над приветствием",
                    signature="sig-bound-to-sonnet",
                    model="claude-3-5-sonnet-20241022",
                ),
                TextPart(text="привет!"),
            ],
        ),
        Message(role="user", parts=[TextPart(text="что дальше?")]),
    ]

    _, wire = normalize_messages_anthropic(
        history,
        current_model="claude-opus-4-20250514",
    )

    # Подпись от sonnet не должна уйти к opus.
    flat_blocks = [
        b
        for m in wire
        for b in (m.get("content") or [])
        if isinstance(b, dict)
    ]
    sonnet_sig_present = any(
        b.get("type") == "thinking"
        and b.get("signature") == "sig-bound-to-sonnet"
        for b in flat_blocks
    )
    assert not sonnet_sig_present, (
        "Stale Anthropic thinking signature from a different model leaked "
        "into the wire payload — Anthropic will reject this request with "
        "HTTP 400."
    )
