"""Tests for /import-trades preview + date window (run: python -m pytest tests/ or python tests/test_import_window.py).
Uses Flask's test client with auth, account lookup and the journal lookup stubbed, so nothing hits Supabase."""
import io, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from flask import Flask
import import_routes as ir
import publish

CSV = "Symbol,DateTime,Buy/Sell,Quantity,TradePrice,Commission\n" + "".join(
    f"AAPL,{d} 09:35:00,BUY,10,100,0.5\nAAPL,{d} 09:50:00,SELL,10,101,0.5\n"
    for d in ("2025-01-10", "2026-03-05", "2026-04-06"))


def client(existing=()):
    ir.resolve_user_id = lambda h: "u1"
    publish.resolve_account_id = lambda u, a: "acc"
    ir._existing_trade_ids = lambda u, ids: set(i for i in ids if i in existing)
    app = Flask(__name__)
    app.register_blueprint(ir.bp)
    return app.test_client()


def post(c, **form):
    # a preview must never start a background job. The patch lasts for this one request only:
    # threading.Thread is process-wide, so leaving it patched breaks every test that runs after this one.
    real = ir.threading.Thread
    ir.threading.Thread = lambda *a, **k: (_ for _ in ()).throw(AssertionError("job started during preview"))
    try:
        return c.post("/import-trades", data={"trade_csv": (io.BytesIO(CSV.encode()), "t.csv"), **form}, content_type="multipart/form-data")
    finally:
        ir.threading.Thread = real


def test_preview_returns_trades_and_starts_no_job():
    r = post(client(), preview="1")
    j = r.get_json()
    assert r.status_code == 200 and j["preview"] is True and "job_id" not in j
    assert len(j["trades"]) == 3 and j["file"] == {"total": 3, "first": "2025-01-10", "last": "2026-04-06"}


def test_date_window_is_inclusive_and_file_summary_stays_whole():
    j = post(client(), preview="1", date_from="2026-03-05", date_to="2026-04-06").get_json()
    assert [t["Trade Date"] for t in j["trades"]] == ["2026-03-05", "2026-04-06"]
    assert j["file"]["total"] == 3
    j = post(client(), preview="1", date_to="2025-12-31").get_json()
    assert [t["Trade Date"] for t in j["trades"]] == ["2025-01-10"]


def test_already_imported_is_reported_for_the_window():
    j = post(client(existing={"AAPL-20260305-093500"}), preview="1", date_from="2026-03-01").get_json()
    assert j["already_imported"] == ["AAPL-20260305-093500"]


def test_preview_rows_are_slim_and_carry_the_trade_id():
    j = post(client(), preview="1").get_json()
    assert set(j["trades"][0]) == {"trade_id", "Symbol", "Trade Date", "Entry Time", "Side", "P&L After Comm"}
    assert j["trades"][0]["trade_id"] == "AAPL-20250110-093500"


def test_exclude_ids_drops_hand_picked_trades_after_the_window():
    j = post(client(), preview="1", date_from="2026-01-01", exclude_ids='["AAPL-20260305-093500"]').get_json()
    assert [t["Trade Date"] for t in j["trades"]] == ["2026-04-06"]


def test_bad_exclude_ids_is_rejected():
    assert post(client(), preview="1", exclude_ids="not json").status_code == 400
    assert post(client(), preview="1", exclude_ids='{"a":1}').status_code == 400


def test_bad_date_is_rejected():
    assert post(client(), preview="1", date_from="03/05/2026").status_code == 400


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
