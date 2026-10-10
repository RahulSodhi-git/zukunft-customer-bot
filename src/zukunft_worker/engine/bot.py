"""Hands-off auto-trading bot (TESTNET).

You run it once:   python bot.py BTCUSDT 50
Then it loops forever:
  - backtest all strategies -> pick the best RELIABLE + PROFITABLE one
  - when that strategy signals Long/Short -> open the trade with your USDT
  - auto-set take-profit / stop-loss (capped by fail-safes) as REAL exchange orders
  - close on take-profit, stop-loss, or a strategy flip
Stop it anytime with Ctrl+C.
"""
import json
import os
import sys
import time
from runtime_control import entries_paused, record_monitor_pass
from exit_rules import normal_target
from concurrent.futures import ThreadPoolExecutor, wait

import numpy as np

from data import get_ohlcv, crypto_perp_symbols
from indicators import adx
from strategies import STRATEGIES
from signals import pick_best_strategy, latest_signal, auto_tp_sl, auto_leverage, higher_tf_trend, HISTORY

_HIGHER_TF = {"1m": "15m", "3m": "1h", "5m": "1h", "15m": "4h", "30m": "4h", "1h": "4h",
              "2h": "1d", "4h": "1d", "6h": "1d", "8h": "1d", "12h": "1d", "1d": "1w", "3d": "1w", "1w": "1M"}
from trader.client import BinanceTrader
from trader import guards
from trader import coinalyze          # Coinalyze first, direct Binance fallback

_READ_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="market-read")
_LAST_WATCH = [0.0]


def _read_with_watch(t, r, state, fn, *args, **kwargs):
    """Keep exits on the owner thread while a slow, read-only task runs."""
    interval = max(0.1, float(r.get("watch_seconds", 1)))
    if state and time.monotonic() - _LAST_WATCH[0] >= interval:
        watch_positions(t, r, state)
    future = _READ_POOL.submit(fn, *args, **kwargs)
    while not wait([future], timeout=interval)[0]:
        watch_positions(t, r, state)
    if state and time.monotonic() - _LAST_WATCH[0] >= interval:
        watch_positions(t, r, state)
    return future.result()

STATE_FILE = "bot_state.json"
TIMEFRAME = "1h"
LOSS_TIMES = {}    # coin -> [epoch of each losing close]; N losses within loss_ban_hours -> banned
LAST_CLOSE = {}    # coin -> (epoch_closed, timeframe) for the short re-entry cooldown
RECENT_OPENS = []  # timestamps of recent opens (spreads new trades out over time)
LAST_NORMAL_CLOSE = [0.0]  # epoch of the most recent NORMAL-trade close -> global 'rest' timer
SPECIAL_TP_LOCK = {}       # coin -> epoch of a +100% special TP; blocks re-entry (both dirs) for 24h
SPECIAL_TP_FILE = "special_tp_lock.json"   # persisted so the 24h lock survives bot restarts
REENTRY_FILE = "reentry_state.json"        # persists LAST_CLOSE + LOSS_TIMES so the cooldown, the 6h
#                                            post-loss lockout, and the 2-loss ban survive restarts/deploys
GAINER_PREV_TOP = None     # symbols in the top-N gainers at the PREVIOUS 15m bar (None = not seeded)
GAINER_LAST_BAR = [0]      # the 15m bar we last evaluated (entries fire once per closed bar)
GAINER_PREV_FILE = "gainer_prev_top.json"  # persisted so a restart doesn't re-fire stale crossings
MIN_CAPITAL = 100.0        # dashboard 'Trading Capital' floor ($; below this, per-coin min-notional bites)
CAPITAL_FILE = "capital_state.json"        # persists the allocated slice (base + reserved untouched $)
UI_SETTINGS_FILE = "ui_settings.json"      # dashboard settings (max_coins, trading_capital, live_mode)
ACCOUNT_STATUS_FILE = "account_status.json"   # the account the bot is ACTUALLY on (demo/live) -> dashboard reads it


def _write_account_status(testnet, error=None):
    """Publish the account the bot is really trading (so the dashboard + default trader match it), plus
    the last switch error if any (so a failed Demo->Live switch shows on the dashboard, not just logs)."""
    try:
        d = {"testnet": bool(testnet)}
        if error:
            d["switch_error"] = str(error)[:200]
        json.dump(d, open(ACCOUNT_STATUS_FILE, "w"))
    except Exception as e:
        log(f"account status write failed: {e}")


def _desired_testnet(current):
    """The account the dashboard Demo/Live toggle WANTS (ui_settings.json 'live_mode'). Falls back to
    `current` so NOTHING switches until the toggle is set. live_mode true -> live (testnet=False)."""
    try:
        v = json.load(open(UI_SETTINGS_FILE)).get("live_mode")
        if v is not None:
            return not bool(v)
    except Exception:
        pass
    return current


def _clear_for_switch():
    """Switching accounts starts FRESH: the old account's positions/guards don't apply to the new one.
    Wipes tracked state + all guard state, in-memory and on disk."""
    global GAINER_PREV_TOP
    save_state({})
    LAST_CLOSE.clear(); LOSS_TIMES.clear(); SPECIAL_TP_LOCK.clear()
    RECENT_OPENS.clear(); LAST_NORMAL_CLOSE[0] = 0.0
    GAINER_PREV_TOP = None; GAINER_LAST_BAR[0] = 0
    for f in (REENTRY_FILE, SPECIAL_TP_FILE, GAINER_PREV_FILE):
        try:
            if os.path.exists(f):
                os.remove(f)
        except Exception:
            pass


def _coin_banned(symbol, r):
    """Fix #2: if a coin has had `losses_before_ban` losing trades within the last
    `loss_ban_hours`, block ALL trades on it (normal AND special) until they age out."""
    n = int(r.get("losses_before_ban", 2))
    window = float(r.get("loss_ban_hours", 24)) * 3600
    times = [x for x in LOSS_TIMES.get(symbol, []) if time.time() - x < window]
    LOSS_TIMES[symbol] = times                     # prune old losses
    return len(times) >= n


def _record_loss(symbol):
    LOSS_TIMES.setdefault(symbol, []).append(time.time())


def _can_open_now(r):
    """Limit NEW trades per time window so the bot doesn't fire them all at once."""
    window = r.get("open_batch_window_min", 30) * 60
    now = time.time()
    RECENT_OPENS[:] = [ts for ts in RECENT_OPENS if now - ts < window]   # keep only recent
    return len(RECENT_OPENS) < r.get("max_trades_per_batch", 3)


def _open_size(t, r, cap_now):
    """Size the NEXT trade: a fixed % (trade_pct_of_capital) of the CAPITAL BASE — no fixed $-per-trade
    cap, so sizing is purely proportional to whatever capital is allocated. Still capped by the free
    margin actually available on the exchange, so the last trade just takes whatever is left instead of
    failing with 'margin insufficient' (-2019)."""
    want = cap_now * r.get("trade_pct_of_capital", 0.10)      # pure % of the allocated capital (no $ cap)
    try:
        avail = float(t.fut.futures_account()["availableBalance"]) * 0.97   # 3% buffer for fees/slippage
    except Exception:
        avail = 0.0
    return round(max(0.0, min(want, avail)), 2)


def _filled_margin(res, fallback):
    """The REAL margin actually deployed, read back from the exchange fill (notional / leverage).
    futures_market_order returns this in res['margin']; if it's missing (dry-run / old path) we fall
    back to the intended amount. This is what we store as the trade 'size' so the dashboard + history
    show the true margin, not the pre-cap intention."""
    try:
        if isinstance(res, dict) and res.get("margin") and float(res["margin"]) > 0:
            value = float(res["margin"])
            qty = float(res.get("filled_qty", 0))
            if np.isfinite(value) and np.isfinite(qty) and qty > 0:
                return value
    except Exception:
        pass
    raise ValueError("No verified filled margin; no position will be recorded")


def _fill_entry(res, fallback):
    """The REAL average fill price (VWAP of all chunks) — this is exactly what Binance stores as the
    position entryPrice. We use it as our entry and place the stop relative to it, so the dashboard
    P&L and the -20% floor are measured from the true entry, not the pre-order ticker."""
    try:
        if isinstance(res, dict) and res.get("avg_price") and float(res["avg_price"]) > 0:
            value = float(res["avg_price"])
            if np.isfinite(value):
                return value
    except Exception:
        pass
    raise ValueError("No verified entry price")


def _capital_now(t, fallback):
    """The capital base the bot sizes off. If a Trading Capital is set on the dashboard
    (ui_settings.json -> trading_capital), the bot trades ONLY that slice and COMPOUNDS it with its own
    realised P&L, leaving the rest of the wallet untouched: at set-time it reserves (wallet - slice), then
    allocation = current wallet - reserved. So the bot's profits grow the slice, its losses shrink it, and
    the reserved remainder is never traded. Blank/below $100 -> the FULL wallet balance is used (which
    already compounds). Never returns more than the wallet holds. Only the trading rules' capital *base*
    changes here — every %-based rule (10%/trade, leverage, stops, slots) is applied unchanged."""
    try:
        wallet = float(t.futures_balance().get("USDT", 0) or 0.0)
    except Exception:
        wallet = 0.0
    entered = None
    try:
        if os.path.exists(UI_SETTINGS_FILE):
            v = json.load(open(UI_SETTINGS_FILE)).get("trading_capital")
            entered = float(v) if v not in (None, "", 0, "0", 0.0) else None
    except Exception:
        entered = None
    if not entered or entered < MIN_CAPITAL:             # blank / below floor -> full wallet
        if os.path.exists(CAPITAL_FILE):
            try: os.remove(CAPITAL_FILE)                 # clear any stale allocation
            except Exception: pass
        return wallet or fallback
    st = {}
    if os.path.exists(CAPITAL_FILE):
        try: st = json.load(open(CAPITAL_FILE))
        except Exception: st = {}
    if st.get("base") != entered:                        # first set OR the user changed the amount
        if wallet <= 0 or entered > wallet:              # can't allocate more than is in the wallet
            log(f"Trading Capital {entered:.0f} USDT exceeds wallet {wallet:.2f} -> using full wallet")
            if os.path.exists(CAPITAL_FILE):
                try: os.remove(CAPITAL_FILE)
                except Exception: pass
            return wallet or fallback
        st = {"base": entered, "reserved": round(max(0.0, wallet - entered), 2), "set_at": time.time()}
        try: json.dump(st, open(CAPITAL_FILE, "w"))
        except Exception as e: log(f"capital_state save failed: {e}")
        log(f"Trading Capital = {entered:.0f} USDT | reserving {st['reserved']:.2f} USDT of the wallet "
            f"untouched | this slice compounds with the bot's P&L")
    return round(max(0.0, wallet - float(st.get("reserved", 0.0))), 2)   # compounds up/down with P&L

_TF_MIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120,
           "4h": 240, "6h": 360, "8h": 480, "12h": 720, "1d": 1440, "3d": 4320, "1w": 10080}


def _on_cooldown(symbol, r):
    """True if the coin was closed too recently (cooldown scaled to its timeframe)."""
    info = LAST_CLOSE.get(symbol)
    if not info:
        return False
    closed_at, tf = info[0], info[1]
    won = info[2] if len(info) > 2 else False
    cooldown_min = max(r.get("reentry_cooldown_min", 30),
                       _TF_MIN.get(tf, 240) * r.get("reentry_cooldown_factor", 1.0))
    if won:                                # after a WINNING close, wait longer -> don't re-chase the
        cooldown_min = max(cooldown_min,   # coin's reversal (validated: 0-6h post-win re-entry loses).
                           r.get("reentry_after_win_hours", 6) * 60)
    else:                                  # after a LOSING close, lock the coin out of ALL strategies
        cooldown_min = max(cooldown_min,   # (gainer/special/normal share LAST_CLOSE) -> no cross-strategy
                           r.get("reentry_after_loss_hours", 6) * 60)   # re-chase of a coin that just lost
    return (time.time() - closed_at) < cooldown_min * 60


def _load_special_tp_lock():
    """Load the persisted +100%-TP lockout map on startup so the 24h block survives restarts."""
    global SPECIAL_TP_LOCK
    try:
        if os.path.exists(SPECIAL_TP_FILE):
            SPECIAL_TP_LOCK = {k: float(v) for k, v in json.load(open(SPECIAL_TP_FILE)).items()}
    except Exception:
        SPECIAL_TP_LOCK = {}


def _load_reentry():
    """Restore LAST_CLOSE (re-entry cooldown + 6h post-loss lockout + post-win rule) and LOSS_TIMES
    (2-loss ban) on startup, so those guards survive a bot restart / deploy — the same way the
    +100%-TP lock already does. Without this, a deploy inside a lockout window would forget it."""
    global LAST_CLOSE, LOSS_TIMES
    try:
        if os.path.exists(REENTRY_FILE):
            with open(REENTRY_FILE) as handle:
                d = json.load(handle)
            LAST_CLOSE = {k: tuple(v) for k, v in (d.get("last_close") or {}).items()}
            LOSS_TIMES = {k: [float(x) for x in v] for k, v in (d.get("loss_times") or {}).items()}
            RECENT_OPENS[:] = [float(x) for x in d.get("recent_opens", [])]
            LAST_NORMAL_CLOSE[0] = float(d.get("last_normal_close", 0))
    except Exception:
        pass


