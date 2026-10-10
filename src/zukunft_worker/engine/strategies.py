"""Trading strategies. Each returns a position array: +1 = long, -1 = short, 0 = flat.
All can go BOTH ways (long & short) so they fit a futures/leverage agent.
Grouped by difficulty: Basic / Medium / Advanced.
"""
import numpy as np
import pandas as pd

from indicators import (ema, sma, rsi, macd, supertrend, adx, donchian, atr,
                        stochastic, williams_r, cci, psar, keltner, rolling_vwap,
                        ichimoku, heikin_ashi, awesome, trix)


def _hold(sig, index):
    """Turn a sparse signal of {1, -1, nan} into a continuous held position."""
    return pd.Series(sig, index=index).ffill().fillna(0).values


# ===================== BASIC =====================
def sma_cross(df, fast=50, slow=200):
    quick, trend = sma(df["close"], fast), sma(df["close"], slow)
    return np.where(trend.isna() | quick.isna(), 0, np.where(quick > trend, 1, -1))


def ema_cross(df, fast=20, slow=50):
    return np.where(ema(df["close"], fast) > ema(df["close"], slow), 1, -1)


def ema_fast(df):
    return np.where(ema(df["close"], 9) > ema(df["close"], 21), 1, -1)


def trend_200ema(df):
    return np.where(df["close"] > ema(df["close"], 200), 1, -1)


def rsi_meanrev(df, low=30, high=70):
    r = rsi(df["close"])
    return _hold(np.where(r < low, 1, np.where(r > high, -1, np.nan)), df.index)


def stoch_strategy(df):
    k, _ = stochastic(df)
    return _hold(np.where(k < 20, 1, np.where(k > 80, -1, np.nan)), df.index)


def williams_strategy(df):
    w = williams_r(df)
    return _hold(np.where(w < -80, 1, np.where(w > -20, -1, np.nan)), df.index)


def momentum_roc(df, period=14):
    roc = df["close"].pct_change(period)
    return np.where(roc > 0, 1, np.where(roc < 0, -1, 0))


def multilayer_momentum(df):
    """Multi-Layer Momentum (GainzAlgo-style): only trades when FOUR layers all agree —
    trend (EMA 20/50) + momentum (MACD + RSI) + regime (ADX>20) + structure (vs 20-mean).
    High-conviction, heavily filtered — few but cleaner signals."""
    ma_fast, ma_slow = ema(df["close"], 20), ema(df["close"], 50)
    line, sig_line = macd(df["close"])
    r = rsi(df["close"])
    ax = adx(df)
    mean20 = df["close"].rolling(20).mean()
    up = (ma_fast > ma_slow) & (line > sig_line) & (r > 50) & (ax > 20) & (df["close"] > mean20)
    dn = (ma_fast < ma_slow) & (line < sig_line) & (r < 50) & (ax > 20) & (df["close"] < mean20)
    return _hold(np.where(up, 1, np.where(dn, -1, np.nan)), df.index)


def buy_hold(df):
    return np.ones(len(df))


# ===================== MEDIUM =====================
def macd_strategy(df):
    line, sig = macd(df["close"])
    return np.where(line > sig, 1, -1)


def bollinger_meanrev(df, period=20, k=2):
    ma, sd = df["close"].rolling(period).mean(), df["close"].rolling(period).std()
    return _hold(np.where(df["close"] < ma - k * sd, 1,
                          np.where(df["close"] > ma + k * sd, -1, np.nan)), df.index)


def bollinger_breakout(df, period=20, k=2):
    ma, sd = df["close"].rolling(period).mean(), df["close"].rolling(period).std()
    return _hold(np.where(df["close"] > ma + k * sd, 1,
                          np.where(df["close"] < ma - k * sd, -1, np.nan)), df.index)


def cci_strategy(df):
    c = cci(df)
    return _hold(np.where(c > 100, 1, np.where(c < -100, -1, np.nan)), df.index)


def psar_strategy(df):
    return psar(df).values


def keltner_breakout(df):
    _, up, lo = keltner(df)
    return _hold(np.where(df["close"] > up, 1, np.where(df["close"] < lo, -1, np.nan)), df.index)


def donchian_adx(df, period=20, adx_min=20):
    up, lo = donchian(df, period)
    ax = adx(df)
    sig = np.where((df["close"] > up) & (ax > adx_min), 1,
                   np.where((df["close"] < lo) & (ax > adx_min), -1, np.nan))
    return np.where(ax.values < adx_min, 0, _hold(sig, df.index))


def vwap_strategy(df):
    return np.where(df["close"] > rolling_vwap(df, 20), 1, -1)


def heikin_trend(df):
    ho, hc = heikin_ashi(df)
    return np.where(hc > ho, 1, -1)


def trix_strategy(df):
    t = trix(df["close"], 15)
    return np.where(t > ema(t, 9), 1, -1)


def awesome_strategy(df):
    ao = awesome(df)
    return np.where(ao > 0, 1, np.where(ao < 0, -1, 0))


