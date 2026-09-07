"""
engine.py
Orchestrates the backtest: for each trading day in range, find the top-N
gappers, simulate the strategy on each, and aggregate results + stats.

Swappable strategy: pass any callable with the signature
    (bars_df, trade_date, params) -> list[trade_dict]
Defaults to orb_strategy.simulate_orb_trades, which returns a list of one
trade per symbol/day unless params["allow_reentry"] is on, in which case a
symbol/day can produce more than one trade (see orb_strategy.py).
"""

import logging
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

import polygon_client as pc
from orb_strategy import simulate_orb_trades, DEFAULT_PARAMS

log = logging.getLogger("backtest.engine")


# --- Commission model -------------------------------------------------
# Approximates IBKR Pro's "Tiered" US stock/ETF commission schedule: a
# per-share base rate that steps down as the account's cumulative
# *monthly* traded share volume climbs, subject to a per-order minimum
# and a per-order cap of 1% of trade value, plus a flat per-share
# estimate for the exchange/regulatory/clearing fees IBKR passes through
# on top of its own commission. This is a best-effort approximation for
# backtesting purposes -- not IBKR's actual published fee schedule (which
# varies by exchange/venue and changes over time). See
# interactivebrokers.com/pricing for the real numbers.
IBKR_TIERED_RATE_SCHEDULE = [
    # (cumulative monthly shares ceiling, rate $/share for volume in this band)
    (300_000, 0.0035),
    (3_000_000, 0.0020),
    (20_000_000, 0.0015),
    (100_000_000, 0.0010),
    (float("inf"), 0.0005),
]
IBKR_MIN_PER_ORDER = 0.35
IBKR_MAX_PCT_OF_TRADE_VALUE = 0.01
PASS_THROUGH_FEE_PER_SHARE = 0.0003  # rough estimate: exchange + clearing + regulatory


def _tier_rate_for(cumulative_shares_after: float) -> float:
    for ceiling, rate in IBKR_TIERED_RATE_SCHEDULE:
        if cumulative_shares_after <= ceiling:
            return rate
    return IBKR_TIERED_RATE_SCHEDULE[-1][1]


def estimate_commission(shares: int, price: float, monthly_shares_before: float) -> float:
    """Estimated commission for a single order (entry or exit) of `shares`
    at `price`, given the account's cumulative monthly share volume
    *before* this order. Uses the tier the order lands in when it's
    finished (rather than splitting one order across a tier boundary
    mid-fill) -- an approximation, but a good one for typical
    retail-sized day-trading orders that don't straddle a boundary."""
    if shares <= 0 or price <= 0:
        return 0.0
    rate = _tier_rate_for(monthly_shares_before + shares)
    base = shares * rate
    trade_value = shares * price
    base = max(IBKR_MIN_PER_ORDER, base)
    base = min(base, trade_value * IBKR_MAX_PCT_OF_TRADE_VALUE)
    return round(base + shares * PASS_THROUGH_FEE_PER_SHARE, 4)


class BacktestCancelled(Exception):
    """Raised (from inside progress_cb) to stop a run early. run_backtest
    lets this propagate -- the caller decides what to do with whatever
    trades had already been produced (see chart_service.py)."""
    pass


@dataclass
class BacktestConfig:
    start_date: date
    end_date: date
    top_n: int = 5
    min_price: float = 1.0
    max_price: float = 50.0
    min_dollar_volume: float = 5_000_000
    min_gap_pct: float = 5.0
    position_size_dollars: float = 2000.0  # notional per trade, for $ P&L (not just per-share/R stats)
    strategy_params: dict = field(default_factory=lambda: dict(DEFAULT_PARAMS))
    strategy_fn: callable = simulate_orb_trades
    include_commissions: bool = True  # estimate IBKR-tiered-style commissions and net them out of pnl_dollars/win

    # --- Capital / position sizing -----------------------------------
    # starting_capital is the account balance the run begins with. It's
    # always tracked (running equity = starting_capital + cumulative net
    # pnl_dollars so far) and reported in compute_stats' output, but it
    # only *drives* sizing when position_sizing_mode isn't "fixed_dollars":
    #   "fixed_dollars"        -- position_size_dollars notional per trade,
    #                              same behavior as before this existed.
    #   "pct_of_capital"       -- notional per trade = position_size_pct%
    #                              of *current* running equity (compounds).
    #   "risk_pct_of_capital"  -- shares sized so that
    #                              risk_per_share * shares == risk_pct_of_capital%
    #                              of current running equity (compounds).
    starting_capital: float = 25_000.0
    position_sizing_mode: str = "fixed_dollars"
    position_size_pct: float = 10.0
    risk_pct_of_capital: float = 1.0


