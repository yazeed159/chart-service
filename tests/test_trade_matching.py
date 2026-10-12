"""Regression tests for trade_matching.py (run: python -m pytest tests/ or python tests/test_trade_matching.py)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import trade_matching as tm


def mk(sym, t, q, p, c=0, oc=None, lod=None):
    return {"symbol": sym, "dateTime": f"20261006;{t}", "quantity": q, "tradePrice": p,
            "commission": c, "openClose": oc, "_source": "csv"}


def test_orphan_close_is_ignored_not_turned_into_a_position():
    ign = []
    assert tm.fifo_match_and_merge([mk("TNMG", "041449", -100, 4.01, 1, "C")], skipped=ign) == []
    assert ign and ign[0]["symbol"] == "TNMG"


def test_partially_opened_position_is_dropped_whole_and_next_trade_unaffected():
    ign = []
    r = tm.fifo_match_and_merge([
        mk("X", "040000", 100, 5, 0, "O"), mk("X", "040100", -150, 6, 0, "C"),   # 50 shares opened yesterday
        mk("X", "040200", 100, 5, 0, "O"), mk("X", "040300", -100, 5.5, 0, "C"),
    ], skipped=ign)
    assert [(t["Entry Time"], t["No. of Shares"]) for t in r] == [("04:02:00", 100.0)]
    assert len(ign) == 1


def test_no_indicator_column_keeps_old_behaviour():
    r = tm.fifo_match_and_merge([mk("X", "040000", -100, 5), mk("X", "040100", 100, 4)])
    assert r[0]["Side"] == "Short" and r[0]["P&L Before Comm"] == 100.0


def test_flip_c_semicolon_o_still_flips():
    r = tm.fifo_match_and_merge([mk("X", "040000", 100, 5, 0, "O"), mk("X", "040100", -200, 6, 0, "C;O"),
                                 mk("X", "040200", 100, 5, 0, "C")])
    assert [t["Side"] for t in r] == ["Long", "Short"]


def test_order_and_execution_levels_are_not_double_counted():
    base = {"Symbol": "AAA", "DateTime": "20261006;040000", "TradePrice": "1", "IBCommission": "0"}
    rows = [
        {**base, "LevelOfDetail": "ORDER", "Quantity": "300", "Buy/Sell": "BUY", "Open/CloseIndicator": "O"},
        {**base, "LevelOfDetail": "EXECUTION", "Quantity": "100", "Buy/Sell": "BUY", "Open/CloseIndicator": "O", "IBExecID": "a"},
        {**base, "LevelOfDetail": "EXECUTION", "Quantity": "200", "Buy/Sell": "BUY", "Open/CloseIndicator": "O", "IBExecID": "b"},
        {"Symbol": "Symbol", "DateTime": "ReportDate", "TradePrice": "Source"},  # stray header line
    ]
    raw = tm.parse_csv_executions(rows)
    assert sorted(r["quantity"] for r in raw) == [100.0, 200.0]


def test_partial_matches_do_not_recharge_a_lots_commission():
    # one 100-share buy (comm 1.00) sold in two 50-share pieces (comm 0 each)
    r = tm.fifo_match_and_merge([mk("X", "040000", 100, 5, 1.0, "O"), mk("X", "040100", -50, 6, 0, "C"),
                                 mk("X", "040200", -50, 6, 0, "C")])
    assert r[0]["Commission"] == 1.0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)


def test_commission_is_split_into_entry_and_exit_and_adds_up():
    # two entries (1.00 + 0.50), one exit of both (1.20): in = 1.50, out = 1.20, total 2.70
    r = tm.fifo_match_and_merge([
        mk("Z", "040000", 100, 5, 1.00, "O"), mk("Z", "040030", 50, 5.1, 0.50, "O"),
        mk("Z", "040500", -150, 5.5, -1.20, "C"),
    ])
    t = r[0]
    assert t["Commission"] == 2.70 and t["Commission In"] == 1.50 and t["Commission Out"] == 1.20
    assert round(sum(f["commission_entry"] for f in t["Fills"]), 2) == 1.50
    assert round(sum(f["commission_exit"] for f in t["Fills"]), 2) == 1.20


def test_partial_exits_share_the_entry_commission_without_double_counting():
    r = tm.fifo_match_and_merge([
        mk("Y", "040000", 100, 5, 1.00, "O"),
        mk("Y", "040100", -40, 5.2, 0.40, "C"), mk("Y", "040200", -60, 5.3, 0.60, "C"),
    ])
    t = r[0]
    assert t["Commission In"] == 1.00 and t["Commission Out"] == 1.00 and t["Commission"] == 2.00