def _save_reentry():
    """Persist LAST_CLOSE + LOSS_TIMES. Prunes entries well past any rule window (24h / 48h, both far
    beyond the 6h lockout and 24h ban) so the file stays tiny. Called on every trade close."""
    try:
        now = time.time()
        lc = {k: list(v) for k, v in LAST_CLOSE.items() if v and now - v[0] < 86_400}
        lt = {k: [x for x in v if now - x < 172_800] for k, v in LOSS_TIMES.items()}
        lt = {k: v for k, v in lt.items() if v}
        with open(REENTRY_FILE + ".tmp", "w") as handle:
            json.dump({"last_close": lc, "loss_times": lt, "recent_opens": RECENT_OPENS,
                       "last_normal_close": LAST_NORMAL_CLOSE[0]}, handle)
        os.replace(REENTRY_FILE + ".tmp", REENTRY_FILE)
    except Exception as e:
        log(f"reentry-state save failed: {e}")


def _mark_special_tp(symbol):
    """A special just booked +100% margin -> lock this coin out of new specials (both directions)."""
    SPECIAL_TP_LOCK[symbol] = time.time()
    try:
        json.dump(SPECIAL_TP_LOCK, open(SPECIAL_TP_FILE, "w"))
    except Exception as e:
        log(f"special TP lock save failed ({symbol}): {e}")


def _special_tp_locked(symbol, r):
    """True if this coin booked a +100% special TP within the last special_tp_cooldown_hours (default
    24h) -> no new special entry, EITHER direction (the climax move is exhausted; backtest showed both
    same-direction re-entry and the opposite fade lose). Set special_tp_cooldown_hours=0 to disable."""
    hrs = float(r.get("special_tp_cooldown_hours", 24))
    if hrs <= 0:
        return False
    ts = SPECIAL_TP_LOCK.get(symbol)
    return ts is not None and (time.time() - ts) < hrs * 3600


def _gainer_bar_now(r):
    """The current 15m bar (epoch, floored). The gainer entry is evaluated ONCE per closed bar —
    NOT on every 120s cycle — because that is the cadence the strategy was validated at."""
    bar = max(60, int(r.get("gainer_bar_seconds", 900)))
    return int(time.time() // bar) * bar


def _load_gainer_prev():
    """Restore the previous top-N list so a restart doesn't treat every listed coin as a fresh
    crossing (which would open the gainer slots instantly on stale signals)."""
    global GAINER_PREV_TOP
    try:
        if os.path.exists(GAINER_PREV_FILE):
            d = json.load(open(GAINER_PREV_FILE))
            GAINER_LAST_BAR[0] = int(d.get("bar", 0))
            GAINER_PREV_TOP = set(d.get("top") or [])
    except Exception:
        GAINER_PREV_TOP = None


def _save_gainer_prev(bar, top):
    try:
        with open(GAINER_PREV_FILE + ".tmp", "w") as handle:
            json.dump({"bar": bar, "top": sorted(top)}, handle)
        os.replace(GAINER_PREV_FILE + ".tmp", GAINER_PREV_FILE)
    except Exception as e:
        log(f"gainer prev-top save failed: {e}")


def _gainer_candidates(r, state):
    """TOP-10 GAINERS entry. Fires ONLY on the bar a coin CROSSES INTO the top-N 24h gainers list
    (was outside last bar, inside now) — not while it sits there. LONG only, no other entry gate;
    the 4h OI check happens later in _do_gainer_trades. Returns [] on any cycle that is not a
    fresh 15m bar, and on the first bar after a restart (seed only, never trade a stale list)."""
    global GAINER_PREV_TOP
    bar = _gainer_bar_now(r)
    if bar == GAINER_LAST_BAR[0]:
        return []                                       # this 15m bar was already evaluated
    top_n = int(r.get("gainer_top_n", 10))
    try:
        from data import top_gainers
        cur = top_gainers(n=top_n, min_quote_volume=r.get("gainer_min_volume", 5_000_000))
    except Exception as e:
        log(f"gainer scan failed: {e}")
        return []
    if not cur:
        return []
    prev = GAINER_PREV_TOP
    if bar - GAINER_LAST_BAR[0] != max(60, int(r.get("gainer_bar_seconds", 900))):
        prev = None
    cur_set = set(cur)
    GAINER_LAST_BAR[0] = bar
    GAINER_PREV_TOP = cur_set
    _save_gainer_prev(bar, cur_set)
    if prev is None:                                    # first bar after a restart -> seed only
        log(f"gainer: seeded the top-{top_n} list ({len(cur_set)} coins) — no entries on this bar")
        return []
    out = []
    for sym in cur:
        if sym in prev or sym in state:                 # already on the list / already trading it
            continue
        if _excluded(sym, r) or _coin_banned(sym, r) or _on_cooldown(sym, r):
            continue
        if _special_tp_locked(sym, r):
            continue
        out.append({"symbol": sym, "direction": 1,      # LONG only (validated; shorts untested)
                    "reason": f"crossed INTO the top-{top_n} 24h gainers list"})
    if out:
        log(f"gainer: {len(out)} new crossing(s) into the top-{top_n}: "
            f"{', '.join(o['symbol'] for o in out)}")
    return out


def _gainer_oi_ok(t, symbol, r):
    """The GAINER-ONLY OI gate: open interest must be up >= gainer_oi_pct over the last
    gainer_oi_lookback_hours (validated at 4h/+2%). Rising price + rising OI = new money behind
    the move; rising price + flat/falling OI = a short squeeze that fades. FAILS CLOSED."""
    hrs = float(r.get("gainer_oi_lookback_hours", 4))
    need = float(r.get("gainer_oi_pct", 2.0))
    # Same provider order and data validation for every trade type.
    chg, src = coinalyze.oi_change(symbol, hrs, t)
    if chg is None:                                     # both Coinalyze AND Binance failed
        if r.get("gainer_oi_fail_closed", True):
            log(f"gainer skip {symbol}: OI unavailable (Coinalyze+Binance both failed) -> fail-closed")
            return False
        return True
    if chg < need:
        log(f"gainer skip {symbol}: OI {chg:+.1f}% over {hrs:g}h [{src}] (needs >= +{need:g}%)")
        return False
    log(f"gainer {symbol}: OI {chg:+.1f}% over {hrs:g}h [{src}] -> fresh money confirmed")
    return True


_BTC24 = {"ts": 0.0, "pct": None}          # tiny cache so we don't refetch BTC every candidate


def _btc_24h_change(r):
    """BTC's ROLLING 24h % move — Binance's FUTURES 24h ticker (priceChangePercent), i.e. the exact
    continuous rolling number the exchange shows, live. Cached ~5 min (btc24_cache_seconds) so a single
    cycle makes at most one fetch. Returns a float, or the last good value / None if briefly unavailable."""
    now = time.time()
    if _BTC24["pct"] is not None and (now - _BTC24["ts"]) < float(r.get("btc24_cache_seconds", 300)):
        return _BTC24["pct"]
    try:
        # Use Binance's FUTURES 24h ticker (priceChangePercent) — the SAME continuous rolling-24h number
        # shown on the exchange, so the gate and what you see on Binance agree exactly and update live.
        # (Earlier this used hourly CLOSED candles, which only stepped once an hour -> looked frozen and
        # lagged the market. Equivalent as a regime signal for the >=0/1/2% thresholds.)
        from data import _get, FUT_HOSTS
        tk = _get(FUT_HOSTS, "/fapi/v1/ticker/24hr", {"symbol": "BTCUSDT"})
        pct = float(tk.get("priceChangePercent"))
        if not np.isfinite(pct):
            raise ValueError("Non-finite BTC change")
        _BTC24["pct"] = pct
        _BTC24["ts"] = now
        return pct
    except Exception as e:
        log(f"BTC 24h fetch failed: {e}")
        return None                                            # expired values cannot confirm a regime


def _btc_regime_ok(r, kind):
    """TOGGLEABLE BTC rolling-24h regime gate for a whole strategy. `kind` is 'gainer' or 'special'.
    Only lets that strategy trade when BTC's trailing-24h move >= <kind>_btc24_min_pct.
      toggle:     <kind>_btc24_filter  (default OFF — set true to enable)
      threshold:  <kind>_btc24_min_pct (gainer 0%, special 1%)
    FAIL-OPEN: if BTC data can't be fetched we DON'T block trading (the OI gate is the real safety
    gate; this is a regime preference). Set btc24_fail_closed=true to block instead."""
    if not r.get(f"{kind}_btc24_filter", False):
        return True
    need = float(r.get(f"{kind}_btc24_min_pct", 0.0))
    pct = _btc_24h_change(r)
    if pct is None:
        if r.get("btc24_fail_closed", False):
            log(f"{kind}: BTC 24h unavailable -> fail-closed (skip {kind} entries this cycle)")
            return False
        log(f"{kind}: BTC 24h unavailable -> fail-open (regime filter skipped this cycle)")
        return True
    if pct < need:
        log(f"{kind}: BTC 24h {pct:+.2f}% < +{need:g}% -> hold off on new {kind} entries")
        return False
    return True


def _normal_strategy_policy(name, r):
    """PER-STRATEGY policy for the NORMAL book (config-driven, reversible):
      'on'    -> trade always (no BTC gate)          e.g. MACD, Momentum
      'stop'  -> strategy disabled, never opens      e.g. Williams %R
      'btc24' -> only when BTC rolling-24h >= normal_strategy_btc24_min_pct   e.g. Parabolic SAR, EMA Fast, TRIX
    Any strategy NOT listed in normal_strategy_filters uses normal_strategy_default (default 'on'),
    so unlisted strategies keep their current always-on behaviour until you bucket them."""
    fmap = r.get("normal_strategy_filters") or {}
    return fmap.get(name, r.get("normal_strategy_default", "on"))


def _stopped_strategies(r):
    """Strategy names whose normal policy is 'stop' — excluded from the scan/pick ENTIRELY so the bot
    never backtests, ranks, or surfaces them (no wasted evaluation, no 'disabled -> skip' log spam, no
    coin lost to a disabled top pick). NOTE: btc24-gated strategies are NOT excluded here — they're
    conditionally allowed and still scanned; the live BTC gate decides them at open time."""
    return {name for name in STRATEGIES if _normal_strategy_policy(name, r) == "stop"}


def _do_gainer_trades(t, r, state, cap_now, total_cap):
    """Fill the GAINER slots from top-10-gainers crossings that pass the 4h OI gate."""
    if not r.get("gainer_enabled", True):
        return
    cands = _read_with_watch(t, r, state, _gainer_candidates, r, state)
    free = min(int(r.get("gainer_slots", 2)) - sum(1 for v in state.values() if v.get("gainer")),
               total_cap - len(state))
    if free <= 0:
        return
    if not cands:
        return
    if not _btc_regime_ok(r, "gainer"):                 # BTC rolling-24h regime gate (toggleable, >=0%)
        return                                          # (after the list update -> no stale-crossing burst)
    _read_with_watch(t, r, state, coinalyze.prime, [c["symbol"] for c in cands],
                    r.get("gainer_oi_lookback_hours", 4))
    min_usdt = r.get("min_trade_usdt", 5.0)
    tradable = t.tradable_symbols()                     # only coins THIS exchange lists (-1121)
    for c in cands:
        if free <= 0:
            break
        sym = c["symbol"]
        if sym in state:
            continue
        if tradable and sym not in tradable:
            continue
        if not _read_with_watch(t, r, state, _gainer_oi_ok, t, sym, r):
            continue
        usdt = _open_size(t, r, cap_now)
        if usdt < min_usdt:
            log(f"gainer skip {sym}: only {usdt:.2f} USDT free (< {min_usdt} min)")
            break
        if _open_special_trade(t, sym, c["direction"], usdt, r, state, c["reason"], gainer=True):
            free -= 1


def _excluded(symbol, r):
    """True if this coin is on the do-not-trade list (majors, metals, tokenized stocks).
    Matches the base ticker, ignoring the USDT suffix and any 1000/1000000 shitcoin prefix."""
    ex = r.get("exclude_symbols") or []
    if not ex:
        return False
    up = symbol.upper()
    base = up[:-4] if up.endswith("USDT") else up
    for pre in ("1000000", "1000"):                    # 1000SHIBUSDT -> SHIB, etc.
        if base.startswith(pre) and len(base) > len(pre):
            base = base[len(pre):]
            break
    return base in {e.upper() for e in ex}


CLOSE_REQ_FILE = "close_requests.json"


def _take_close_requests():
    """Read + CLEAR the dashboard's manual-close queue. Returns a list of symbols the user
    pressed the ✕ button on. The bot owns state, so it does the actual close (race-free)."""
    try:
        if not os.path.exists(CLOSE_REQ_FILE):
            return []
        reqs = json.load(open(CLOSE_REQ_FILE)) or []
        json.dump([], open(CLOSE_REQ_FILE, "w"))       # clear once taken
        return [str(s).upper() for s in reqs]
    except Exception:
        return []


def rules():
    with open("risk_rules.json") as f:
        return json.load(f)


def load_state():
    return json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}


def save_state(s):
    with open(STATE_FILE + ".tmp", "w") as handle:
        json.dump(s, handle, indent=2)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def save_recommendations(picks):
    """Save the latest scan so the dashboard can show live suggestions (no button)."""
    try:
        keep = ("symbol", "timeframe", "strategy", "signal", "net", "confirmations",
                "leverage", "vol_spike", "volume_flag", "win", "trades", "trend")
        rows = [{k: p.get(k) for k in keep} for p in picks[:12]]
        json.dump({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "picks": rows},
                  open("recommendations.json", "w"))
    except Exception:
        pass


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:                     # Windows cp1252 console can't print some chars
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)


ACTIVITY_FILE = "activity.log"


