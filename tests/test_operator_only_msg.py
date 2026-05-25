"""F26: OPERATOR_ONLY_MSG must be a clear, helpful message.

The original "✕ operator-only" was opaque. The new constant tells the
non-operator user (a) what the restriction means, (b) how to become
operator, (c) which commands they CAN use freely.

This is also the toast displayed via answer_callback for forbidden
inline button taps, so the constant is the canonical contract — both
test_tg_acl.py and the call sites consume it.
"""
from __future__ import annotations


def test_operator_only_msg_is_helpful():
    from cogitum.gateway.telegram import OPERATOR_ONLY_MSG

    msg = OPERATOR_ONLY_MSG

    # Bullet-point requirements per F26:
    #   - keep the rune so existing tests / UX continuity holds
    assert "✕" in msg
    assert "operator" in msg.lower()
    #   - explain who operator is (deployment owner)
    assert "deployment owner" in msg
    #   - tell them where to configure it
    assert "cog tg setup" in msg
    #   - point them at non-restricted alternatives
    assert "/tools" in msg
    assert "/help" in msg
