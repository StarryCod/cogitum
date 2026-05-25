"""Tests for legion_tree responsive card sizing.

Audit fix #7: the L0 / L1 / L2 cards declared fixed widths (56 / 30 /
22), so a row of three L1s required 96+ cols and clipped on a 80-col
terminal. We added `pick_visual_class()` which downgrades cards to
the smaller `l2` / `l3` style when the slot can't accommodate them,
plus an `l3` ultra-compact card for very narrow rows.
"""
from __future__ import annotations

from cogitum.widgets.legion_tree import _NodeCard


def test_l0_picked_when_slot_is_56_or_more():
    assert _NodeCard.pick_visual_class(0, 56) == "l0"
    assert _NodeCard.pick_visual_class(0, 80) == "l0"


def test_l0_downgrades_to_l1_on_smaller_slot():
    """An L0 hero card with only 40 cols falls back to L1 footprint."""
    assert _NodeCard.pick_visual_class(0, 40) == "l1"


def test_l1_default_at_30_slot():
    assert _NodeCard.pick_visual_class(1, 30) == "l1"


def test_l1_downgrades_to_l2_at_25():
    """Three L1s on an 80-col terminal → ~25 cols each → l2 footprint."""
    assert _NodeCard.pick_visual_class(1, 25) == "l2"


def test_l1_downgrades_to_l3_at_18():
    """Very narrow rows force the ultra-compact card."""
    assert _NodeCard.pick_visual_class(1, 18) == "l3"


def test_l2_default_at_22_slot():
    assert _NodeCard.pick_visual_class(2, 22) == "l2"


def test_l2_downgrades_to_l3_at_14():
    assert _NodeCard.pick_visual_class(2, 14) == "l3"


def test_l3_minimum_at_8():
    """Below 16 cols even L3 doesn't formally fit but we still return
    `l3` since there's no smaller class to fall back to."""
    assert _NodeCard.pick_visual_class(2, 8) == "l3"


def test_l1_card_set_compact_class_swaps_class_attr():
    """The runtime swap removes the old class and adds the new one."""
    card = _NodeCard("alpha", depth=1)
    assert card._visual_class == "l1"
    card.set_compact_class("l2")
    assert card._visual_class == "l2"
    # And no-op when called with the same class
    card.set_compact_class("l2")
    assert card._visual_class == "l2"
