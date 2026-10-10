"""Technical indicators built from OHLCV data (no external TA library needed)."""
import numpy as np
import pandas as pd


def sma(s, period):
    return s.rolling(period).mean()


def ema(s, span):
    return s.ewm(span=span, adjust=False).mean()


def rsi(s, period=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    dn = -d.clip(upper=0).ewm(alpha=1 / period, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def macd(s, fast=12, slow=26, signal=9):
    line = ema(s, fast) - ema(s, slow)
    return line, ema(line, signal)


def atr(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def supertrend(df, period=10, factor=3):
    """Trend direction: +1 (up) or -1 (down)."""
    hl2 = (df["high"] + df["low"]) / 2
    a = atr(df, period)
    upper, lower = (hl2 + factor * a).values, (hl2 - factor * a).values
    close = df["close"].values
    n = len(df)
    fu, fl, dir_ = upper.copy(), lower.copy(), np.ones(n)
    for i in range(1, n):
        fu[i] = upper[i] if (upper[i] < fu[i - 1] or close[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lower[i] if (lower[i] > fl[i - 1] or close[i - 1] < fl[i - 1]) else fl[i - 1]
        dir_[i] = 1 if close[i] > fu[i - 1] else (-1 if close[i] < fl[i - 1] else dir_[i - 1])
    return pd.Series(dir_, index=df.index)


def adx(df, period=14):
    """Trend strength (0-100). >25 = strong trend, <20 = ranging."""
    h, l, c = df["high"], df["low"], df["close"]
    up, dn = h.diff(), -l.diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1 / period, adjust=False).mean()
    p_di = 100 * plus.ewm(alpha=1 / period, adjust=False).mean() / atr_
    m_di = 100 * minus.ewm(alpha=1 / period, adjust=False).mean() / atr_
    dx = 100 * (p_di - m_di).abs() / (p_di + m_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def donchian(df, period=20):
    return df["high"].rolling(period).max().shift(1), df["low"].rolling(period).min().shift(1)


def stochastic(df, k=14, d=3):
    ll, hh = df["low"].rolling(k).min(), df["high"].rolling(k).max()
    kline = 100 * (df["close"] - ll) / (hh - ll)
    return kline, kline.rolling(d).mean()


def williams_r(df, period=14):
    hh, ll = df["high"].rolling(period).max(), df["low"].rolling(period).min()
    return -100 * (hh - df["close"]) / (hh - ll)


def cci(df, period=20):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma = tp.rolling(period).mean()
    md = tp.rolling(period).apply(lambda values: np.abs(values - values.mean()).mean(), raw=True)
    return (tp - ma) / (0.015 * md)


def psar(df, af=0.02, af_max=0.2):
    """Parabolic SAR trend direction: +1 (up) / -1 (down)."""
    high, low = df["high"].values, df["low"].values
    n = len(df)
    sar, trend = low.copy(), np.ones(n)
    up, ep, acc = True, high[0], af
    for i in range(1, n):
        sar[i] = sar[i - 1] + acc * (ep - sar[i - 1])
        if up:
            if low[i] < sar[i]:
                up, sar[i], ep, acc = False, ep, low[i], af
            elif high[i] > ep:
                ep, acc = high[i], min(acc + af, af_max)
        else:
            if high[i] > sar[i]:
                up, sar[i], ep, acc = True, ep, high[i], af
            elif low[i] < ep:
                ep, acc = low[i], min(acc + af, af_max)
        trend[i] = 1 if up else -1
    return pd.Series(trend, index=df.index)


def keltner(df, period=20, mult=2):
    mid, a = ema(df["close"], period), atr(df, period)
    return mid, mid + mult * a, mid - mult * a


def rolling_vwap(df, period=20):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return (tp * df["volume"]).rolling(period).sum() / df["volume"].rolling(period).sum()


def ichimoku(df, tenkan=9, kijun=26):
    conv = (df["high"].rolling(tenkan).max() + df["low"].rolling(tenkan).min()) / 2
    base = (df["high"].rolling(kijun).max() + df["low"].rolling(kijun).min()) / 2
    return conv, base


def heikin_ashi(df):
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ho = np.zeros(len(df))
    ho[0] = df["open"].iloc[0]
    hc = ha_close.values
    for i in range(1, len(df)):
        ho[i] = (ho[i - 1] + hc[i - 1]) / 2
    return pd.Series(ho, index=df.index), ha_close


def awesome(df):
    mp = (df["high"] + df["low"]) / 2
    return mp.rolling(5).mean() - mp.rolling(34).mean()


def trix(s, period=15):
    e = ema(ema(ema(s, period), period), period)
    return 100 * e.pct_change()