def note(msg):
    """Plain-English diary of what the bot did, one simple sentence per line.
    Written to activity.log so a human can read exactly what happened and why."""
    try:
        with open(ACTIVITY_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:
        pass


def _coin(sym):
    """BTCUSDT -> BTC (nicer to read in the diary)."""
    return sym[:-4] if sym.endswith("USDT") else sym


def _record_close(pos, symbol, exit_price, reason, t=None):
    """Record a completed trade. The $ P&L is the ACTUAL realized figure read from the EXCHANGE
    (never computed by the bot), alongside the margin used and the % return on that margin.
    Also checks win/loss internally to feed the 24h loss-ban."""
    try:
        entry, side, lev = pos["entry"], pos["side"], pos.get("leverage", 1)
        margin = float(pos.get("size") or pos.get("margin") or 0.0)     # margin used = the slot size
        # --- ACTUAL $ P&L, straight from Binance (sum of REALIZED_PNL for this trade's window) ---
        oe = pos.get("open_epoch")
        start_ms = int((oe if oe else time.time() - 3 * 86400) * 1000)
        pnl_total, last = None, None
        if t is not None:
            for _ in range(3):                              # retry a few times: the income event can
                last = t.realized_pnl(symbol, start_ms)     # lag the fill by a second or two
                if last:                                    # non-zero -> got the real figure
                    pnl_total = last
                    break
                time.sleep(1.0)
            if pnl_total is None:
                pnl_total = last                            # genuine ~$0 close, or exchange unreachable
        price_move = (exit_price / entry - 1) * 100 * side
        # realized_pnl sums the WHOLE trade window. A gainer that booked a 50% partial already recorded
        # that $ in its own 'partial' row, so subtract it -> THIS row is only the remaining runner leg.
        # The partial row + this row are then ADDITIVE (no double-count). No partial -> booked 0 -> unchanged.
        booked = float(pos.get("realized_booked", 0.0) or 0.0)
        pnl_usd = (round(pnl_total - booked, 2) if pnl_total is not None else None)   # this leg's $ P&L
        # SANITY GUARD: on ISOLATED margin a leg can lose at most its margin. A figure far outside
        # [-margin, +5x margin] is an EXCHANGE GLITCH (e.g. a bad-price stop fill on testnet) — fall back
        # to the price-based P&L, so ONE bad print can never poison the stats. Logged + row tagged.
        if pnl_usd is not None and margin > 0 and (pnl_usd < -margin * 1.05 or pnl_usd > margin * 5):
            price_pnl = round(price_move / 100 * lev * margin, 2)                # $ implied by the price
            price_pnl = max(-round(margin, 2), min(price_pnl, round(margin * 5, 2)))
            log(f"{symbol}: exchange realized P&L {pnl_usd:.2f} is impossible for {margin:.2f} isolated "
                f"margin -> GLITCH, recording price-based {price_pnl:.2f} instead")
            pnl_usd = price_pnl
            reason = f"{reason} [glitch-corrected]"
        pnl_pct = round(pnl_usd / margin * 100, 2) if (pnl_usd is not None and margin > 0) else ""
        # win/loss for the ban = the WHOLE TRADE (total realized incl. any partial), not just this leg —
        # a trade that booked +25% then ran its remainder to break-even is a WINNER, not a loss.
        loss = (pnl_total < 0) if pnl_total is not None else ((exit_price / entry - 1) * side * lev < 0)
        if loss:
            _record_loss(symbol)
        LAST_CLOSE[symbol] = (time.time(), pos.get("timeframe", "4h"), not loss)  # re-entry cooldown + won flag
        _save_reentry()                                   # persist cooldown/loss-lockout/ban across restarts
        if pos.get("special") and str(reason).startswith("special TP"):   # +100% climax booked
            _mark_special_tp(symbol)                          # 24h lockout (both dirs), survives restarts
            log(f"{symbol}: +100% special TP -> locked out of new specials (both directions) "
                f"for the special_tp_cooldown_hours window")
        if not pos.get("special") and not pos.get("news"):    # a NORMAL trade closed -> start the rest
            LAST_NORMAL_CLOSE[0] = time.time()
        _save_reentry()
        pnl_txt = f"{pnl_usd:+.2f} USDT ({pnl_pct:+.1f}% on margin)" if pnl_usd is not None else "see exchange"
        leg_txt = f" [remaining leg — {booked:+.2f} already booked in a partial]" if booked else ""
        note(f"Closed {_coin(symbol)} ({'long' if side > 0 else 'short'}) at {exit_price:.6g}. "
             f"Reason: {reason}. Real P&L: {pnl_txt}{leg_txt}. Margin used: {margin:.0f} USDT.")
        from history import record
        record({
            "open_time": pos.get("open_time", ""),
            "close_time": time.strftime("%Y-%m-%d %H:%M"),
            "coin": symbol, "side": "LONG" if side > 0 else "SHORT",
            "type": ("gainer" if pos.get("gainer") else
                     "special" if pos.get("special") else
                     "news" if pos.get("news") else "strategy"),
            "strategy": pos.get("strategy") or ("gainer" if pos.get("gainer") else
                                                "special" if pos.get("special") else "news"),
            "timeframe": pos.get("timeframe", ""), "leverage": lev,
            "confirmations": pos.get("confirmations", ""),
            "entry": round(entry, 6), "exit": round(exit_price, 6),
            "margin_usdt": round(margin, 2),
            "price_move_%": round(price_move, 2),
            "pnl_%": pnl_pct,
            "pnl_usdt": round(pnl_usd, 2) if pnl_usd is not None else "",
            "exit_reason": reason,
        })
    except Exception as e:
        log(f"history record failed ({symbol}): {e}")
        return False
    return True


def _trail_level(pos, price, r):
    from exit_rules import normal_stop
    stop, armed, peak = normal_stop(pos, price, r)
    pos["peak"] = peak
    return stop, armed, peak


def _normal_tp_margin(pos, price, r):
    """Full take-profit for a NORMAL trade — returns the target margin% once hit, else None.
    Normal previously had NO take-profit at all: the tight trail was the only way to book a win, so a
    winner could never be large enough to pay for the losers (capped upside + open downside). Only
    active in ladder mode; set normal_target_margin_pct=0 to disable just the TP."""
    if r.get("normal_exit_mode", "trail") != "ladder":
        return None
    tgt = float(r.get("normal_target_margin_pct", 0) or 0)
    if tgt <= 0:
        return None
    lev = pos.get("leverage", 1) or 1
    margin = (price / pos["entry"] - 1) * 100 * pos["side"] * lev
    return tgt if margin >= tgt - 1e-9 else None


def _normal_stop_label(r):
    return "ladder-stop" if r.get("normal_exit_mode", "trail") == "ladder" else "trailing-stop"


def _safe_close(t, symbol, pos, reason, price, state):
    """Fix #1: CLOSE FIRST, verify the exchange is really flat, and ONLY THEN record + drop it.
    If the close is rejected (thin coin), the position is KEPT and retried next cycle — never
    logged as a phantom 'closed' trade. Returns True only if actually closed."""
    if not pos.get("close_intent"):
        pos["close_intent"] = {"reason": reason, "created": time.time()}
        save_state(state)
        note(f"Exit pending for {_coin(symbol)}: {reason}. New entries blocked until resolved.")
    intent = pos["close_intent"]
    intent.setdefault("initial_qty", pos.get("qty"))
    intent.setdefault("initial_size", pos.get("size"))
    attempts = intent.get("attempts", 0)
    if time.time() - pos.get("close_attempt_at", 0) < min(60, 2 ** min(max(attempts, 1), 6)):
        return False
    reason = intent["reason"]
    pos["close_attempt_at"] = time.time()
    intent["attempts"] = attempts + 1
    save_state(state)
    try:
        if t.force_close(symbol):
            price = t.exit_fill_price(symbol, int(pos.get("open_epoch", 0) * 1000), pos["side"], intent["initial_qty"])
            if pos.get("tp_placed") and t.take_profit_filled(symbol, int(pos.get("open_epoch", 0) * 1000), pos["side"]):
                reason = "special TP (exchange)" if pos.get("special") else "normal TP (exchange)"
            pos["qty"] = 0
            pos["stop_placed"] = False
            pos["protection_status"] = "flat"
            t.cancel_open_orders(symbol)
            # Emergency partial fills belong to the entire original final leg.
            record_pos = dict(pos, qty=intent["initial_qty"])
            if intent["initial_size"] is not None:
                record_pos["size"] = intent["initial_size"]
            if _record_close(record_pos, symbol, price, reason, t) is not False:
                state.pop(symbol, None)
                save_state(state)
                log(f"CLOSE {symbol} @ {price:.6g}  ({reason})")
                return True
            intent["last_error"] = "Exchange flat; close history write failed"
        else:
            intent["last_error"] = str(getattr(t, "last_close_error", None) or "exchange closure not confirmed")
    except Exception as exc:
        intent["last_error"] = str(exc)
        raise
    finally:
        if symbol in state:
            try:
                amt, _ = t._position_amt_entry(symbol)
                pos["qty"] = abs(amt)
                pos["quantity_checked_at"] = time.time()
                if intent["initial_qty"] and intent["initial_size"] is not None:
                    pos["size"] = intent["initial_size"] * abs(amt) / intent["initial_qty"]
            except Exception:
                pos["quantity_checked_at"] = None
            intent["manual_attention"] = intent["attempts"] >= 3
            if intent["manual_attention"] and time.time() - intent.get("alert_at", 0) >= 300:
                note(f"ACTION REQUIRED: {symbol} exit unresolved; new entries blocked. {intent.get('last_error', '')}")
                intent["alert_at"] = time.time()
            save_state(state)
    detail = intent.get("last_error", "exchange closure not confirmed")
    log(f"{symbol}: close FAILED ({reason}) - position kept; {detail}")
    return False


def _entry_safety_ok(t, state):
    if entries_paused():
        return False
    if any(p.get("close_intent") or p.get("execution_uncertain") or not p.get("stop_placed", False)
           or p.get("protection_status") in {"missing", "terminal", "unknown"} for p in state.values()):
        return False
    try:
        return not t._pending_entries()
    except Exception:
        return False


def _entry_liquidity(t, symbol, side, usdt, leverage, r):
    return t.check_entry_liquidity(symbol, side, usdt, leverage,
                                   r.get("entry_max_spread_pct", 0.5),
                                   r.get("entry_max_estimated_slippage_pct", 0.5))


def _check_protection(t, sym, pos, r, state):
    if time.time() - pos.get("stop_checked_at", 0) < r.get("stop_status_poll_seconds", 3):
        return
    try:
        status = t.stop_status(sym, pos["side"], pos["sl"], int(pos.get("open_epoch", 0) * 1000))
    except Exception as exc:
        status = {"status": "unknown", "reason": str(exc)}
    pos["protection_status"] = status["status"]
    pos["stop_checked_at"] = time.time()
    pos["stop_placed"] = status["status"] == "active"
    if status["status"] == "active":
        pos.pop("protection_unknown_since", None)
        pos["exchange_stop_id"] = status.get("id")
    elif status["status"] == "unknown":
        since = pos.setdefault("protection_unknown_since", time.time())
        if time.time() - since >= r.get("stop_unknown_timeout_seconds", 15):
            pos.setdefault("close_intent", {"reason": "stop protection cannot be verified", "created": time.time()})
    else:
        pos.setdefault("close_intent", {"reason": "exchange stop " + status["status"] + ": " + status.get("reason", ""),
                                       "created": time.time()})
    if status["status"] != "active" and time.time() - pos.get("safety_alert_at", 0) >= 60:
        note(f"SAFETY: {_coin(sym)} stop is {status['status']}. New entries blocked. {status.get('reason', '')}")
        pos["safety_alert_at"] = time.time()
    save_state(state)


def _target_price(pos, r):
    if pos.get("special"):
        lev = pos.get("leverage", 1) or 1
        margin = pos.get("target_margin", pos.get("target_pct", 20) * lev)
        return pos["entry"] * (1 + pos["side"] * margin / lev / 100) if margin > 0 else None
    if not pos.get("news"):
        return normal_target(pos, r)
    return None


def _maintain_native_target(t, r, state):
    # At most one position per pass, AFTER all urgent software exits. Never cancel its SL.
    for sym, pos in list(state.items()):
        target = _target_price(pos, r)
        if (target is None or pos.get("close_intent") or not pos.get("stop_placed")
                or time.time() - pos.get("tp_checked_at", 0) < 60):
            continue
        pos["tp_checked_at"] = time.time()
        try:
            amount, _ = t._position_amt_entry(sym)
            if amount == 0:
                _safe_close(t, sym, pos, "closed on exchange", pos["entry"], state)
                return
            if amount * pos["side"] < 0:
                raise ValueError("Exchange position side differs from tracked side")
            result = t.place_stops(sym, pos["side"], target, None)
            status = result.get("TAKE_PROFIT_MARKET", "unverified")
        except Exception as exc:
            status = f"failed: {exc}"
        pos["tp_placed"] = status == "placed"
        pos["tp_status"] = status
        if not pos["tp_placed"] and time.time() - pos.get("tp_alert_at", 0) >= 300:
            note(f"{sym}: native take-profit backstop {status}; software exits remain active.")
            pos["tp_alert_at"] = time.time()
        save_state(state)
        return


def watch_positions(t, r, state):
    started = time.time()
    _watch_positions(t, r, state)
    _maintain_native_target(t, r, state)
    try:
        record_monitor_pass(started, len(state))
    except OSError as exc:
        log(f"Exit monitor heartbeat write failed: {exc}")


def _watch_positions(t, r, state):
    """FAST loop (runs every second): live prices + trailing maths only.
    No downloads, no backtests. One API call covers every open trade.
    This is what closes a winner the moment it turns, instead of ~2.5 min later."""
    # manual closes requested from the dashboard (✕ button) — act on them first, near-instantly
    _LAST_WATCH[0] = time.monotonic()
    reqs = _take_close_requests()
    for sym in reqs:
        pos = state.get(sym)
        try:
            if pos:
                _safe_close(t, sym, pos, "manual close (dashboard)", pos["entry"], state)
            else:                                        # not tracked but maybe open on the exchange
                if t.force_close(sym):
                    t.cancel_open_orders(sym)
            log(f"MANUAL CLOSE requested for {sym}")
        except Exception as e:
            log(f"manual close {sym} failed: {e}")
    if not state:
        return
    try:
        prices = t.futures_prices_all()          # ONE request for all symbols
    except Exception:
        prices = {}                              # persistent exits do not depend on the price feed
    for sym in list(state.keys()):
        pos, price = state.get(sym), prices.get(sym)
        if not pos:
            continue
        try:
            _check_protection(t, sym, pos, r, state)
            if pos.get("close_intent"):
                _safe_close(t, sym, pos, pos["close_intent"]["reason"], price or pos["entry"], state)
                continue
        except Exception as exc:
            pos["protection_status"] = "unknown"
            save_state(state)
            log(f"safety watch {sym}: {exc}")
            if pos.get("close_intent"):
                continue
        if not price:
            continue
        if pos.get("special"):                   # special: not trailed, but 1s-check the +target% window
            try:
                _special_manage_stop(t, sym, pos, price, r, state)
            except Exception as e:
                log(f"fast special {sym}: {e}")
            continue
        try:
            old_peak = pos.get("peak", pos["entry"])
            tp = _normal_tp_margin(pos, price, r)
            if tp is not None:                   # ladder mode: full take-profit at +tgt% of margin
                _safe_close(t, sym, pos, f"normal TP +{tp:.0f}% margin", price, state)
                continue
            stop, armed, peak = _trail_level(pos, price, r)
            side = pos["side"]
            hard = pos.get("sl")                 # the -20%-of-margin disaster floor (set at entry)
            if armed and (price <= stop if side > 0 else price >= stop):
                _safe_close(t, sym, pos, _normal_stop_label(r), price, state)   # Fix #1: close then log
            # HARD FLOOR enforced IN-LOOP every 1s — independent of `armed` and of the exchange stop
            # (which can silently fail to place / -4130). Mirrors the special floor so a normal can NEVER
            # drift past -20% margin between the slow (~120s) cycles.
            elif hard is not None and (price <= hard if side > 0 else price >= hard):
                _safe_close(t, sym, pos, "hard stop -20% margin", price, state)
            elif peak != old_peak:
                save_state(state)                # remember the new high water mark
        except Exception as e:
            log(f"fast watch {sym}: {e}")


def step(t, symbol, usdt, r, state, timeframe=TIMEFRAME, candidate=None):
    """One decision cycle: manage the open trade, or look for a new one."""
    if symbol not in state and not _entry_safety_ok(t, state):
        return
    if state.get(symbol, {}).get("close_intent"):
        pos = state[symbol]
        _safe_close(t, symbol, pos, pos["close_intent"]["reason"], pos["entry"], state)
        return
    if state.get(symbol, {}).get("special"):        # special trades run their OWN lifecycle
        _manage_special(t, symbol, r, state)        # (2% hard stop + timed partial TP), not this
        return
    min_trades = r.get("min_trades_for_strategy", 20)

    df = _read_with_watch(t, r, state, get_ohlcv, symbol, timeframe, HISTORY,
                          closed_only=True, market="futures")
    price = t.futures_price(symbol)
    pos = state.get(symbol)
    if pos and pos.get("close_intent"):
        _safe_close(t, symbol, pos, pos["close_intent"]["reason"], price, state)
        return

    # If the exchange already closed us (TP/SL hit while we slept), clear state.
    live = {p["symbol"] for p in t.futures_positions()}
    if pos and symbol not in live:
        log(f"{symbol}: position already closed on exchange (TP/SL hit). Resetting.")
        _safe_close(t, symbol, pos, "closed on exchange", price, state)
        return

    if pos:  # ---- manage the open trade (TRAILING stop) ----
        side = pos["side"]
        if not pos.get("stop_placed", True):          # earlier stop-order failed -> keep retrying
            if t.place_stops(symbol, side, None, pos["sl"]).get("STOP_MARKET") == "placed":
                pos["stop_placed"] = True
                save_state(state)
                log(f"{symbol}: re-placed the missing stop-loss on the exchange")
        strat = pos.get("strategy")
        if strat and strat in STRATEGIES:
            cur = latest_signal(STRATEGIES[strat][0], df)
            flipped = cur != side and (cur != 0 or r.get("normal_zero_signal_policy", "hold") == "exit")
        else:
            flipped = False               # news trade: no strategy -> exit only on trailing stop
        old_peak = pos.get("peak", pos["entry"])
        tp = _normal_tp_margin(pos, price, r)
        if tp is not None:                      # ladder mode: full take-profit at +tgt% of margin
            _safe_close(t, symbol, pos, f"normal TP +{tp:.0f}% margin", price, state)
            return
        stop, armed, peak = _trail_level(pos, price, r)         # same maths as the fast watcher
        hit = price <= stop if side > 0 else price >= stop

        if peak != old_peak:
            save_state(state)
        if hit:                                 # armed -> ladder/trailing exit; not armed -> hard floor
            reason = _normal_stop_label(r) if armed else "hard stop -20% margin"
        else:
            reason = "signal-flip" if flipped else None
        if reason:
            _safe_close(t, symbol, pos, reason, price, state)   # Fix #1: close & verify, then log
        else:
            tag = "LONG" if side > 0 else "SHORT"
            pl = (price / pos["entry"] - 1) * 100 * side
            _mode = "ladder" if r.get("normal_exit_mode", "trail") == "ladder" else "trailing"
            log(f"HOLD {symbol} {tag} @ {price:.4f} | {_mode if armed else 'SL-only'} stop {stop:.4f} "
                f"| peak {peak:.4f} | P/L {pl:+.1f}% ({pl*pos.get('leverage',1):+.0f}% margin) "
                f"| {pos.get('strategy', 'news')}")

    else:  # ---- flat: look for a new trade ----
        if not _entry_safety_ok(t, state):
            return
        if not _can_open_now(r) or time.time() - LAST_NORMAL_CLOSE[0] < r.get("rest_after_close_min", 5) * 60:
            return
        if _excluded(symbol, r):                       # do-not-trade list (majors / metals / stocks)
            log(f"{symbol}: on the exclude list (major/metal/tokenized-stock) -> skip")
            return
        cps = crypto_perp_symbols()                    # backstop: only genuine crypto perps
        if cps and symbol not in cps:
            log(f"{symbol}: not a crypto perp (tokenized stock / metal / index / unlisted) -> skip")
            return
        if _coin_banned(symbol, r):                    # Fix #2: 2 losses in 24h -> banned 24h
            log(f"{symbol}: BANNED - {r.get('losses_before_ban', 2)} losses within "
                f"{r.get('loss_ban_hours', 24)}h -> no trading it")
            note(f"Skipped {_coin(symbol)} - it lost {r.get('losses_before_ban', 2)} times recently, "
                 f"so no trades on it for {r.get('loss_ban_hours', 24)} hours.")
            return
        if _on_cooldown(symbol, r):
            log(f"{symbol}: re-entry cooldown active (recently closed) -> skip")
            return
        # CHOP FILTER: trend strength must be high enough, else strategies whipsaw (signal flips)
        min_adx = r.get("min_adx", 0)
        if min_adx:
            try:
                cur_adx = float(adx(df).iloc[-1])
            except Exception:
                cur_adx = None                       # can't measure -> don't block the trade
            if cur_adx is None or not np.isfinite(cur_adx):
                return
            if cur_adx < min_adx:
                log(f"{symbol}: choppy market (ADX {cur_adx:.0f} < {min_adx}) -> skip")
                note(f"Skipped {_coin(symbol)} - the market is choppy, not trending "
                     f"(trend strength {cur_adx:.0f}, needs {min_adx}).")
                return
        best = _read_with_watch(t, r, state, pick_best_strategy, df, min_trades, r.get("max_backtest_return_pct", 300),
                                  exclude=_stopped_strategies(r), rules=r)
        if candidate and (str(df["time"].iloc[-1]) != candidate.get("signal_bar") or not best
                          or best["name"] != candidate["strategy"] or best["signal"] != candidate["signal"]):
            log(f"{symbol}: ranked candidate is no longer current -> skip")
            return
        if not best:
            log(f"{symbol}: no reliable+profitable strategy right now -> staying flat")
        else:
            name, fn, ret, ntr = best["name"], best["fn"], best["ret"], best["trades"]
            conf, need = best["confirmations"], r.get("min_confirmations", 3)
            parr = np.asarray(fn(df), float)
            sig = int(parr[-1])
            if sig == 0:
                log(f"{symbol}: best = {name} (+{ret:.0f}%, {ntr} trades) but signal Flat -> wait")
            elif conf < need:                          # require multi-strategy agreement
                log(f"{symbol}: {name} says {'LONG' if sig > 0 else 'SHORT'} but only {conf} "
                    f"strategies agree (need {need}) -> skip")
                note(f"Skipped {_coin(symbol)} - {name} wanted to go "
                     f"{'long' if sig > 0 else 'short'}, but only {conf} strategies agreed "
                     f"(needs at least {need}).")
            else:
                # --- PER-STRATEGY normal filter (config-driven): stop losers, gate some behind
                #     BTC's rolling-24h regime, leave the rest always-on. See _normal_strategy_policy. ---
                pol = _normal_strategy_policy(name, r)
                if pol == "stop":
                    log(f"{symbol}: {name} is disabled for normal trades (policy=stop) -> skip")
                    note(f"Skipped {_coin(symbol)} - the {name} strategy is turned off.")
                    return
                if pol == "btc24":
                    need_btc = float(r.get("normal_strategy_btc24_min_pct", 0.0))
                    pct = _btc_24h_change(r)
                    if pct is None:
                        if r.get("btc24_fail_closed", False):
                            log(f"{symbol}: {name} needs BTC 24h (unavailable) -> fail-closed skip")
                            return
                        log(f"{symbol}: {name} BTC 24h unavailable -> fail-open (gate skipped)")
                    elif pct < need_btc:
                        log(f"{symbol}: {name} gated -> BTC 24h {pct:+.2f}% < +{need_btc:g}% -> skip")
                        note(f"Skipped {_coin(symbol)} - {name} only trades when Bitcoin's 24h move is "
                             f"above {need_btc:g}% (it's {pct:+.1f}% now).")
                        return
                if r.get("normal_trend_filter", False):
                    htf = _HIGHER_TF.get(timeframe, "1d")
                    try:
                        trend = higher_tf_trend(_read_with_watch(t, r, state, get_ohlcv, symbol, htf, HISTORY,
                                                                closed_only=True, market="futures"))
                    except Exception:
                        return                             # an enabled filter needs valid data
                    if sig != trend:
                        log(f"{symbol}: {name} {'LONG' if sig > 0 else 'SHORT'} but {htf} trend is "
                            f"{'UP' if trend > 0 else 'DOWN'} -> skip (counter-trend)")
                        note(f"Skipped {_coin(symbol)} - {name} wanted to go "
                             f"{'long' if sig > 0 else 'short'}, but the bigger {htf} trend is "
                             f"{'up' if trend > 0 else 'down'}, so it would be fighting the trend.")
                        return
                if r.get("normal_require_oi_rising", False):   # OI-rising gate (fresh money behind the move)
                    change, source = _read_with_watch(t, r, state, coinalyze.oi_change,
                        symbol, float(r.get("oi_gate_lookback_min", 45)) / 60, t)
                    rising = None if change is None else change >= r.get("oi_gate_pct", 2.0)
                    log(f"{symbol}: normal OI source={source}, change={change}")
                    if rising is None and r.get("oi_fail_closed", True):   # data unavailable -> FAIL-CLOSED
                        log(f"{symbol}: OI data unavailable after retry -> skip "
                            f"(fail-closed: no OI confirmation, no trade)")
                        note(f"Skipped {_coin(symbol)} - couldn't confirm open interest (data unavailable), "
                             f"and we never trade without OI confirmation.")
                        return
                    if rising is False:
                        log(f"{symbol}: OI not rising >= {r.get('oi_gate_pct', 2.0)}% in "
                            f"{r.get('oi_gate_lookback_min', 45)}m -> skip (no fresh money)")
                        note(f"Skipped {_coin(symbol)} - open interest isn't rising, so there's no fresh "
                             f"money confirming the move (OI-quality filter).")
                        return
                cap = r.get("max_per_strategy", 2)     # risk cap: max N open normal trades per strategy
                same = sum(1 for v in state.values()
                           if not v.get("special") and not v.get("news") and v.get("strategy") == name)
                if same >= cap:
                    log(f"{symbol}: {name} already has {same} open normal trade(s) (cap {cap}) -> skip")
                    note(f"Skipped {_coin(symbol)} - the {name} strategy already has {same} trade(s) open "
                         f"(limit {cap} per strategy, so one strategy can't dominate the book).")
                    return
                if symbol in state:            # ONE open trade per coin across ALL strategies
                    log(f"skip {symbol}: a trade is already open on this coin (one-per-coin guard)")
                    return
                leverage = _read_with_watch(t, r, state, auto_leverage, df, parr,
                                         {**r, "max_leverage": r.get("normal_max_leverage", 1)})
                if leverage is None:
                    return
                # LEVERAGE FLOOR: the exit ladder/floor are margin-based, so at 1-2x a normal trade
                # needed a 70-76% win rate just to break even (live: 54-62%) — the two worst buckets.
                # Clamp into [normal_leverage_min, normal_max_leverage]; set min=1 to revert.
                _lmin = int(r.get("normal_leverage_min", 1))
                _lmax = int(r.get("normal_max_leverage", 5))
                leverage = max(_lmin, min(int(leverage), _lmax))
                guards.check_order(symbol, usdt, leverage)
                _, sl, _, slp = auto_tp_sl(df, sig, price, r, leverage)
                _read_with_watch(t, r, state, _entry_liquidity, t, symbol, sig, usdt, leverage, r)
                if (not _entry_safety_ok(t, state) or symbol in state or _on_cooldown(symbol, r) or not _can_open_now(r)
                        or time.time() - LAST_NORMAL_CLOSE[0] < r.get("rest_after_close_min", 5) * 60):
                    return
                res = t.futures_market_order(symbol, "BUY" if sig > 0 else "SELL", usdt, leverage,
                                             split=r.get("order_chunking_enabled", True))
                size = _filled_margin(res, usdt)               # REAL margin deployed (post-chunk fill)
                entry = _fill_entry(res, price)                # REAL average fill (= Binance entryPrice)
                sl = entry * (1 - slp / 100) if sig > 0 else entry * (1 + slp / 100)   # keep the stop distance off the true entry
                state[symbol] = {"side": sig, "entry": entry, "sl": sl, "peak": entry, "qty": res.get("filled_qty"),
                                 "trail_pct": slp, "strategy": name, "timeframe": timeframe,
                                 "leverage": leverage, "size": size, "open_time": time.strftime("%Y-%m-%d %H:%M"),
                                 "open_epoch": time.time(),
                                 "expected_return": round(ret, 1), "confirmations": conf,
                                 "stop_placed": False}
                save_state(state)
                stops = t.place_stops(symbol, sig, _target_price(state[symbol], r), sl)
                state[symbol]["stop_placed"] = stops.get("STOP_MARKET") == "placed"
                state[symbol]["tp_placed"] = stops.get("TAKE_PROFIT_MARKET") == "placed"
                state[symbol]["tp_status"] = stops.get("TAKE_PROFIT_MARKET", "not configured")
                state[symbol]["tp_checked_at"] = time.time()
                save_state(state)
                RECENT_OPENS.append(time.time())
                _save_reentry()
                cap_note = f" (intended {usdt})" if abs(size - usdt) > 0.01 else ""
                _ladder_on = r.get("normal_exit_mode", "trail") == "ladder"
                _lad = r.get("normal_stop_ladder", [[25, 0], [50, 25], [75, 50]])
                _tgt = r.get("normal_target_margin_pct", 100)
                _exit_txt = (f"ladder {', '.join(f'+{a:g}->' + ('BE' if b == 0 else f'+{b:g}') for a, b in _lad)}"
                             f" | TP +{_tgt:g}% margin" if _ladder_on else f"trailing {slp}%")
                log(f"OPEN {symbol} {'LONG' if sig > 0 else 'SHORT'} {size}USDT{cap_note} {leverage}x(auto) "
                    f"via {name} (+{ret:.0f}%, {ntr} trades, {conf} agree) | entry {entry:.4f} "
                    f"SL {sl:.4f}(-{slp}%) | {_exit_txt} | stops: {stops}")
                _exit_note = ((f"As profit grows the stop ladders up: "
                               f"{', '.join(f'+{a:g}% -> ' + ('break-even' if b == 0 else f'+{b:g}%') for a, b in _lad)} "
                               f"(all on margin), and it books the whole trade at +{_tgt:g}% profit on margin.")
                              if _ladder_on else
                              f"A {slp}% trailing stop follows the price up once the trade is in profit.")
                note(f"Opened a {'LONG (betting up)' if sig > 0 else 'SHORT (betting down)'} trade on "
                     f"{_coin(symbol)} at {entry:.6g} with {size} USDT. Chose the {name} strategy "
                     f"because it scored best in the backtest (+{ret:.0f}%), and {conf} strategies "
                     f"agreed on this direction. Safety stop-loss placed at {sl:.6g} ({slp}% away). "
                     f"{_exit_note}")


def _open_news_trade(t, symbol, direction, usdt, r, state, headline):
    """Open a trade purely on a news signal (no strategy). Uses its own extra slot."""
    if _excluded(symbol, r) or not r.get("news_enabled", False) or not _can_open_now(r) or symbol in state or not _entry_safety_ok(t, state):
        return False
    try:
        leverage = min(r.get("news_leverage", 2), r.get("max_leverage", 5))
        guards.check_order(symbol, usdt, leverage)
        price = t.futures_price(symbol)
        sl_pct = r.get("news_stop_loss_pct", 3.0)
        _read_with_watch(t, r, state, _entry_liquidity, t, symbol, direction, usdt, leverage, r)
        if not _entry_safety_ok(t, state) or symbol in state or not _can_open_now(r):
            return False
        res = t.futures_market_order(symbol, "BUY" if direction > 0 else "SELL", usdt, leverage,
                                     split=r.get("order_chunking_enabled", True))
        size = _filled_margin(res, usdt)                            # REAL margin deployed (post-chunk fill)
        entry = _fill_entry(res, price)                            # REAL average fill (= Binance entryPrice)
        sl = entry * (1 - sl_pct / 100) if direction > 0 else entry * (1 + sl_pct / 100)
        state[symbol] = {"side": direction, "entry": entry, "sl": sl, "peak": entry, "qty": res.get("filled_qty"),
                         "trail_pct": sl_pct, "news": True, "headline": headline,
                         "leverage": leverage, "size": size, "timeframe": "4h",
                         "open_time": time.strftime("%Y-%m-%d %H:%M"), "open_epoch": time.time(),
                         "stop_placed": False}
        save_state(state)
        stops = t.place_stops(symbol, direction, None, sl)
        state[symbol]["stop_placed"] = stops.get("STOP_MARKET") == "placed"
        save_state(state)
        RECENT_OPENS.append(time.time())
        _save_reentry()
        log(f"NEWS TRADE {symbol} {'LONG' if direction > 0 else 'SHORT'} {size}USDT {leverage}x | {headline}")
        note(f"Opened a NEWS trade: {'LONG' if direction > 0 else 'SHORT'} on {_coin(symbol)} at "
             f"{entry:.6g}, triggered by the headline: \"{headline}\". Stop-loss at {sl:.6g}.")
        return True
    except guards.RiskError as e:
        log(f"news trade blocked ({symbol}): {e}")
    except Exception as e:
        log(f"news trade error ({symbol}): {e}")
    return False


def _do_news_trades(t, r, state, usdt, seen, total_cap):
    """Check news; open news trades in their OWN slot(s), within the total position cap."""
    free = min(r.get("news_slots", 1) - sum(1 for v in state.values() if v.get("news")),
               total_cap - len(state))
    if free <= 0:
        return
    try:
        from news import get_news_signals
        signals = get_news_signals(max_age_min=r.get("news_max_age_min", 30),
                                   min_sentiment=r.get("news_min_sentiment", 1))
    except Exception as e:
        log(f"news check failed: {e}")
        return
    if len(seen) > 1000:
        seen.clear()
    for ns in signals:
        if free <= 0:
            break
        if ns["id"] in seen:
            continue
        seen.add(ns["id"])
        if ns["symbol"] in state:            # already trading this coin
            continue
        if _coin_banned(ns["symbol"], r):
            continue                         # banned after repeated losses
        if _on_cooldown(ns["symbol"], r):
            continue                         # re-entry cooldown active
        if _open_news_trade(t, ns["symbol"], ns["direction"], usdt, r, state, ns["headline"]):
            free -= 1


# ======================= SPECIAL (momentum / news) trades =======================
# A separate breed of trade with its OWN lifecycle (the normal strategy/trailing rules do
# NOT apply):
#   entry  : fresh strong news OR a volume spike + short-term momentum (see _special_candidates)
#   size   : same 10%-of-capital slot, but 5-10x leverage
#   stop   : a HARD stop = -20% of margin, placed as a REAL exchange order (RULE 2)
#   profit : at +50% profit on margin -> move the stop to BREAK-EVEN (one-time, no partial);
#            at +100% profit on margin -> CLOSE 100% of the trade (full take-profit).
#            No partials, no runner. A safety max-hold still applies as a backstop.
# NOTE: "crypto chatrooms / Binance Square" is NOT wired in — there is no free live or historical
# feed for it here. News + volume-spike momentum are the available stand-ins; a Square/X source can
# be bolted on later exactly like news.py (see its header note).

def _record_partial(pos, symbol, exit_price, fraction, reason):
    """Log + record a partial profit-take (special trades bank 50%/75% and let the rest run)."""
    try:
        from history import record
        entry, side, lev = pos["entry"], pos["side"], pos.get("leverage", 1)
        move = (exit_price / entry - 1) * 100 * side
        pnl = round(move * lev, 2)
        booked = round(fraction * pos.get("size", 100) * pnl / 100, 2)   # $ realized by this partial
        note(f"Banked {fraction*100:.0f}% of the {_coin(symbol)} special trade at {exit_price:.6g} "
             f"- {pnl:+.2f}% on the part closed "
             f"({'target hit' if reason == 'target' else '24h deadline'}). "
             f"Letting the remaining {100 - fraction*100:.0f}% run.")
        record({
            "open_time": pos.get("open_time", ""),
            "close_time": time.strftime("%Y-%m-%d %H:%M"),
            "coin": symbol, "side": "LONG" if side > 0 else "SHORT",
            "type": "gainer-partial" if pos.get("gainer") else "special-partial",
            "strategy": "gainer" if pos.get("gainer") else "special",
            "timeframe": pos.get("timeframe", "1h"),
            "leverage": lev, "confirmations": f"{fraction*100:.0f}%",
            "expected_return_%": pos.get("target_pct", ""),
            "entry": round(entry, 6), "exit": round(exit_price, 6),
            "price_move_%": round(move, 2), "pnl_%": pnl,
            "pnl_usdt": booked,                                # only the % booked (this leg)
            "exit_reason": f"partial-{reason}",
        })
        return booked                                          # caller records it so the final row can subtract it
    except Exception as e:
        log(f"partial record failed ({symbol}): {e}")
        raise


def _special_candidates(r, seen, state):
    """Coins likely to move a lot soon: strong fresh news, or a volume spike with momentum.
    A LIQUIDITY FILTER then drops thin coins (where the stop slips) — the backtest showed this
    flips special from a net loser to a winner."""
    out, picked = [], set()
    try:
        from data import quote_volumes
        liq = quote_volumes()
    except Exception:
        liq = {}
    min_qv = r.get("special_min_volume", 100_000_000)   # only trade coins >= this 24h $volume
    # 1) news-driven (a higher sentiment bar than the single news slot uses)
    try:
        from news import get_news_signals
        news_signals = (get_news_signals(max_age_min=r.get("special_news_max_age_min", 60),
                                       min_sentiment=r.get("special_news_min_sentiment", 2))
                        if r.get("special_news_enabled", r.get("news_enabled", False)) else [])
        for ns in news_signals:
            if ns["id"] in seen or ns["symbol"] in picked:
                continue
            seen.add(ns["id"]); picked.add(ns["symbol"])
            out.append({"symbol": ns["symbol"], "direction": ns["direction"],
                        "reason": f"news: {ns['headline']}"})
    except Exception as e:
        log(f"special news scan failed: {e}")
    # 2) volume-spike movers (direction = short-term momentum)
    try:
        from data import high_volatility_coins, get_ohlcv
        movers = high_volatility_coins(n=r.get("special_scan_movers", 15),
                                       min_quote_volume=min_qv)
        for sym in movers:
            if sym in picked or sym in state:
                continue
            try:
                df = get_ohlcv(sym, "1h", 30, closed_only=True, market="futures")
            except Exception:
                continue
            if df is None or len(df) < 6:
                continue
            vols = df["volume"].values
            base = vols[-6:-1].mean()
            spike = vols[-1] / base if base > 0 else 0
            if spike < r.get("special_vol_spike", 2.0):
                continue
            mom = float(df["close"].iloc[-1] / df["close"].iloc[-4] - 1)   # last ~3h momentum
            picked.add(sym)
            out.append({"symbol": sym, "direction": 1 if mom >= 0 else -1,
                        "reason": f"volume spike x{spike:.1f}, {mom*100:+.1f}% in 3h"})
    except Exception as e:
        log(f"special volume scan failed: {e}")
    # LIQUIDITY FILTER: skip thin coins (stop slips there); also skip cooldown / repeated losers.
    # If the volume feed is unavailable (empty liq map), don't block — fall back to no filter.
    cps = crypto_perp_symbols()                          # only genuine crypto perps (no TradFi/metals)
    return [o for o in out
            if liq.get(o["symbol"], 0) >= min_qv        # Fix #3: require >= 50M 24h volume (no bypass)
            and (not cps or o["symbol"] in cps)          # drop tokenized stocks / metals / indices
            and not _excluded(o["symbol"], r)            # do-not-trade list (majors / metals / stocks)
            and not _coin_banned(o["symbol"], r)         # Fix #2: 2 losses in 24h -> banned
            and not _on_cooldown(o["symbol"], r)         # short (~60m) re-entry cooldown
            and not _special_tp_locked(o["symbol"], r)]  # 24h lockout after a +100% TP (both dirs)


def _special_leverage(symbol, r):
    """Auto-pick leverage in the 5-10x band from the coin's recent hourly volatility:
    a calm/liquid coin gets the max (10x); a jumpy/thin coin gets the min (5x), so a 2% stop
    never erodes more than intended. Falls back to the MIN (safest) if it can't measure."""
    lo = int(r.get("special_leverage_min", 5))
    hi = int(r.get("special_leverage_max", 10))
    try:
        df = get_ohlcv(symbol, "1h", 48, closed_only=True, market="futures")
        vol = float(df["close"].pct_change().tail(24).std() * 100)   # % hourly volatility
    except Exception:
        return lo
    calm = r.get("special_vol_calm_pct", 1.0)      # <= this % -> max leverage (calm coin)
    wild = r.get("special_vol_wild_pct", 3.0)      # >= this % -> min leverage (jumpy coin)
    if vol <= calm:
        return hi
    if vol >= wild:
        return lo
    frac = (vol - calm) / (wild - calm)                              # scale linearly in between
    return int(round(hi - frac * (hi - lo)))


def _open_special_trade(t, symbol, direction, usdt, r, state, reason, gainer=False):
    """Opens a special OR a gainer trade. A gainer is marked special=True too, so it reuses the
    whole special lifecycle (ladder stop, hard floor, fast watcher, max-hold) unchanged — the only
    difference is the extra 50%-at-first-rung partial TP handled in _special_manage_stop."""
    if _excluded(symbol, r) or not _entry_safety_ok(t, state) or not _can_open_now(r) or _coin_banned(symbol, r) or _on_cooldown(symbol, r):
        return False
    if symbol in state:                          # ONE open trade per coin across ALL strategies
        log(f"skip {symbol}: a trade is already open on this coin (one-per-coin guard)")
        return False
    try:
        lev = _read_with_watch(t, r, state, _special_leverage, symbol, r)
        guards.check_order(symbol, usdt, lev)
        price = t.futures_price(symbol)
        # MARGIN-based: stop = hard floor % of margin, target = +100% of margin, at ANY leverage.
        # In price terms that's (margin% / leverage): e.g. 10%/5x = 2% price, 100%/5x = 20% price.
        # PER-TYPE floor: gainer keeps 20% (its stop-sweep was noisy), special tightened (validated).
        stop_margin = r.get("gainer_stop_margin_pct", 20.0) if gainer else r.get("special_stop_margin_pct", 20.0)
        tgt_margin = r.get("special_target_margin_pct", 100.0)
        sl_pct = stop_margin / lev                                  # price move for a -20% margin loss
        tgt_pct = tgt_margin / lev                                  # price move for a +100% margin gain (full close)
        _read_with_watch(t, r, state, _entry_liquidity, t, symbol, direction, usdt, lev, r)
        if not _entry_safety_ok(t, state) or symbol in state or not _can_open_now(r) or _on_cooldown(symbol, r):
            return False
        res = t.futures_market_order(symbol, "BUY" if direction > 0 else "SELL", usdt, lev,
                                     split=r.get("order_chunking_enabled", True))
        size = _filled_margin(res, usdt)                           # REAL margin deployed (post-chunk fill)
        entry = _fill_entry(res, price)                            # REAL average fill (= Binance entryPrice)
        # stop is measured off the ACTUAL average entry, so -20% margin is exactly -20% of margin
        sl = entry * (1 - sl_pct / 100) if direction > 0 else entry * (1 + sl_pct / 100)
        state[symbol] = {"side": direction, "entry": entry, "sl": sl, "peak": entry, "qty": res.get("filled_qty"),
                         "special": True, "leverage": lev, "size": size, "timeframe": "1h",
                         "target_pct": tgt_pct, "target_margin": tgt_margin,
                         "peak_margin": 0.0, "rung": 0,
                         "open_epoch": time.time(), "open_time": time.strftime("%Y-%m-%d %H:%M"),
                         "reason": reason[:160],
                         "stop_placed": False}
        if gainer:                                      # gainer-only: bank half at the first rung
            state[symbol].update({
                "gainer": True,
                "partial_at": float(r.get("gainer_partial_at_margin", 25.0)),
                "partial_frac": float(r.get("gainer_partial_frac", 0.5)),
                "partial_done": False,
                "size_original": size,
            })
        save_state(state)
        stops = t.place_stops(symbol, direction, _target_price(state[symbol], r), sl)
        state[symbol]["stop_placed"] = stops.get("STOP_MARKET") == "placed"
        state[symbol]["tp_placed"] = stops.get("TAKE_PROFIT_MARKET") == "placed"
        state[symbol]["tp_status"] = stops.get("TAKE_PROFIT_MARKET", "unverified")
        state[symbol]["tp_checked_at"] = time.time()
        save_state(state)
        RECENT_OPENS.append(time.time())
        _save_reentry()
        ladder = r.get("special_stop_ladder", [[25, 0], [50, 25], [75, 50]])   # build the text FROM config
        ladder_txt = ", ".join(f"{tr:g}->{'BE' if sl == 0 else format(sl, 'g')}" for tr, sl in ladder)
        ladder_note = ", ".join(f"+{tr:g}%->{'break-even' if sl == 0 else f'+{sl:g}%'}" for tr, sl in ladder)
        kind = "GAINER" if gainer else "SPECIAL"
        p_txt = (f" | partial {r.get('gainer_partial_frac', 0.5)*100:.0f}% at "
                 f"+{r.get('gainer_partial_at_margin', 25.0):.0f}% margin") if gainer else ""
        cap_note = f" (intended {usdt})" if abs(size - usdt) > 0.01 else ""
        log(f"{kind} {symbol} {'LONG' if direction > 0 else 'SHORT'} {size}USDT{cap_note} {lev}x | "
            f"hard stop -{sl_pct:.1f}%px (=-{stop_margin:.0f}% margin) @ {sl:.6g} | "
            f"ladder {ladder_txt}{p_txt} | full TP +{tgt_margin:.0f}% margin | {reason[:50]}")
        p_note = (f"Half the position is banked automatically at "
                  f"+{r.get('gainer_partial_at_margin', 25.0):.0f}% profit on margin, and the rest runs. "
                  ) if gainer else ""
        note(f"Opened a {kind} {'LONG' if direction > 0 else 'SHORT'} trade on {_coin(symbol)} at "
             f"{entry:.6g} ({lev}x), because - {reason}. Hard stop -{stop_margin:.0f}% of margin. {p_note}"
             f"As profit grows the stop ladders up: {ladder_note}, and it "
             f"books the whole trade at +{tgt_margin:.0f}% profit on margin.")
        return True
    except guards.RiskError as e:
        log(f"special trade blocked ({symbol}): {e}")
    except Exception as e:
        log(f"special trade error ({symbol}): {e}")
    return False


def _do_special_trades(t, r, state, cap_now, seen, total_cap):
    """Fill the special slots from news / volume-spike momentum, within the total cap. Specials are
    funded FIRST (before normals), each at 10% of capital capped by whatever free margin remains."""
    free = min(r.get("special_slots", 2) - sum(1 for v in state.values()
                                               if v.get("special") and not v.get("gainer")),
               total_cap - len(state))          # gainers have their OWN slots, don't count here
    if free <= 0:
        return
    if not _btc_regime_ok(r, "special"):        # BTC rolling-24h regime gate (toggleable, >=1%)
        return
    if len(seen) > 1000:
        seen.clear()
    min_usdt = r.get("min_trade_usdt", 5.0)
    tradable = t.tradable_symbols()              # only coins the CURRENT exchange lists (testnet now, live later)
    cands = _read_with_watch(t, r, state, _special_candidates, r, seen, state)
    _read_with_watch(t, r, state, coinalyze.prime, [c["symbol"] for c in cands],
                    r.get("special_oi_lookback_hours", 4))
    for c in cands:
        if free <= 0:
            break
        if c["symbol"] in state:                 # never double-up on a coin
            continue
        if r.get("special_long_only", True) and c["direction"] < 0:   # specials are LONG-ONLY:
            log(f"special skip {c['symbol']}: SHORT (special is long-only)")   # shorts lost in every
            continue                                                  # analysis (live + 60d OOS)
        if tradable and c["symbol"] not in tradable:   # not listed on this exchange -> would -1121; skip
            continue
        if r.get("special_require_oi_rising", False):   # OI gate — fresh money must back the move
            # Preserve the special lookback while preferring Coinalyze.
            hrs = float(r.get("special_oi_lookback_hours", 4))
            need = float(r.get("special_oi_pct", 2.0))
            chg, src = _read_with_watch(t, r, state, coinalyze.oi_change, c["symbol"], hrs, t)
            if chg is None:                             # both Coinalyze AND Binance failed
                if r.get("special_oi_fail_closed", r.get("oi_fail_closed", True)):
                    log(f"special skip {c['symbol']}: OI unavailable (Coinalyze+Binance both failed) -> fail-closed")
                    continue
            elif chg < need:
                log(f"special skip {c['symbol']}: OI {chg:+.1f}% over {hrs:g}h [{src}] (needs >= +{need:g}%)")
                continue
            else:
                log(f"special {c['symbol']}: OI {chg:+.1f}% over {hrs:g}h [{src}] -> fresh money confirmed")
        usdt = _open_size(t, r, cap_now)         # 10% of capital, capped by free margin
        if usdt < min_usdt:                      # nothing left even for a priority special
            log(f"special skip {c['symbol']}: only {usdt:.2f} USDT free (< {min_usdt} min)")
            break
        if _open_special_trade(t, c["symbol"], c["direction"], usdt, r, state, c["reason"]):
            free -= 1


def _special_set_stop(t, symbol, pos, state, sl_price, label):
    """Push the REAL exchange stop up to sl_price — a backstop for when the bot is OFFLINE. Verifies
    the order was actually accepted (unlike the old code, which assumed success). If it fails, the
    in-loop ladder guard still protects the trade while the bot is up, and the next rung retries."""
    ok = False
    try:
        t.cancel_open_orders(symbol)
        res = t.place_stops(symbol, pos["side"], None, sl_price)
        ok = "placed" in str(res)
        if not ok:
            log(f"SPECIAL {symbol}: exchange stop move to {label} FAILED ({res}) — in-loop guard active, will retry")
    except Exception as e:
        log(f"SPECIAL {symbol}: exchange stop move to {label} error ({e}) — in-loop guard active")
    if ok:
        pos["sl"] = sl_price
        save_state(state)
    return ok


def _special_manage_stop(t, symbol, pos, price, r, state):
    """Ladder stop-loss + full take-profit for a special trade. Runs every 1s (fast watcher) AND every
    slow cycle. Returns True if it CLOSED the trade.

    Ladder — as PEAK profit-on-margin climbs, the stop ratchets UP (never down):
        peak +25% -> stop break-even | +50% -> +25% | +75% -> +50% | +100% -> full close.
    Below the first rung, the -20%-of-margin hard stop (a real exchange order) is the floor.

    The stop is ENFORCED IN-LOOP — the bot closes the trade itself the moment price crosses the rung.
    This is the fix for the old bug: a cancel/replace of an exchange order could silently fail and
    leave the position unprotected. The original exchange hard stop remains the offline backstop."""
    if pos.get("close_intent"):
        return _safe_close(t, symbol, pos, pos["close_intent"]["reason"], price, state)
    lev = pos.get("leverage", 1)
    move = (price / pos["entry"] - 1) * 100 * pos["side"]            # price move in our favour (%)
    margin = move * lev                                              # profit ON MARGIN (%)
    prev_pm = pos.get("peak_margin")
    peak = margin if prev_pm is None else max(prev_pm, margin)
    pos["peak_margin"] = peak
    if prev_pm is None or peak > prev_pm:        # persist EVERY new high water mark (not just at rungs),
        save_state(state)                        # so the dashboard 'peak on margin' is always accurate
    target_margin = pos.get("target_margin", pos.get("target_pct", 20) * lev)
    # 1a) GAINER-ONLY partial TP: bank half the position at the FIRST ladder rung. Validated as
    #     expectancy-neutral but ~20% less volatile, so it compounds better and roughly doubles the
    #     win rate (round-trips that used to end at break-even now end positive). Specials skip this.
    if (pos.get("gainer") and not pos.get("partial_done") and not pos.get("execution_uncertain")
            and margin >= float(pos.get("partial_at", 25.0)) - 1e-9):
        frac = float(pos.get("partial_frac", 0.5))
        try:
            if pos.get("partial_remaining") is not None and pos.get("qty"):
                frac = min(1.0, pos["partial_remaining"] / pos["qty"])
            res = t.partial_close(symbol, frac)
            filled = float(res.get("partial_closed_qty", 0))
            if filled <= 0:
                raise ValueError("No partial fill confirmed")
            frac = float(res["fraction_filled"])
            booked = _record_partial(pos, symbol, float(res["avg_price"]), frac, "gainer-first-rung")
            pos["partial_done"] = filled >= float(res["intended_qty"]) - 1e-10
            pos["partial_remaining"] = max(0.0, float(res["intended_qty"]) - filled)
            pos["qty"] = float(res["remaining_qty"])
            pos["realized_booked"] = round(float(pos.get("realized_booked", 0.0) or 0.0)
                                           + float(booked or 0.0), 2)   # so the final row subtracts it (no double-count)
            pos["size"] = pos.get("size", 0) * (1 - frac)     # only the remainder is still at risk
            save_state(state)
            log(f"GAINER {symbol}: +{margin:.0f}% margin -> banked {frac*100:.0f}% of the position ({res})")
        except Exception as e:
            pos["execution_uncertain"] = True
            save_state(state)
            log(f"GAINER {symbol}: partial close failed ({e}) — will retry next cycle")
    # 1) full take-profit at +100% of margin
    if margin >= target_margin - 1e-9:
        if _safe_close(t, symbol, pos, f"special TP +{target_margin:.0f}% margin", price, state):
            log(f"SPECIAL {symbol}: +{margin:.0f}% margin hit -> closed 100% of the trade")
            return True
        return False
    # 2) current ladder stop (in margin) = the highest rung the PEAK has reached
    ladder = r.get("special_stop_ladder", [[25, 0], [50, 25], [75, 50]])
    stop_margin, rung = None, 0
    for i, (trig, sl) in enumerate(ladder, start=1):
        if peak >= trig - 1e-9:
            stop_margin, rung = sl, i
    # 3) a NEW rung reached -> the ladder floor is enforced IN-LOOP (section 4) by a reduceOnly market
    #    close. We deliberately do NOT move the exchange stop each rung: replacing a closePosition stop
    #    fails on testnet with -4130 (and the cancel/replace could leave the position stopless). The
    #    ORIGINAL -20% floor stop placed at entry stays untouched as the OFFLINE backstop; the 1s in-loop
    #    guard closes at the laddered level while the bot is up.
    if rung > pos.get("rung", 0):
        pos["rung"] = rung
        save_state(state)
        lbl = "break-even" if stop_margin == 0 else f"+{stop_margin:.0f}% margin"
        log(f"SPECIAL {symbol}: peak +{peak:.0f}% margin -> ladder stop now {lbl} (enforced in-loop)")
        note(f"{_coin(symbol)} special reached +{peak:.0f}% profit on margin -> stop laddered up to {lbl}.")
    # 3b) GAINER-ONLY early time-cut: a gainer still underwater AND unprotected (never reached the
    #     first +25% rung -> rung 0, no partial banked) after N minutes is very likely a floor loss
    #     (validated: losers reveal themselves fast; 45m/-5% -> 85% precision). Exit early instead of
    #     riding to the -20% floor. Only before the first rung, so a trade that already banked its
    #     partial is never touched. Reversible via gainer_underwater_cut_enabled.
    if pos.get("gainer") and rung == 0 and not pos.get("partial_done") \
            and r.get("gainer_underwater_cut_enabled", True):
        cut_min = float(r.get("gainer_underwater_cut_min", 45))
        cut_pct = float(r.get("gainer_underwater_cut_margin_pct", 5.0))
        age_min = (time.time() - pos.get("open_epoch", time.time())) / 60.0
        if age_min >= cut_min and margin <= -cut_pct:
            # Label the ACTUAL hold time as well as the rule. cut_min is a MINIMUM AGE, not a scheduled
            # exit: past it the trade is closed the moment it is <= -cut_pct, which can be much later
            # (METIS ran 144m because it was +3% at its 45m checkpoint). Showing only the threshold made
            # the dashboard look inconsistent, and it read the CURRENT config, so a trade that spanned a
            # config change was labelled with a rule that never governed it.
            _h, _m = divmod(int(age_min), 60)
            _held = f"{_h}h{_m:02d}m" if _h else f"{_m}m"
            reason = (f"gainer underwater time-cut (held {_held}, rule >={cut_min:.0f}m, "
                      f"{margin:+.0f}% margin)")
            if _safe_close(t, symbol, pos, reason, price, state):
                log(f"GAINER {symbol}: still {margin:+.0f}% margin after {age_min:.0f}m "
                    f"(<= -{cut_pct:.0f}%, no rung) -> early time-cut close")
                return True
    # 4) ENFORCE the stop IN-LOOP (reliable; independent of the exchange order, which can silently fail
    #    to place on thin coins / -4130). CRITICAL: this now includes the HARD FLOOR at rung 0 —
    #    the bot itself closes the trade, so a missing/failed exchange stop can NEVER let a special run
    #    to liquidation again (the TSTUSDT -99% bug). Runs every 1s in the fast watcher.
    hard = r.get("gainer_stop_margin_pct", 20.0) if pos.get("gainer") else r.get("special_stop_margin_pct", 20.0)
    floor = stop_margin if rung > 0 else -hard
    if margin <= floor + 1e-9:
        lbl = f"+{floor:.0f}% margin (ladder)" if rung > 0 else f"-{hard:.0f}% margin (hard floor)"
        if _safe_close(t, symbol, pos, f"special stop {lbl}", price, state):
            log(f"SPECIAL {symbol}: fell to {margin:+.0f}% margin (<= {floor:+.0f}%) -> closed IN-LOOP ({lbl})")
            return True
    return False


def _manage_special(t, symbol, r, state):
    """One special-trade cycle (RULE 2 + breakeven-at-50%): full take-profit at +100% margin,
    move the stop to break-even once +50% margin is reached, hard -20% margin stop before that,
    and a safety max-hold backstop. No partials, no runner."""
    pos = state.get(symbol)
    if not pos:
        return
    side, entry, lev = pos["side"], pos["entry"], pos.get("leverage", 1)
    try:
        price = t.futures_price(symbol)
    except Exception as e:
        log(f"special {symbol}: price fetch failed ({e})")
        return
    # exchange may have closed us (-20% stop / breakeven stop filled, or a manual close)
    live = {p["symbol"] for p in t.futures_positions()}
    if symbol not in live:
        _safe_close(t, symbol, pos, "closed on exchange", price, state)
        return

    move = (price / entry - 1) * 100 * side           # price move in our favour (%)
    age_h = (time.time() - pos.get("open_epoch", time.time())) / 3600.0

    # ladder stop (in-loop) + full close at +100% margin
    if _special_manage_stop(t, symbol, pos, price, r, state):
        return

    # safety backstop: if it just sits there forever, close it (the ladder stop caps the downside)
    max_hold = r.get("special_runner_max_hold_hours", 72)
    if age_h >= max_hold:
        _safe_close(t, symbol, pos, "max-hold backstop", price, state)   # Fix #1: close & verify
        return

    tag = "LONG" if side > 0 else "SHORT"
    rung = pos.get("rung", 0)
    peak = pos.get("peak_margin", move * lev)
    ladder = r.get("special_stop_ladder", [[25, 0], [50, 25], [75, 50]])
    _hardpct = r.get("gainer_stop_margin_pct", 20) if pos.get("gainer") else r.get("special_stop_margin_pct", 20)
    stop_lbl = f"-{_hardpct:.0f}% (hard)" if rung == 0 else ("BE" if ladder[rung - 1][1] == 0 else f"+{ladder[rung-1][1]:.0f}%")
    log(f"HOLD SPECIAL {symbol} {tag} @ {price:.6g} | {move*lev:+.0f}% margin (peak +{peak:.0f}%) "
        f"| stop {stop_lbl} | age {age_h:.1f}h")


def _orphan_sweep(t, state):
    """Fix #4: any position OPEN on the exchange that the bot is NOT tracking (e.g. a close that
    failed earlier and orphaned it) gets FORCE-CLOSED. Keeps the account == the bot's state."""
    try:
        live = t.futures_positions()
    except Exception:
        return
    for p in live:
        sym = p["symbol"]
        if sym in state:
            continue                                   # tracked -> managed normally
        try:
            if t.force_close(sym):
                t.cancel_open_orders(sym)
                log(f"ORPHAN SWEEP: force-closed untracked position {sym}")
                note(f"Force-closed {_coin(sym)} - it was open on the exchange but the bot "
                     f"wasn't tracking it (orphaned). Cleaned up.")
            else:
                log(f"ORPHAN SWEEP: {sym} would not close (thin) - will retry")
        except Exception as e:
            log(f"orphan sweep {sym} failed: {e}")


def _reconcile_positions(t, state, r):
    """SAFETY GUARD: the EXCHANGE is the source of truth. If a tracked position's SIDE no longer
    matches the real position on the exchange, the bot must NOT keep 'managing' a phantom. This is
    what bit CAKE: a stray reduceOnly close on testnet oversold an 813 LONG into a 1642 SHORT; the
    bot never saw the flip, kept watching a (non-existent) long's stop, and the real short drifted to
    -45% with no stop order. Here we detect the hard long<->short mismatch, FLATTEN the real position
    (reconcile_flatten: reduceOnly market -> limit -> plain market, since testnet rejects reduceOnly),
    record an honest close from exchange truth, and drop it from state. Only a SIDE flip triggers it —
    a size difference (e.g. a gainer that booked its 50% partial) is expected and left alone.
    Toggle with reconcile_positions_enabled. Runs in the slow loop, BEFORE step() manages anything."""
    if not r.get("reconcile_positions_enabled", True):
        return
    for sym in list(state.keys()):
        pos = state.get(sym)
        if not pos:
            continue
        if pos.get("close_intent"):
            continue  # Partial emergency fills must not be mistaken for a new desync.
        try:
            amt, real_entry = t._position_amt_entry(sym)
        except Exception:
            continue                                       # unreachable this cycle -> retry next
        if amt == 0:
            continue                                       # flat-while-tracked handled by step()/manage
        tracked = pos.get("side", 1)
        if (amt > 0) == (tracked > 0):
            expected = pos.get("qty")
            if expected is None or np.isclose(abs(amt), expected, rtol=1e-8, atol=1e-10):
                pos["qty"] = abs(amt)
                pos.pop("execution_uncertain", None)
                save_state(state)
                continue
        # ---- HARD SIDE MISMATCH: the bot's memory is wrong. Trust the exchange. ----
        real_side = 1 if amt > 0 else -1
        log(f"RECONCILE {sym}: state says {'LONG' if tracked > 0 else 'SHORT'} but exchange holds "
            f"{'LONG' if real_side > 0 else 'SHORT'} {abs(amt):g} @ {real_entry:.6g} -> flattening")
        note(f"Fixed a desync on {_coin(sym)}: the bot's record ({'long' if tracked > 0 else 'short'}) "
             f"didn't match the exchange ({'long' if real_side > 0 else 'short'}). Closing the REAL "
             f"position on the exchange and clearing the stale record.")
        try:
            flat, detail = t.reconcile_flatten(sym)
        except Exception as e:
            flat, detail = False, f"flatten raised: {e}"
        log(f"RECONCILE {sym}: flatten -> {'FLAT' if flat else 'STILL OPEN'} ({detail})")
        if not flat:
            note(f"{_coin(sym)}: could not flatten the desynced position yet ({detail}) - will retry next cycle.")
            continue
        t.cancel_open_orders(sym)
        LAST_CLOSE[sym] = (time.time(), pos.get("timeframe", "4h"), False)
        if not pos.get("special") and not pos.get("news"):
            LAST_NORMAL_CLOSE[0] = time.time()
        _save_reentry()
        # honest close row built from EXCHANGE truth (the tracked side/entry were wrong)
        try:
            mark = t.futures_price(sym)
        except Exception:
            mark = real_entry
        realized = ""
        try:
            since = int(float(pos.get("open_epoch", 0)) * 1000)
            trs = t.fut.futures_account_trades(symbol=sym)
            realized = round(sum(float(x["realizedPnl"]) for x in trs if x["time"] >= since), 2)
        except Exception:
            pass
        lev = pos.get("leverage", 1) or 1
        if realized != "":
            LAST_CLOSE[sym] = (time.time(), pos.get("timeframe", "4h"), realized >= 0)
            if realized < 0:
                _record_loss(sym)
            _save_reentry()
            realized = round(realized - float(pos.get("realized_booked", 0) or 0), 2)
        margin = round(abs(amt) * real_entry / lev, 2)
        try:
            from history import record
            record({
                "open_time": pos.get("open_time", ""),
                "close_time": time.strftime("%Y-%m-%d %H:%M"),
                "coin": sym, "side": "LONG" if real_side > 0 else "SHORT",
                "type": ("gainer" if pos.get("gainer") else "special" if pos.get("special")
                         else "news" if pos.get("news") else "strategy"),
                "strategy": pos.get("strategy") or "",
                "timeframe": pos.get("timeframe", ""), "leverage": lev,
                "confirmations": pos.get("confirmations", ""),
                "entry": round(real_entry, 6), "exit": round(mark, 6),
                "margin_usdt": margin, "price_move_%": "",
                "pnl_%": (round(realized / margin * 100, 2) if (realized != "" and margin) else ""),
                "pnl_usdt": realized,
                "exit_reason": "state-desync reconcile (guard)",
            })
        except Exception as e:
            log(f"reconcile {sym}: history record failed: {e}")
        state.pop(sym, None)
        save_state(state)
        log(f"RECONCILE {sym}: cleared from state (realized {realized})")
        note(f"{_coin(sym)}: desync cleaned up. Realized P&L on the reconciled position: {realized} USDT.")


def run_auto(capital, max_coins=None):
    """Fully automatic multi-coin mode: auto-pick best coins + timeframe, trade up to N at once,
    plus a separate extra slot for news-driven trades."""
    from autobot import scan_and_rank, UNIVERSE
    t = BinanceTrader()
    active_tn = t.testnet                              # the account we're really trading (demo/live toggle)
    _write_account_status(active_tn)
    switch_warned = False
    log(f"trading account: {'DEMO (testnet, fake money)' if active_tn else 'LIVE (REAL money)'}")
    _load_special_tp_lock()                            # restore the 24h +100%-TP lockouts across restart
    _load_reentry()                                    # restore cooldown / 6h loss-lockout / 2-loss ban
    _load_gainer_prev()                                # restore the last top-N list (no stale crossings)
    try:                                               # keep bot.pid == the live process so the
        open("bot.pid", "w").write(str(os.getpid()))   # dashboard shows 'running' (bot is a service now)
    except Exception:
        pass
    r = rules()
    interval = r.get("check_interval_seconds", 300)
    total_cap = max_coins or r.get("max_concurrent_positions", 10)
    reg_slots = r.get("regular_slots", 6)
    news_slots = r.get("news_slots", 1)
    spec_slots = r.get("special_slots", 2)
    gain_slots = r.get("gainer_slots", 2) if r.get("gainer_enabled", True) else 0
    pct = r.get("trade_pct_of_capital", 0.10)
    try:                                               # the CAPITAL BASE: allocated slice or full wallet
        start_bal = _capital_now(t, capital)
    except Exception:
        start_bal = capital
    log(f"AUTO BOT START | capital {start_bal:.0f} USDT | {pct*100:.0f}% per trade (~{start_bal*pct:.0f} USDT, "
        f"capped by free margin) | {total_cap} slots = {reg_slots} normal + {spec_slots} special + "
        f"{gain_slots} gainer (specials & gainers first) | "
        f"{'TESTNET (fake money)' if t.testnet else 'LIVE (REAL money)'} | check every {interval}s")
    _lad = r.get("special_stop_ladder", [[25, 0], [50, 25], [75, 50]])           # describe FROM config
    _be = _lad[0][0] if _lad else 25                                             # peak% at which stop -> BE
    _slo, _shi = r.get("special_leverage_min", 5), r.get("special_leverage_max", 10)
    _sstop, _stgt = r.get("special_stop_margin_pct", 20), r.get("special_target_margin_pct", 100)
    _gstop = r.get("gainer_stop_margin_pct", 20)                    # gainer keeps its own (wider) floor
    _nlo, _nlev = r.get("normal_leverage_min", 1), r.get("normal_max_leverage", 5)
    _nstop = r.get("stop_loss_pct", 20)
    _nlad = r.get("normal_stop_ladder", [[25, 0], [50, 25], [75, 50]])
    _ntgt = r.get("normal_target_margin_pct", 100)
    _nexit = (f"a margin ladder ({', '.join(f'+{a:g}%->' + ('break-even' if b == 0 else f'+{b:g}%') for a, b in _nlad)})"
              f" and a full close at +{_ntgt:g}% of margin"
              if r.get("normal_exit_mode", "trail") == "ladder"
              else "a trailing stop with a break-even clamp")
    _gcut = (f", and cut early if still below -{r.get('gainer_underwater_cut_margin_pct', 5):g}% of margin after "
             f"{r.get('gainer_underwater_cut_min', 35):g} min"
             if r.get("gainer_underwater_cut_enabled", True) else "")
    note(f"--- Bot started. Wallet balance {start_bal:.0f} USDT, using {pct*100:.0f}% per trade "
         f"(~{start_bal*pct:.0f} USDT each, and the last trade takes whatever balance is left), "
         f"{total_cap} slots total: {spec_slots} special momentum trades (funded FIRST, {_slo:g}-{_shi:g}x, stop at "
         f"-{_sstop:g}% of margin, full close at +{_stgt:g}% of margin, break-even stop after +{_be:g}%), "
         f"{gain_slots} gainer trades (a coin entering the top-{r.get('gainer_top_n', 10)} 24h gainers list, "
         f"checked once every 15 minutes, only if open interest is up "
         f"{r.get('gainer_oi_pct', 2):g}%+ over {r.get('gainer_oi_lookback_hours', 4):g}h - "
         f"same ladder/target as specials but its own -{_gstop:g}% of margin stop, with "
         f"{r.get('gainer_partial_frac', 0.5)*100:.0f}% banked at "
         f"+{r.get('gainer_partial_at_margin', 25):g}% and the rest running{_gcut}), and {reg_slots} "
         f"normal strategy trades (auto {_nlo:g}-{_nlev:g}x, -{_nstop:g}% margin stop, {_nexit}). "
         f"No news trades. On {'TESTNET (fake money)' if t.testnet else 'LIVE money'}. "
         f"Only trading normal when trend strength (ADX) is at least {r.get('min_adx', 0)}. "
         f"Watching open trades every {r.get('watch_interval_seconds', 1)}s, "
         f"looking for new trades every {interval}s. ---")
    state = load_state()
    seen_news = set()
    seen_special = set()

    while True:
        try:
            r = rules()
            # ---- DEMO <-> LIVE account toggle (dashboard). Switch ONLY when the book is flat, so we never
            #      strand positions across accounts. Nothing happens until the toggle is actually flipped. ----
            want_tn = _desired_testnet(active_tn)
            if want_tn != active_tn:
                if state:
                    if not switch_warned:
                        log(f"ACCOUNT SWITCH to {'DEMO' if want_tn else 'LIVE'} requested — waiting for "
                            f"{len(state)} open position(s) to close first (won't switch while trades are open)")
                        switch_warned = True
                else:
                    try:
                        new_t = BinanceTrader(testnet=want_tn)     # rebuild for the new account (keys/endpoint)
                        new_t.fut.futures_account()                # VERIFY the keys authenticate BEFORE committing
                        t = new_t                                  # only now swap — a bad key never strands the bot
                        active_tn = want_tn; switch_warned = False
                        _clear_for_switch(); state = load_state()      # fresh start on the new account
                        _write_account_status(active_tn)
                        log(f"*** SWITCHED TRADING ACCOUNT -> "
                            f"{'DEMO (testnet, fake money)' if active_tn else 'LIVE (REAL money)'} ***")
                        note(f"Switched trading account to "
                             f"{'DEMO (testnet)' if active_tn else 'LIVE - REAL money'}. Starting fresh.")
                    except Exception as e:
                        _write_account_status(active_tn,                      # surface it on the dashboard
                            error=f"can't reach the {'demo' if want_tn else 'LIVE'} account: {e}")
                        if not switch_warned:
                            log(f"account switch to {'DEMO' if want_tn else 'LIVE'} FAILED: {e} "
                                f"-> staying on {'DEMO' if active_tn else 'LIVE'} "
                                f"(check the {'demo' if want_tn else 'live'} API keys in .env)")
                            switch_warned = True
            elif switch_warned:                            # desired == active again -> clear any stale error
                switch_warned = False
                _write_account_status(active_tn)
            coins = r.get("allowed_symbols") or None       # empty list -> dynamic all-coins scan
            # CAPITAL BASE = the dashboard 'Trading Capital' slice (compounding) if set, else the full
            # LIVE wallet balance. Each trade WANTS 10% of it, capped at the FREE margin available, so the
            # last trade simply takes whatever is left (never fails with -2019). Specials are funded FIRST.
            cap_now = _capital_now(t, capital)             # allocated slice (compounds) or full wallet
            min_usdt = r.get("min_trade_usdt", 5.0)
            nominal = _open_size(t, r, cap_now)            # size for the (rare) re-open inside manage
            # 0) RECONCILE first: fix any position whose SIDE no longer matches the exchange (a desync
            #    like the CAKE long->short flip) BEFORE step() tries to manage a phantom.
            try:
                _reconcile_positions(t, state, r)
            except Exception as e:
                log(f"reconcile pass failed: {e}")
            # 1) manage every open position (regular + special) — isolate each coin
            for sym in list(state.keys()):
                pos = state.get(sym)
                if pos is None:
                    continue  # The fast watcher may have closed it during the previous step.
                try:
                    step(t, sym, nominal, r, state, pos.get("timeframe", "4h"))
                except Exception as e:
                    log(f"manage {sym} skipped this cycle: {e}")
            _orphan_sweep(t, state)                 # Fix #4: close exchange positions the bot isn't tracking
            # 2) SPECIAL trades FIRST — priority claim on the capital (5-10x, -20% margin stop)
            if not entries_paused() and r.get("special_enabled", True):
                try:
                    _do_special_trades(t, r, state, cap_now, seen_special, total_cap)
                except Exception as e:
                    log(f"special check failed: {e}")
            # 2b) GAINER trades — top-10 24h-gainers crossings, confirmed by a 4h OI build.
            #     Evaluated once per closed 15m bar (NOT every cycle) and funded like specials.
            if not entries_paused() and r.get("gainer_enabled", True):
                try:
                    _do_gainer_trades(t, r, state, cap_now, total_cap)
                except Exception as e:
                    log(f"gainer check failed: {e}")
            # 3) NORMAL strategy slots — funded from whatever capital is LEFT after specials.
            #    REST TIMER: after any normal trade closes, wait rest_after_close_min before scanning
            #    for a new one (open-trade watching every 1s is UNAFFECTED — this only gates NEW opens).
            rest_s = r.get("rest_after_close_min", 5) * 60
            resting = time.time() - LAST_NORMAL_CLOSE[0] < rest_s
            regular = sum(1 for v in state.values() if not v.get("news") and not v.get("special"))
            slots = min(reg_slots - regular, total_cap - len(state))
            if entries_paused():
                slots = 0
            if slots > 0 and resting:
                left = int(rest_s - (time.time() - LAST_NORMAL_CLOSE[0]))
                log(f"resting {left}s after the last trade close before scanning for a new normal trade")
            elif slots > 0:
                log(f"Scanning for the best setups… ({slots} normal slot(s) free)")
                picks = _read_with_watch(t, r, state, scan_and_rank, coins, min_trades=r.get("min_trades_for_strategy", 20),
                                      coin_limit=r.get("scan_coin_limit", 120),
                                      max_return=r.get("max_backtest_return_pct", 300),
                                      spike_low=r.get("volume_spike_low", 1.5),
                                      spike_high=r.get("volume_spike_high", 2.0),
                                      spike_boost=r.get("volume_spike_boost", 1.5),
                                      spike_exclude=r.get("volume_spike_exclude"),
                                      min_adx=r.get("min_adx", 0),
                                      exclude_strategies=_stopped_strategies(r), rules=r)
                save_recommendations(picks)          # publish live suggestions for the dashboard
                _tradable = t.tradable_symbols()      # coins the CURRENT exchange lists (testnet now, live later)
                for pick in picks:
                    if slots <= 0:
                        break
                    if not _can_open_now(r):              # spread entries out over time
                        log(f"batch limit: {r.get('max_trades_per_batch', 3)} trades opened in the last "
                            f"{r.get('open_batch_window_min', 30)} min -> waiting for better entries")
                        break
                    if pick["symbol"] in state or pick["signal"] == 0:
                        continue
                    if _excluded(pick["symbol"], r):     # do-not-trade list backstop
                        continue
                    if _tradable and pick["symbol"] not in _tradable:   # not on THIS exchange -> -1121; skip
                        continue
                    usdt = _open_size(t, r, cap_now)     # 10% of capital, capped by free margin
                    if usdt < min_usdt:                  # wallet full -> nothing left for a normal trade
                        log(f"normal slots: only {usdt:.2f} USDT free (< {min_usdt} min) -> wallet full, waiting")
                        break
                    try:
                        step(t, pick["symbol"], usdt, r, state, pick["timeframe"], candidate=pick)
                    except Exception as e:
                        log(f"open {pick['symbol']} skipped: {e}")
                        continue
                    if pick["symbol"] in state:
                        slots -= 1
            # 4) NEWS trades — DISABLED (news_enabled=false). Kept for completeness only.
            if not entries_paused() and r.get("news_enabled", False):
                _do_news_trades(t, r, state, nominal, seen_news, total_cap)
        except guards.RiskError as e:
            log(f"BLOCKED by safety rule: {e}")
        except Exception as e:
            log(f"auto error (will retry): {e}")
        # ---- FAST WATCH: until the next scan is due, check live prices every second ----
        fast = max(0.5, float(r.get("watch_interval_seconds", 1)))
        deadline = time.time() + interval
        while time.time() < deadline:
            t0 = time.time()
            try:
                watch_positions(t, r, state)
            except Exception as e:
                log(f"fast watch error: {e}")
            time.sleep(max(0.2, fast - (time.time() - t0)))   # keep the true cycle at ~fast seconds


def run_bot(symbol, usdt, timeframe=TIMEFRAME):
    symbol = symbol.upper()
    t = BinanceTrader()
    interval = rules().get("check_interval_seconds", 300)
    leverage = rules().get("default_leverage", 2)

    guards.check_order(symbol, usdt, leverage)  # fail fast if the request itself is unsafe
    log(f"BOT START | {symbol} | {usdt} USDT | {leverage}x | {timeframe} | "
        f"{'TESTNET (fake money)' if t.testnet else 'LIVE (REAL money)'} | check every {interval}s")
    state = load_state()

    while True:
        try:
            r = rules()
            step(t, symbol, usdt, r, state, timeframe)
        except guards.RiskError as e:
            log(f"BLOCKED by safety rule: {e}")
        except Exception as e:
            log(f"error (will retry): {e}")
        until = time.monotonic() + interval
        while time.monotonic() < until:
            watch_positions(t, r, state)
            time.sleep(min(1.0, max(0.0, until - time.monotonic())))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage:\n"
              "  python bot.py <SYMBOL> <USDT> [TIMEFRAME]   # one coin, e.g. python bot.py ETHUSDT 50 4h\n"
              "  python bot.py auto <CAPITAL> [MAX_COINS]     # fully automatic, e.g. python bot.py auto 1000 5")
        sys.exit(1)
    try:
        if sys.argv[1].lower() == "auto":
            if len(sys.argv) > 3:
                mc = int(sys.argv[3])
            else:                                   # no CLI max_coins -> use the dashboard's shared setting
                try:
                    mc = int(json.load(open("ui_settings.json")).get("max_coins", 10))
                except Exception:
                    mc = 10
            run_auto(float(sys.argv[2]), mc)
        else:
            run_bot(sys.argv[1], float(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else TIMEFRAME)
    except KeyboardInterrupt:
        print("\nBot stopped by user. Open positions (if any) remain — close them in the UI or the testnet site.")
