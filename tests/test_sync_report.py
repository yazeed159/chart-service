"""Tests for the sync_report details daily_sync.py builds for the Accounts page.
Run: python tests/test_sync_report.py   (or pytest)"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import daily_sync as ds


def test_stuck_entry_carries_reason_attempts_and_next_retry():
    trade = {"Symbol": "BBB", "Trade Date": "2026-10-12", "Entry Time": "10:00:00"}
    e = ds._stuck_entry(trade, {"n": 3, "last": "2026-10-12T10:00:00Z", "err": "no bars"})
    assert e["id"] == "BBB-20261012-100000" and e["reason"] == "no bars" and e["attempts"] == 3
    assert e["next_retry"] == "2026-10-13T06:00:00Z"        # last + 20h cooldown (default)


def test_next_retry_is_next_run_during_fast_retries_and_reason_has_a_fallback():
    assert ds.next_retry_of({"n": 1, "last": "2026-10-12T10:00:00Z"}) is None
    assert ds.next_retry_of(None) is None
    assert ds._stuck_entry({"Symbol": "X"}, None)["reason"]


def test_note_failure_remembers_the_reason_and_keeps_it_if_none_given():
    t, att = {"Symbol": "BBB", "Trade Date": "2026-10-12", "Entry Time": "10:00:00"}, {}
    ds._note_failure(att, t, "no bars")
    ds._note_failure(att, t)
    assert att[ds.trade_id_of(t)]["n"] == 2 and att[ds.trade_id_of(t)]["err"] == "no bars"


def test_build_report_collects_open_stuck_skipped_and_failed_saves():
    acct = {"_open": [{"symbol": "BBB"}], "_stuck": [{"symbol": "AAA"}], "_ignored": [{"symbol": "CCC", "reason": "r"}], "_deferred": 2}
    r = ds.build_report(acct, [{"status": "published", "id": "A-1-1"}, {"status": "failed", "id": "EEE-20261012-101010", "reason": "db down"}])
    assert r["open_positions"] == [{"symbol": "BBB"}] and r["no_chart"] == [{"symbol": "AAA"}] and r["deferred"] == 2
    assert [s["symbol"] for s in r["skipped"]] == ["CCC", "EEE"] and "db down" in r["skipped"][1]["reason"]


if __name__ == "__main__":  # (sync-now tests need only flask)
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")


# ---- /sync-now (the "Sync now" button) ----
def _client(monkeypatch_user="u1", accounts=None):
    from flask import Flask
    import supabase_auth
    supabase_auth.resolve_user_id = lambda h: monkeypatch_user if h == "Bearer good" else None
    ds.get_broker_accounts = lambda: accounts if accounts is not None else [
        {"id": "acc1", "account_id": "acc1", "user_id": "u1"}, {"id": "acc2", "account_id": "acc2", "user_id": "u2"}]
    started = []
    ds.run_daily_sync = lambda accounts=None: started.append([a["id"] for a in accounts])
    ds._manual_last.clear()
    app = Flask(__name__); app.register_blueprint(ds.bp)
    return app.test_client(), started


def test_sync_now_requires_login_and_an_account():
    c, _ = _client()
    assert c.post("/sync-now", json={"account_id": "acc1"}).status_code == 401
    assert c.post("/sync-now", json={}, headers={"Authorization": "Bearer good"}).status_code == 400


def test_sync_now_only_syncs_the_callers_own_account():
    c, started = _client()
    h = {"Authorization": "Bearer good"}
    assert c.post("/sync-now", json={"account_id": "acc2"}, headers=h).status_code == 404   # someone else's
    r = c.post("/sync-now", json={"account_id": "acc1"}, headers=h)
    assert r.status_code == 202
    import time; time.sleep(0.3)
    assert started == [["acc1"]]


def test_sync_now_cooldown_and_running_lock():
    c, _ = _client()
    h = {"Authorization": "Bearer good"}
    assert c.post("/sync-now", json={"account_id": "acc1"}, headers=h).status_code == 202
    import time; time.sleep(0.3)
    assert c.post("/sync-now", json={"account_id": "acc1"}, headers=h).status_code == 429
    ds._manual_last.clear()
    ds._daily_sync_lock.acquire()
    try:
        assert c.post("/sync-now", json={"account_id": "acc1"}, headers=h).status_code == 409
    finally:
        ds._daily_sync_lock.release()


def test_statement_info_reports_period_and_latest_fill():
    doc = {"FlexQueryResponse": {"FlexStatements": {"FlexStatement": {"fromDate": "20261012", "toDate": "20261012", "period": "LastBusinessDay"}}}}
    i = ds.statement_info(doc, [{"dateTime": "20261012;155830"}, {"dateTime": "20261012;093005"}], 1)
    assert i["fills"] == 2 and i["latest_fill"] == "2026-10-12 15:58" and i["from"] == "20261012" and i["closed"] == 1
