"""The Tag guide's text comes from tag_catalog.py -- keep it in step with the tagger."""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tag_catalog as tc  # noqa: E402
import trade_tags as tt  # noqa: E402


def _cat():
    return tc.build()


def test_every_tag_has_exactly_one_entry_and_nothing_extra():
    keys = [e["key"] for e in _cat()["tags"]]
    assert len(keys) == len(set(keys)), "duplicate catalog entries"
    assert set(keys) == tc.expected_keys()


def test_entries_are_complete_and_labels_match_the_tagger():
    groups = {g["id"] for g in _cat()["groups"]}
    for e in _cat()["tags"]:
        assert e["group"] in groups and e["tone"] in ("good", "watch", "info"), e["key"]
        assert e["label"].strip() and e["summary"].strip() and len(e["rule"]) > 20, e["key"]
        if e["key"] in tt.TAG_LABELS:
            assert e["label"] == tt.TAG_LABELS[e["key"]], e["key"]


def test_rule_text_follows_the_constants():
    by = {e["key"]: e["rule"] for e in _cat()["tags"]}
    assert "3%" in by["extended_entry"] and "6%" in by["entry_after_spike"] and "60 seconds" in by["rapid_fire"]
    old = tt.DRAWDOWN_PCT
    try:
        tt.DRAWDOWN_PCT = 0.05
        assert "5%" in {e["key"]: e["rule"] for e in tc.build()["tags"]}["deep_drawdown"]
    finally:
        tt.DRAWDOWN_PCT = old