def run_backtest(cfg: BacktestConfig, progress_cb=None, cancel_check=None) -> list[dict]:
    """
    Returns a list of trade dicts (normally one per symbol/day that
    produced a trade, or several per symbol/day if
    cfg.strategy_params["allow_reentry"] is on -- see orb_strategy.py),
    each with: date, symbol, gap_pct, entry_time, entry_price,
    exit_time, exit_price, exit_reason, shares, fills, risk_per_share,
    pnl_per_share, pnl_dollars_gross, commission_entry, commission_exit,
    commission_total, pnl_dollars, r_multiple, win.

    entry_price is the share-weighted average cost basis across every
    fill the trade made -- identical to a single-fill trade's entry price
    when cfg.strategy_params["scale_in_enabled"] is off (the default).
    When it's on, `fills` breaks that average down into the individual
    buys (initial + adds), each with its own fill time/price/share count,
    so the UI can show exactly how the position was built instead of just
    the blended number. `shares` is always the trade's total across fills.

    When cfg.include_commissions is True (the default), pnl_dollars is
    NET of an estimated IBKR-tiered-style commission (see
    estimate_commission above) and win reflects that net figure -- a
    trade that's gross-profitable but too small to clear its round-trip
    commission is counted as a loss, same as it would be in a real
    account. Commission volume tiers are tracked cumulatively per
    calendar month across the whole backtest, the same way IBKR tracks
    monthly share volume. When False, pnl_dollars is the old
    commission-free gross figure and win comes straight from the
    strategy, unchanged from before this existed.

    cancel_check, if given, is a no-arg callable returning True once a
    cancel has been requested. It's polled before *every* symbol (not just
    once per day) so a cancel actually takes effect within a second or
    two even mid-day, instead of only at day boundaries -- a single day
    can involve several rate-limited Polygon calls and take a while.
    """
    trades = []
    days = list(pc.trading_days_between(cfg.start_date, cfg.end_date))
    # Cumulative shares traded per calendar month ("YYYY-MM"), used to walk
    # the tiered commission schedule the same way IBKR tracks it -- reset
    # implicitly per month by using a fresh dict key, never explicitly.
    monthly_shares: dict[str, float] = {}
    # Running account equity, used only when position_sizing_mode isn't
    # "fixed_dollars" (see BacktestConfig) -- updated after every trade so
    # later trades size off the compounded balance, not the starting one.
    equity = cfg.starting_capital

    for i, d in enumerate(days):
        if cancel_check and cancel_check():
            raise BacktestCancelled()
        if progress_cb:
            # Pass a snapshot of trades collected so far so the caller can
            # surface partial results (and, by raising BacktestCancelled
            # from in here, stop the run early) without waiting for every
            # remaining day to finish.
            progress_cb(i, len(days), d, list(trades))
        try:
            gainers = pc.find_top_gainers(
                d, top_n=cfg.top_n, min_price=cfg.min_price, max_price=cfg.max_price,
                min_dollar_volume=cfg.min_dollar_volume, min_gap_pct=cfg.min_gap_pct,
            )
        except Exception as e:
            log.warning("Skipping %s -- gainer scan failed: %s", d, e)
            continue

        if gainers.empty:
            continue

        for symbol, row in gainers.iterrows():
            if cancel_check and cancel_check():
                raise BacktestCancelled()
            try:
                bars = pc.fetch_minute_bars(symbol, d)
            except Exception as e:
                log.warning("Skipping %s %s -- bar fetch failed: %s", symbol, d, e)
                continue

            try:
                results = cfg.strategy_fn(bars, d, cfg.strategy_params)
            except Exception as e:
                log.warning("Skipping %s %s -- strategy error: %s", symbol, d, e)
                continue

            # Normally 0 or 1 trade; more than one only when
            # strategy_params["allow_reentry"] is on (see orb_strategy.py).
            # Looping here instead of assuming a single result is the only
            # change from before this existed -- equity compounding and the
            # monthly commission-volume tier both still update once per
            # trade, in the chronological order simulate_orb_trades returns.
            for result in results:
                # "Full" size is computed off the INITIAL fill's price/risk,
                # same formulas as before scale-in existed -- a plain trade
                # (no scale_in_enabled) has exactly one fill at size_frac
                # 1.0, so full_shares below IS its share count, unchanged.
                # With scale-in on, this is the size the trade would hold
                # if it had gone in all at once; each fill in result["fills"]
                # then gets its own slice of it via size_frac.
                initial_price = result.get("initial_entry_price", result["entry_price"])
                if cfg.position_sizing_mode == "pct_of_capital":
                    notional = max(equity, 0.0) * (cfg.position_size_pct / 100.0)
                    full_shares = int(notional / initial_price) if initial_price else 0
                elif cfg.position_sizing_mode == "risk_pct_of_capital":
                    risk_dollars = max(equity, 0.0) * (cfg.risk_pct_of_capital / 100.0)
                    full_shares = int(risk_dollars / result["risk_per_share"]) if result["risk_per_share"] else 0
                else:  # "fixed_dollars" (default, unchanged from before this existed)
                    full_shares = int(cfg.position_size_dollars / initial_price) if initial_price else 0

                fills = result.get("fills") or [{"time": result["entry_time"], "price": result["entry_price"], "size_frac": 1.0}]
                month_key = d.strftime("%Y-%m")
                prior_volume = monthly_shares.get(month_key, 0.0)

                fill_rows = []
                shares = 0
                cost_basis_dollars = 0.0
                commission_entry = 0.0
                for f in fills:
                    fill_shares = round(full_shares * f["size_frac"])
                    if fill_shares <= 0:
                        continue
                    shares += fill_shares
                    cost_basis_dollars += fill_shares * f["price"]
                    if cfg.include_commissions:
                        c = estimate_commission(fill_shares, f["price"], prior_volume)
                        prior_volume += fill_shares
                        commission_entry += c
                    fill_rows.append({
                        "time": f["time"].strftime("%H:%M:%S"),
                        "price": f["price"],
                        "shares": fill_shares,
                    })

                # blended average cost basis across every fill (identical to
                # result["entry_price"] when there's only the one fill, since
                # that's already how orb_strategy.py computed it -- recomputed
                # here off the real rounded share counts instead of the raw
                # size_frac weights, for penny-accurate $ P&L)
                avg_entry_price = (cost_basis_dollars / shares) if shares else result["entry_price"]
                pnl_dollars_gross = shares * (result["exit_price"] - avg_entry_price)

                if cfg.include_commissions:
                    commission_exit = estimate_commission(shares, result["exit_price"], prior_volume)
                    prior_volume += shares
                    monthly_shares[month_key] = prior_volume
                    commission_total = round(commission_entry + commission_exit, 2)
                    pnl_dollars = round(pnl_dollars_gross - commission_total, 2)
                    win = pnl_dollars > 0
                else:
                    commission_exit = 0.0
                    commission_total = 0.0
                    pnl_dollars = round(pnl_dollars_gross, 2)
                    win = result["win"]

                trades.append({
                    "date": d.isoformat(),
                    "symbol": symbol,
                    "gap_pct": round(float(row["gap_pct"]), 2),
                    "entry_time": result["entry_time"].strftime("%H:%M:%S"),
                    "entry_price": round(avg_entry_price, 4),
                    "exit_time": result["exit_time"].strftime("%H:%M:%S"),
                    "exit_price": result["exit_price"],
                    "exit_reason": result["exit_reason"],
                    "stop_price": result["stop_price"],
                    "target_price": result["target_price"],
                    "shares": shares,
                    # Every fill this trade actually made (just one unless
                    # scale-in added more), with real share counts -- lets
                    # the UI show "started 250, added 125 @ $11.00, added
                    # 125 @ $11.20" instead of one opaque entry price.
                    "fills": fill_rows,
                    "risk_per_share": result["risk_per_share"],
                    "pnl_per_share": round(result["exit_price"] - avg_entry_price, 4),
                    "pnl_dollars_gross": round(pnl_dollars_gross, 2),
                    "commission_entry": round(commission_entry, 2),
                    "commission_exit": round(commission_exit, 2),
                    "commission_total": commission_total,
                    "r_multiple": result["r_multiple"],
                    "pnl_dollars": pnl_dollars,
                    "win": win,
                })
                equity += pnl_dollars

    trades.sort(key=lambda t: (t["date"], t["entry_time"]))
    return trades