# ===================== ADVANCED =====================
def supertrend_strategy(df, period=10, factor=3):
    return supertrend(df, period, factor).values


def supertrend_fusion(df):
    a, b = supertrend(df, 10, 3), supertrend(df, 20, 5)
    return np.where((a == 1) & (b == 1), 1, np.where((a == -1) & (b == -1), -1, 0))


def regime_adaptive(df, adx_min=25):
    ax = adx(df).values
    trend = supertrend(df, 10, 3).values
    r = rsi(df["close"])
    mr = _hold(np.where(r < 40, 1, np.where(r > 60, -1, np.nan)), df.index)
    return np.where(ax >= adx_min, trend, mr)


def supertrend_rsi_volume(df):
    st = supertrend(df, 10, 3).values
    r = rsi(df["close"]).values
    vol_ok = df["volume"].values > df["volume"].rolling(20).mean().values
    sig = np.where((st == 1) & (r > 50) & vol_ok, 1,
                   np.where((st == -1) & (r < 50) & vol_ok, -1, np.nan))
    return _hold(sig, df.index)


def ichimoku_strategy(df):
    conv, base = ichimoku(df)
    return np.where(conv > base, 1, -1)


def macd_rsi_combo(df):
    line, sig = macd(df["close"])
    r = rsi(df["close"]).values
    s = np.where((line > sig) & (r > 50), 1, np.where((line < sig) & (r < 50), -1, np.nan))
    return _hold(s, df.index)


def atr_breakout(df, period=14, mult=1.5):
    a = atr(df, period)
    up, lo = df["close"].shift(1) + mult * a, df["close"].shift(1) - mult * a
    return _hold(np.where(df["close"] > up, 1, np.where(df["close"] < lo, -1, np.nan)), df.index)


def dual_momentum(df):
    fast, slow = df["close"].pct_change(10), df["close"].pct_change(40)
    return np.where((fast > 0) & (slow > 0), 1, np.where((fast < 0) & (slow < 0), -1, 0))


def zscore_meanrev(df, period=20):
    ma, sd = df["close"].rolling(period).mean(), df["close"].rolling(period).std()
    z = (df["close"] - ma) / sd
    return _hold(np.where(z < -2, 1, np.where(z > 2, -1, np.nan)), df.index)


# name -> (function, level)
STRATEGIES = {
    "SMA Cross (50/200)": (sma_cross, "Basic"),
    "EMA Cross (20/50)": (ema_cross, "Basic"),
    "EMA Fast (9/21)": (ema_fast, "Basic"),
    "Price vs 200 EMA": (trend_200ema, "Basic"),
    "RSI Mean-Reversion": (rsi_meanrev, "Basic"),
    # PRUNED 2026-07-20 (net-negative in live results): "Stochastic (14,3)": (stoch_strategy, "Basic"),
    "Williams %R (14)": (williams_strategy, "Basic"),
    "Momentum (ROC-14)": (momentum_roc, "Basic"),
    "Buy & Hold": (buy_hold, "Basic"),
    "MACD (12/26/9)": (macd_strategy, "Medium"),
    "Bollinger Mean-Reversion": (bollinger_meanrev, "Medium"),
    "Bollinger Breakout": (bollinger_breakout, "Medium"),
    "CCI (20)": (cci_strategy, "Medium"),
    "Parabolic SAR": (psar_strategy, "Medium"),   # kept 2026-07-26 (open MIRA +$122 winner; watching)
    "Keltner Breakout": (keltner_breakout, "Medium"),
    "Donchian + ADX Breakout": (donchian_adx, "Medium"),
    # PRUNED 2026-07-20 (net-negative in live results): "VWAP Trend": (vwap_strategy, "Medium"),
    "TRIX (15,9)": (trix_strategy, "Medium"),
    "Awesome Oscillator": (awesome_strategy, "Medium"),
    "Supertrend (10,3)": (supertrend_strategy, "Advanced"),
    "Supertrend Fusion": (supertrend_fusion, "Advanced"),
    # PRUNED 2026-08-13 (duplicate given the ADX>=30 entry gate): regime_adaptive switches at ADX>=25,
    # so at every live entry (ADX>=30) it emits the IDENTICAL Supertrend(10,3) signal -> 100% agreement,
    # adds no new entry, and double-counts in the min_confirmations vote. "Regime-Adaptive (ADX)": (regime_adaptive, "Advanced"),
    "Supertrend + RSI + Volume": (supertrend_rsi_volume, "Advanced"),
    # PRUNED 2026-07-20 (net-negative in live results): "Ichimoku (Tenkan/Kijun)": (ichimoku_strategy, "Advanced"),
    # PRUNED 2026-07-20 (net-negative in live results): "MACD + RSI Combo": (macd_rsi_combo, "Advanced"),
    "ATR Volatility Breakout": (atr_breakout, "Advanced"),
    "Z-Score Mean-Reversion": (zscore_meanrev, "Advanced"),
    "Multi-Layer Momentum": (multilayer_momentum, "Advanced"),
}
