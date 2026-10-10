"""Closed-bar signals, next-open fills, and one ledger for returns and trade stats.

Pass exit_rules to simulate normal floors/ladder/trailing exits. Intrabar
stop/target ambiguity uses stop-first; funding is not estimated here.
"""
import numpy as np
import pandas as pd

from exit_rules import normal_stop, normal_target, normal_floor_pct


def run(df, positions, leverage=1, fee=0.0005, exit_rules=None, zero_policy='hold', warmup=0):
    if not np.isfinite(leverage) or leverage <= 0 or not np.isfinite(fee) or fee < 0:
        raise ValueError('Invalid leverage or fee')
    if zero_policy not in ('hold', 'exit'):
        raise ValueError('zero_policy must be hold or exit')
    signal = np.asarray(positions, float).copy()
    if len(signal) != len(df) or not np.isin(signal, [-1, 0, 1]).all():
        raise ValueError('Signals must be -1, 0 or 1 and match candle count')
    signal[:warmup] = 0
    cash = gross_cash = 1.0
    pos = None
    net_curve, gross_curve, trades = [], [], []
    opens = df.get('open', df['close']).to_numpy(float)
    closes = df['close'].to_numpy(float)
    highs = df.get('high', df['close']).to_numpy(float)
    lows = df.get('low', df['close']).to_numpy(float)
    if any(not np.isfinite(values).all() or (values <= 0).any() for values in (opens, closes, highs, lows)):
        raise ValueError('Prices must be finite and positive')
    liquidated = False

    def close(price):
        nonlocal pos, cash, gross_cash, liquidated
        profit = pos['qty'] * pos['side'] * (price - pos['entry'])
        cost = pos['qty'] * price * fee
        before = cash
        raw_cash = cash + profit - cost
        cash = max(0.0, raw_cash)
        gross_cash += profit + max(0.0, -raw_cash)
        trades.append(cash - before - pos['entry_fee'])
        liquidated |= cash <= 0
        pos = None

    for i, price in enumerate(closes):
        wanted = int(signal[i - 1]) if i else 0
        if pos and ((wanted and wanted != pos['side']) or (not wanted and zero_policy == 'exit')):
            close(opens[i])
        if pos is None and wanted and cash > 0:
            entry = opens[i]
            qty = cash * leverage / entry
            entry_fee = qty * entry * fee
            cash -= entry_fee
            pos = dict(entry=entry, side=wanted, qty=qty, entry_fee=entry_fee, leverage=leverage, peak=entry)
            if exit_rules:
                pos['sl'] = entry * (1 - wanted * normal_floor_pct(exit_rules, leverage) / 100)
        if pos and exit_rules:
            stop, _, _ = normal_stop(pos, pos['peak'], exit_rules)
            target = normal_target(pos, exit_rules)
            side = pos['side']
            adverse, favorable = (lows[i], highs[i]) if side > 0 else (highs[i], lows[i])
            if side * (opens[i] - stop) <= 0:
                close(opens[i])
            elif side * (adverse - stop) <= 0:
                close(stop)
            elif target is not None and side * (favorable - target) >= 0:
                close(opens[i] if side * (opens[i] - target) >= 0 else target)
            else:
                raised, armed, peak = normal_stop(pos, favorable, exit_rules)
                pos['peak'] = peak
                if armed and side * (adverse - raised) <= 0:
                    close(raised)
        if pos:
            adverse = lows[i] if pos['side'] > 0 else highs[i]
            if cash + pos['qty'] * pos['side'] * (adverse - pos['entry']) <= 0:
                close(pos['entry'] - pos['side'] * cash / pos['qty'])
                cash = 0.0
                liquidated = True
        if i == len(df) - 1 and pos:
            close(price)
        unrealized = pos['qty'] * pos['side'] * (price - pos['entry']) if pos else 0
        net_curve.append(cash + unrealized)
        gross_curve.append(gross_cash + unrealized)
    equity = pd.Series(net_curve, index=df.index, dtype=float)
    gross = pd.Series(gross_curve, index=df.index, dtype=float)
    peaks = equity.cummax().clip(lower=1)
    net = (equity.iloc[-1] - 1) * 100 if len(equity) else 0.0
    gross_return = (gross.iloc[-1] - 1) * 100 if len(gross) else 0.0
    return dict(equity=equity, return_pct=float(net), gross_return_pct=float(gross_return),
                fees_pct=round(float(gross_return - net), 2), trades=len(trades),
                win_rate=float(np.mean(np.array(trades) > 0) * 100) if trades else 0.0,
                max_dd=float(((equity / peaks - 1) * 100).min()) if len(equity) else 0.0,
                liquidated=bool(liquidated), trade_pnls=trades)
