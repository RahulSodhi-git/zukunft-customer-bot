"""The bot's brain: pick the best reliable strategy, read its live signal, and
auto-size take-profit / stop-loss from volatility (capped by fail-safe limits)."""
import numpy as np

from strategies import STRATEGIES
from backtest import run
from indicators import atr, ema
from exit_rules import normal_floor_pct

HISTORY = 500
WARMUP = 200


def strategy_score(net, trades, win):
    return net * (0.5 + 0.5 * min(trades / 40, 1.0)) * (win / 100 + 0.5)


def strategy_positions(fn, df):
    positions = np.asarray(fn(df), float).copy()
    positions[:WARMUP] = 0
    return positions


def higher_tf_trend(df):
    """+1 if the higher-timeframe trend is UP (EMA20 > EMA50), else -1.
    Used to only trade WITH the bigger trend (avoids counter-trend whipsaw)."""
    return 1 if ema(df["close"], 20).iloc[-1] > ema(df["close"], 50).iloc[-1] else -1


def pick_best_strategy(df, min_trades=20, max_return=100000, exclude=None, rules=None, fee=0.0005):
    """Best reliable + profitable strategy, PLUS how many strategies confirm its signal.
    Skips 'miracle' overfit backtests (return above max_return). `exclude` = strategy names to skip
    entirely (e.g. disabled/'stop' strategies) so they're never evaluated or picked.
    Returns a dict {name, fn, ret, trades, win, signal, confirmations} or None."""
    exclude = exclude or set()
    best = None
    sigs = {}
    for name, (fn, level) in STRATEGIES.items():
        if name == "Buy & Hold" or name in exclude:
            continue
        pos = strategy_positions(fn, df)
        sigs[name] = int(pos[-1])                     # each strategy's latest vote
        r = run(df, pos, fee=fee, exit_rules=rules,
                zero_policy=(rules or {}).get("normal_zero_signal_policy", "hold"))
        if r["trades"] >= min_trades and 0 < r["return_pct"] <= max_return:
            score = strategy_score(r["return_pct"], r["trades"], r["win_rate"])
            if best is None or score > best["score"]:
                best = {"name": name, "fn": fn, "ret": r["return_pct"],
                        "trades": r["trades"], "win": r["win_rate"], "signal": int(pos[-1]),
                        "score": score, "backtest": r}
    if best is None:
        return None
    aliases = {"Z-Score Mean-Reversion": "Bollinger Mean-Reversion"}
    best["confirmations"] = len({aliases.get(name, name) for name, value in sigs.items()
                                 if value == best["signal"] and value != 0})
    return best


def latest_signal(fn, df):
    """+1 = go long, -1 = go short, 0 = stay flat (the strategy's newest reading)."""
    return int(strategy_positions(fn, df)[-1])


def auto_leverage(df, positions, rules):
    """Pick the leverage (up to max_leverage) that MAXIMISES the backtested net return.
    Higher leverage amplifies profit, so this walks up while the backtest keeps improving and
    never liquidates — a leverage that would have wiped the position out is never chosen."""
    cap = int(rules.get("normal_max_leverage", rules.get("max_leverage", 5)))
    minimum = int(rules.get("normal_leverage_min", 1))
    if not 1 <= minimum <= cap:
        raise ValueError("Invalid leverage range")
    best_lev = None
    positions = np.asarray(positions, float).copy()
    positions[:WARMUP] = 0
    kwargs = {"exit_rules": rules, "zero_policy": rules.get("normal_zero_signal_policy", "hold")}
    best_ret = -float("inf")
    for cand in sorted({1, 2, 3, 5, minimum, cap}):
        if not minimum <= cand <= cap:
            continue
        r = run(df, positions, leverage=cand, **kwargs)
        if not r["liquidated"] and r["return_pct"] > best_ret:   # keep the most profitable, safe leverage
            best_lev, best_ret = cand, r["return_pct"]
    return best_lev


def auto_tp_sl(df, side, entry_price, rules, leverage=1):
    """Stop-loss price = a wide price stop that SCALES DOWN with leverage, so the money at risk
    stays ~stop_loss_pct% of the margin at ANY leverage (20% price @1x, 4% @5x) — never a
    liquidation-level stop. The trailing stop does the profit-taking; this SL is the disaster floor.
    side: +1 long / -1 short."""
    atr_pct = float(atr(df, 14).iloc[-1]) / entry_price * 100
    sl_pct = normal_floor_pct(rules, leverage)
    tp_pct = min(3.0 * atr_pct, rules["failsafe_max_take_profit_pct"])  # (unused — trailing exits)
    sl_pct, tp_pct = max(sl_pct, 0.3), max(tp_pct, 0.6)                 # never zero
    if side > 0:  # long
        tp, sl = entry_price * (1 + tp_pct / 100), entry_price * (1 - sl_pct / 100)
    else:         # short
        tp, sl = entry_price * (1 - tp_pct / 100), entry_price * (1 + sl_pct / 100)
    return tp, sl, round(tp_pct, 2), round(sl_pct, 2)
