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


def test_order_ids_exchange_and_stock_facts_ride_along_on_fills():
    rows = [
        {"Symbol": "Q", "DateTime": "20261006;040000", "Quantity": "60", "TradePrice": "5", "IBCommission": "-0.6", "Buy/Sell": "BUY",
         "Open/CloseIndicator": "O", "LevelOfDetail": "EXECUTION", "IBOrderID": "11", "Exchange": "ARCA", "IssuerCountryCode": "CN", "SubCategory": "ADR", "IBExecID": "a"},
        {"Symbol": "Q", "DateTime": "20261006;040000", "Quantity": "40", "TradePrice": "5.01", "IBCommission": "-0.4", "Buy/Sell": "BUY",
         "Open/CloseIndicator": "O", "LevelOfDetail": "EXECUTION", "IBOrderID": "11", "Exchange": "MEMX", "IssuerCountryCode": "CN", "SubCategory": "ADR", "IBExecID": "b"},
        {"Symbol": "Q", "DateTime": "20261006;040100", "Quantity": "-100", "TradePrice": "5.2", "IBCommission": "-1", "Buy/Sell": "SELL",
         "Open/CloseIndicator": "C", "LevelOfDetail": "EXECUTION", "IBOrderID": "12", "Exchange": "ARCA", "IssuerCountryCode": "CN", "SubCategory": "ADR", "IBExecID": "c"},
    ]
    t = tm.fifo_match_and_merge(tm.parse_csv_executions(rows))[0]
    assert t["Issuer Country"] == "CN" and t["Sub Category"] == "ADR"
    assert {f["entry_order"] for f in t["Fills"]} == {"11"} and {f["exit_order"] for f in t["Fills"]} == {"12"}
    assert {f["entry_exchange"] for f in t["Fills"]} == {"ARCA", "MEMX"}


def test_two_section_csv_is_read_against_each_sections_own_header():
    raw = (
        "ClientAccountID,Symbol,DateTime,Quantity,TradePrice,IBCommission,Buy/Sell,Open/CloseIndicator,LevelOfDetail,IBOrderID,Exchange\n"
        "A1,Q,20261006;040000,100,5,-1,BUY,O,EXECUTION,11,ARCA\n"
        "A1,Q,20261006;040100,-100,5.2,-1,SELL,C,EXECUTION,12,MEMX\n"
        "ClientAccountID,Symbol,Date,TaxDescription,TaxAmount,OrderID\n"
        "A1,Q,20261006,SEC Fee,-0.03,12\n"
        "A1,Q,20261006,FINRA TAF,-0.01,12\n"
    )
    trade_rows, fee_rows = tm.read_csv_sections(raw)
    assert len(trade_rows) == 2 and len(fee_rows) == 2
    fees = tm.parse_csv_fees(fee_rows)
    t = tm.fifo_match_and_merge(tm.parse_csv_executions(trade_rows), fees=fees)[0]
    assert t["Fees"] == 0.04
    assert t["Venues"] == {"ARCA": {"sh": 100.0, "comm": 1.0}, "MEMX": {"sh": 100.0, "comm": 1.0}}
    # fees never change the commission / P&L columns
    assert t["Commission"] == 2.0


def test_fee_on_a_shared_order_is_split_by_shares_and_a_header_only_section_is_harmless():
    raw = (
        "ClientAccountID,Symbol,DateTime,Quantity,TradePrice,IBCommission,Buy/Sell,Open/CloseIndicator,LevelOfDetail,IBOrderID\n"
        "A1,Q,20261006;040000,100,5,-1,BUY,O,EXECUTION,11\n"
        "A1,Q,20261006;040100,-100,5.2,-1,SELL,C,EXECUTION,12\n"
        "ClientAccountID,Symbol,Date,TaxDescription,TaxAmount,OrderID\n"
    )
    rows, fee_rows = tm.read_csv_sections(raw)
    assert fee_rows == [] and len(rows) == 2
    t = tm.fifo_match_and_merge(tm.parse_csv_executions(rows), fees=tm.parse_csv_fees(fee_rows))[0]
    assert t["Fees"] == 0.0


def test_unclosed_position_is_reported_as_open_and_not_saved_as_a_trade():
    opn = []
    trades = tm.fifo_match_and_merge([
        mk("AAA", "093000", 100, 5, 1, "O"), mk("AAA", "094000", -100, 6, 1, "C"),
        mk("BBB", "100000", 50, 10, 1, "O"), mk("BBB", "100500", 25, 11, 1, "O"),
        mk("DDD", "111000", -40, 8, 1, "O"),
    ], open_positions=opn)
    assert [t["Symbol"] for t in trades] == ["AAA"]
    by = {p["symbol"]: p for p in opn}
    assert set(by) == {"BBB", "DDD"}
    assert by["BBB"]["side"] == "Long" and by["BBB"]["qty"] == 75 and by["BBB"]["entry_time"] == "10:00:00"
    assert by["DDD"]["side"] == "Short" and by["DDD"]["qty"] == 40