def compute_stats(trades: list[dict], starting_capital: float = 0.0) -> dict:
    """Summary stats + equity curve from a list of trade dicts (as produced
    by run_backtest). Safe to call with an empty list. `starting_capital`
    is optional and purely for display/context (ending_capital = it + net
    P&L) -- it doesn't change any of the other stats, which are all still
    computed from pnl_dollars alone."""
    if not trades:
        return {
            "num_trades": 0, "win_rate": 0.0, "profit_factor": None,
            "avg_win_dollars": 0.0, "avg_loss_dollars": 0.0, "avg_r": 0.0,
            "expectancy_r": 0.0, "net_pnl_dollars": 0.0, "max_drawdown_dollars": 0.0,
            "longest_win_streak": 0, "longest_loss_streak": 0, "equity_curve": [],
            "total_commissions_dollars": 0.0,
            "starting_capital": round(starting_capital, 2),
            "ending_capital": round(starting_capital, 2),
        }

    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]

    gross_profit = sum(t["pnl_dollars"] for t in wins)
    gross_loss = -sum(t["pnl_dollars"] for t in losses)  # positive number

    equity = 0.0
    equity_curve = []
    peak = 0.0
    max_dd = 0.0
    cur_win_streak = cur_loss_streak = 0
    longest_win_streak = longest_loss_streak = 0

    for t in trades:
        equity += t["pnl_dollars"]
        equity_curve.append({"date": t["date"], "symbol": t["symbol"], "equity": round(equity, 2)})
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if t["win"]:
            cur_win_streak += 1
            cur_loss_streak = 0
        else:
            cur_loss_streak += 1
            cur_win_streak = 0
        longest_win_streak = max(longest_win_streak, cur_win_streak)
        longest_loss_streak = max(longest_loss_streak, cur_loss_streak)

    avg_r = sum(t["r_multiple"] for t in trades) / len(trades)
    total_commissions = sum(t.get("commission_total", 0.0) for t in trades)

    return {
        "num_trades": len(trades),
        "win_rate": round(100.0 * len(wins) / len(trades), 1),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win_dollars": round(gross_profit / len(wins), 2) if wins else 0.0,
        "avg_loss_dollars": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "avg_r": round(avg_r, 3),
        "expectancy_r": round(avg_r, 3),
        "net_pnl_dollars": round(equity, 2),
        "max_drawdown_dollars": round(max_dd, 2),
        "longest_win_streak": longest_win_streak,
        "longest_loss_streak": longest_loss_streak,
        "equity_curve": equity_curve,
        "total_commissions_dollars": round(total_commissions, 2),
        "starting_capital": round(starting_capital, 2),
        "ending_capital": round(starting_capital + equity, 2),
    }
