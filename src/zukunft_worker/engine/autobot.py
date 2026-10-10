"""Auto coin-picker: scan many coins across timeframes, backtest all strategies
(NET of fees), and rank which coin+timeframe+strategy is best to trade right now.
Network fetches run in parallel (the slow part), so a full scan takes ~15-30s."""
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from data import get_ohlcv
from indicators import adx
from strategies import STRATEGIES
from backtest import run
from signals import auto_leverage, pick_best_strategy, strategy_score, HISTORY

# Curated liquid universe (Option A) — fast to scan, sensible to trade.
UNIVERSE = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
            "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT", "DOTUSDT", "TRXUSDT",
            "ATOMUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT",
            "INJUSDT", "TIAUSDT"]
TRADE_TF = "4h"             # we TRADE on 4h (1d dropped — it was a net loser in live results)
TREND_TF = "1d"             # we use 1d ONLY as the higher-timeframe trend filter
TIMEFRAMES = (TRADE_TF, TREND_TF)
CANDLES = HISTORY

# Coins that are ALWAYS high-volume — a "spike" on these means nothing, so never flag them.
SPIKE_EXCLUDE = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"}


def _score(net, trades, win):
    """Reward net profit + reliability (more trades) + win rate."""
    return strategy_score(net, trades, win)


def _fetch(task):
    sym, tf = task
    try:
        return sym, tf, get_ohlcv(sym, tf, CANDLES, closed_only=True, market="futures")
    except Exception:
        return sym, tf, None


def scan_and_rank(coins=None, timeframes=TIMEFRAMES, min_trades=20, fee=0.0005,
                  max_leverage=5, coin_limit=120, max_return=300,
                  spike_low=1.5, spike_high=2.0, spike_boost=1.5, spike_exclude=None,
                  min_adx=0, exclude_strategies=None, rules=None):
    """Scan ALL liquid coins (dynamic, ranked by volume) → ranked best coins to trade.
    coin_limit caps how many of the most-liquid coins to scan (0 = every liquid coin).
    Each pick includes 'confirmations' = how many strategies agree on the direction."""
    if coins is None:
        from data import tradeable_universe
        coins = tradeable_universe(limit=coin_limit) or list(UNIVERSE)   # dynamic, no fixed list
    coins = list(coins)
    rules = dict(rules or {})
    rules.setdefault("max_leverage", max_leverage)
    filter_trend = rules.get("normal_trend_filter", False)
    spike_exclude = set(spike_exclude) if spike_exclude is not None else SPIKE_EXCLUDE
    exclude_strategies = set(exclude_strategies or ())   # disabled strategies -> never evaluated/ranked

    tasks = [(c, tf) for c in coins for tf in timeframes]
    data = {}
    with ThreadPoolExecutor(max_workers=24) as ex:      # parallel network fetches
        for sym, tf, df in ex.map(_fetch, tasks):
            if df is not None and len(df) > 50:
                data[(sym, tf)] = df

    from signals import higher_tf_trend
    results = []
    for c in coins:
        dft = data.get((c, TRADE_TF))          # 4h = where we trade
        dfh = data.get((c, TREND_TF))          # 1d = the trend filter
        if dft is None or (filter_trend and dfh is None):
            continue
        # CHOP FILTER: skip sideways markets — that's where strategies whipsaw (signal flips)
        coin_adx = None
        if min_adx:
            try:
                coin_adx = float(adx(dft).iloc[-1])
            except Exception:
                coin_adx = None                # can't measure -> don't block
            if coin_adx is None or not np.isfinite(coin_adx) or coin_adx < min_adx:
                continue
        trend = higher_tf_trend(dfh) if dfh is not None else 0

        chosen = pick_best_strategy(dft, min_trades, max_return, exclude_strategies, rules, fee)
        best = None
        if chosen:
            r = chosen["backtest"]
            best = {"symbol": c, "timeframe": TRADE_TF, "strategy": chosen["name"],
                    "signal": chosen["signal"], "net": round(r["return_pct"], 1),
                    "gross": round(r["gross_return_pct"], 1), "fees": r["fees_pct"],
                    "trades": r["trades"], "win": round(r["win_rate"]),
                    "dd": round(r["max_dd"], 1), "score": chosen["score"],
                    "confirmations": chosen["confirmations"], "signal_bar": str(dft["time"].iloc[-1])}
        if best is None:
            continue
        # TREND FILTER: skip any trade that fights the daily trend
        if filter_trend and best["signal"] != 0 and best["signal"] != trend:
            continue
        best["trend"] = "UP" if trend > 0 else "DOWN"
        best["adx"] = round(coin_adx) if coin_adx is not None else None
        best["leverage"] = auto_leverage(dft, np.asarray(STRATEGIES[best["strategy"]][0](dft), float),
                                         rules)
        if best["leverage"] is None:
            continue
        vols = dft["volume"].values             # volume spike (free — df already loaded)
        base = vols[-21:-1].mean() if len(vols) > 21 else (vols[:-1].mean() if len(vols) > 1 else 0)
        best["vol_spike"] = round(vols[-1] / base, 1) if base > 0 else 0
        # boost only MODERATE volume (1.5x-2x = healthy participation); extreme spikes are noise
        best["volume_flag"] = bool(spike_low <= best["vol_spike"] <= spike_high and c not in spike_exclude)
        if best["volume_flag"]:
            best["score"] = round(best["score"] * spike_boost, 1)
        results.append(best)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def equity_for(symbol, timeframe, strategy_name, leverage=1, fee=0.0005):
    """Recompute price + equity curve for one pick (for charts), at its auto leverage."""
    df = get_ohlcv(symbol, timeframe, CANDLES, closed_only=True, market="futures")
    fn = STRATEGIES[strategy_name][0]
    return df, run(df, np.asarray(fn(df), float), leverage=leverage, fee=fee)
