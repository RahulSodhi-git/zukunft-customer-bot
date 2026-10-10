import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

import autobot
import bot
import data
import signals
from backtest import run
from indicators import cci
from strategies import sma_cross
from strategies import STRATEGIES, bollinger_meanrev, zscore_meanrev
from trader import coinalyze
from trader.client import BinanceTrader, PositionUnknown


def candles(prices):
    return pd.DataFrame({"time": pd.date_range("2026-01-01", periods=len(prices), freq="4h"),
                         "open": prices, "high": prices, "low": prices, "close": prices,
                         "volume": np.ones(len(prices))})


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        Path("risk_rules.json").write_bytes((Path(bot.__file__).parent / "risk_rules.json").read_bytes())
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, self.cwd)
        for target, kwargs in [
            ("requests.sessions.Session.request", {"side_effect": AssertionError("Network disabled in tests")}),
            ("trader.client.Client", {"side_effect": AssertionError("Real exchange clients disabled")}),
            ("bot.log", {}), ("bot.note", {}),
        ]:
            p = patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        for name, value in {"LAST_CLOSE": {}, "LOSS_TIMES": {}, "RECENT_OPENS": [],
                            "LAST_NORMAL_CLOSE": [0.0], "_BTC24": {"ts": 0, "pct": None},
                            "GAINER_PREV_TOP": None, "GAINER_LAST_BAR": [0]}.items():
            p = patch.object(bot, name, value)
            p.start()
            self.addCleanup(p.stop)
        coinalyze._OI.clear()
        coinalyze._MAP.update(m={}, ts=0)
        coinalyze._CALLS.clear()
        coinalyze._BLOCKED_UNTIL = 0
        self.trader = BinanceTrader.__new__(BinanceTrader)
        self.trader.testnet = True
        self.trader.dry_run = False
        self.trader.fut = Mock()

    def history(self, minutes, end=1799700):
        times = range(end - minutes * 60, end + 1, 300)
        rows = [{"t": t, "c": 100} for t in times]
        rows[-1]["c"] = 105
        return rows

    def prime_response(self, history):
        coinalyze._MAP.update(m={"BTCUSDT": "BTCUSDT_PERP.A"}, ts=1800000)
        return patch.object(coinalyze, "_request", return_value=[{"symbol": "BTCUSDT_PERP.A", "history": history}])

    def test_coinalyze_preferred_for_both_lookbacks(self):
        for hours in (0.75, 4):
            fallback = Mock()
            with patch("time.time", return_value=1800000), self.prime_response(self.history(round(hours * 60))):
                value, source = coinalyze.oi_change("BTCUSDT", hours, fallback)
            self.assertAlmostEqual(value, 5)
            self.assertEqual(source, "coinalyze")
            fallback.oi_change_pct.assert_not_called()

    def test_valid_negative_primary_never_uses_better_fallback(self):
        rows = self.history(45)
        rows[-1]["c"] = 95
        fallback = Mock()
        with patch("time.time", return_value=1800000), self.prime_response(rows):
            value, source = coinalyze.oi_change("BTCUSDT", 0.75, fallback)
        self.assertAlmostEqual(value, -5)
        self.assertEqual(source, "coinalyze")
        fallback.oi_change_pct.assert_not_called()

    def test_bad_primary_falls_back(self):
        cases = [[], self.history(45)[:-1], self.history(45)[1:], [{"t": None, "c": 4}],
                 [{"t": 1799700, "c": float("nan")}]]
        for rows in cases:
            coinalyze._OI.clear()
            fallback = Mock(oi_change_pct=Mock(return_value=3.0))
            with patch("time.time", return_value=1800000), self.prime_response(rows):
                self.assertEqual(coinalyze.oi_change("BTCUSDT", 0.75, fallback), (3.0, "binance"))
            fallback.oi_change_pct.assert_called_once_with("BTCUSDT", 0.75)

    def test_both_sources_fail(self):
        with patch.object(coinalyze, "prime"), patch("time.time", return_value=1800000):
            for value in (None, float("nan"), float("inf")):
                fallback = Mock(oi_change_pct=Mock(return_value=value))
                self.assertEqual(coinalyze.oi_change("BTCUSDT", 4, fallback), (None, "none"))

    def test_cache_is_separate_by_lookback(self):
        with patch("time.time", return_value=1800000), self.prime_response(self.history(45)):
            self.assertEqual(coinalyze.oi_change("BTCUSDT", 0.75)[1], "coinalyze")
            self.assertEqual(coinalyze.oi_change("BTCUSDT", 4), (None, "none"))

    def test_rate_limit_does_not_sleep_or_keep_requesting(self):
        response = Mock(status_code=429, headers={"Retry-After": "60"})
        with patch.dict(os.environ, {"COINALYZE_KEY": "dummy"}), patch.object(coinalyze._SESS, "get", return_value=response) as get:
            self.assertIsNone(coinalyze._request("/future-markets", {}))
            self.assertIsNone(coinalyze._request("/future-markets", {}))
            self.assertEqual(get.call_count, 1)

    def test_binance_exact_lookbacks(self):
        for hours, minutes in ((0.75, 45), (4, 240)):
            rows = [{"timestamp": x["t"] * 1000, "sumOpenInterest": x["c"]} for x in self.history(minutes)]
            response = Mock(json=Mock(return_value=rows))
            with patch("time.time", return_value=1800000), patch("trader.client.requests.get", return_value=response) as get:
                self.assertAlmostEqual(self.trader.oi_change_pct("BTCUSDT", hours), 5)
                params = get.call_args.kwargs["params"]
                self.assertEqual(params["limit"], minutes // 5 + 1)
                self.assertEqual(params["endTime"] - params["startTime"], minutes * 60000)

    def test_binance_stale_rejected(self):
        response = Mock(json=Mock(return_value=[]))
        with patch("trader.client.requests.get", return_value=response), patch("time.sleep"):
            self.assertIsNone(self.trader.oi_change_pct("BTCUSDT", 4))

    def test_legacy_oi_helper_uses_primary(self):
        with patch.object(coinalyze, "oi_change", return_value=(3, "coinalyze")) as change:
            self.assertTrue(self.trader.oi_rising("BTCUSDT", 2, 45))
            change.assert_called_once_with("BTCUSDT", 0.75, self.trader)

    def test_normal_entry_uses_45_minute_primary_gate(self):
        trader = Mock(futures_positions=Mock(return_value=[]), futures_price=Mock(return_value=100))
        trader._pending_entries.return_value = {}
        df = candles([100] * 500)
        best = {"name": "A", "fn": lambda d: np.ones(len(d)), "ret": 10, "trades": 20,
                "signal": 1, "confirmations": 3}
        with patch.object(bot, "get_ohlcv", return_value=df), patch.object(bot, "crypto_perp_symbols", return_value={"TESTUSDT"}), \
                patch.object(bot, "pick_best_strategy", return_value=best), patch.object(coinalyze, "oi_change", return_value=(1, "coinalyze")) as change:
            bot.step(trader, "TESTUSDT", 100, {"normal_require_oi_rising": True}, {})
        change.assert_called_once_with("TESTUSDT", 0.75, trader)
        trader.futures_market_order.assert_not_called()

    def test_special_and_gainer_use_four_hour_primary_gate(self):
        trader = Mock(tradable_symbols=Mock(return_value={"TESTUSDT"}))
        with patch.object(coinalyze, "oi_change", return_value=(1, "coinalyze")) as change:
            self.assertFalse(bot._gainer_oi_ok(trader, "TESTUSDT", {}))
            change.assert_called_once_with("TESTUSDT", 4.0, trader)
        with patch.object(bot, "_special_candidates", return_value=[{"symbol": "TESTUSDT", "direction": 1}]), \
                patch.object(coinalyze, "prime"), patch.object(coinalyze, "oi_change", return_value=(1, "coinalyze")) as change:
            bot._do_special_trades(trader, {"special_require_oi_rising": True}, {}, 1000, set(), 10)
            change.assert_called_once_with("TESTUSDT", 4.0, trader)
        trader.futures_market_order.assert_not_called()

    def test_unknown_is_never_flat(self):
        for rows in (None, [], {}, [{"symbol": "BTCUSDT", "positionAmt": None}],
                     [{"symbol": "WRONG", "positionAmt": "0"}],
                     [{"symbol": "BTCUSDT", "positionAmt": "nan"}],
                     [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "0"}],
                     [{"symbol": "BTCUSDT", "positionAmt": "0", "positionSide": "LONG"}]):
            self.trader.fut._request_futures_api.return_value = rows
            self.assertFalse(self.trader.is_flat("BTCUSDT"))
            with self.assertRaises(PositionUnknown):
                self.trader._position_amt_entry("BTCUSDT")

    def test_explicit_zero_is_flat(self):
        self.trader.fut._request_futures_api.return_value = [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        self.assertTrue(self.trader.is_flat("BTCUSDT"))

    def prepare_entry(self):
        self.trader._set_isolated = Mock()
        self.trader._mkt_lot = Mock(return_value=("1", "1", "2"))
        self.trader.fut.futures_symbol_ticker.return_value = {"price": "100"}

    def test_chunked_entry_waits_for_all_fills(self):
        self.prepare_entry()
        self.trader._position_amt_entry = Mock(side_effect=[(0, 0), (0, 0), (2, 100), (5, 101), (5, 101), (5, 101)])
        self.trader._settled_order = Mock(side_effect=[{"executedQty": x} for x in (2, 2, 1)])
        with patch("time.sleep"):
            result = self.trader.futures_market_order("BTCUSDT", "BUY", 100, 5)
        self.assertEqual(result["filled_qty"], 5)
        self.assertEqual(result["margin"], 101)
        self.assertEqual(self.trader.fut.futures_create_order.call_count, 3)
        self.assertEqual(self.trader._pending_entries(), {})

    def test_uncertain_entry_is_persisted_and_never_duplicated(self):
        self.prepare_entry()
        self.trader._position_amt_entry = Mock(return_value=(0, 0))
        self.trader._settled_order = Mock(side_effect=PositionUnknown("timeout"))
        for _ in range(2):
            with self.assertRaises(PositionUnknown):
                self.trader.futures_market_order("BTCUSDT", "BUY", 100, 5)
        self.assertEqual(self.trader.fut.futures_create_order.call_count, 1)
        self.assertEqual(len(self.trader._pending_entries()["BTCUSDT"]), 1)

    def test_partial_uses_actual_quantity_and_price(self):
        self.trader._position_amt_entry = Mock(side_effect=[(5, 100), (3, 100)])
        self.trader._mkt_lot = Mock(return_value=("1", "1", "100"))
        self.trader._reduce_only = Mock()
        self.trader._last_reductions = {"BTCUSDT": [{"executedQty": "2", "cumQuote": "220"}]}
        with patch("time.sleep"):
            result = self.trader.partial_close("BTCUSDT", 0.5)
        self.assertEqual(result["fraction_filled"], 0.4)
        self.assertEqual(result["remaining_qty"], 3)
        self.assertEqual(result["avg_price"], 110)
        self.trader._reduce_only.assert_called_once_with("BTCUSDT", "SELL", 2)

    def test_unfilled_partial_is_not_booked(self):
        pos = {"entry": 100, "side": 1, "leverage": 5, "gainer": True, "special": True,
               "partial_done": False, "size": 100, "target_margin": 100, "sl": 96}
        trader = Mock(partial_close=Mock(return_value={"partial_closed_qty": 0}))
        with patch.object(bot, "save_state"), patch.object(bot, "_record_partial") as record:
            bot._special_manage_stop(trader, "BTCUSDT", pos, 106, {}, {"BTCUSDT": pos})
        record.assert_not_called()
        self.assertFalse(pos["partial_done"])
        self.assertEqual(pos["size"], 100)

    def test_stop_acknowledgement_is_not_confirmation(self):
        self.trader._stop_price = Mock(return_value=96)
        self.trader._has_stop = Mock(return_value=False)
        result = self.trader.place_stops("BTCUSDT", 1, None, 96)
        self.assertEqual(result["STOP_MARKET"], "unverified")

    def test_existing_verified_stop_not_duplicated(self):
        self.trader._stop_price = Mock(return_value=96)
        self.trader._has_stop = Mock(return_value=True)
        self.assertEqual(self.trader.place_stops("BTCUSDT", 1, None, 96)["STOP_MARKET"], "placed")
        self.trader.fut.futures_create_order.assert_not_called()

    def test_algo_stop_fallback_verified(self):
        self.trader._stop_price = Mock(return_value=96)
        self.trader._has_stop = Mock(side_effect=[False, True])
        exc = RuntimeError("Conditional endpoint required")
        exc.code = -4120
        self.trader.fut.futures_create_order.side_effect = exc
        self.assertEqual(self.trader.place_stops("BTCUSDT", 1, None, 96)["STOP_MARKET"], "placed")
        self.trader.fut.futures_create_algo_order.assert_called_once()

    def test_tick_rounding(self):
        self.trader.fut.futures_exchange_info.return_value = {"symbols": [{"symbol": "BTCUSDT", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.05"}]}]}
        self.assertEqual(self.trader._stop_price("BTCUSDT", 96.13, 1), 96.15)
        self.assertEqual(self.trader._stop_price("BTCUSDT", 96.13, -1), 96.10)

    def test_stop_lookup_rejects_canceled_and_wrong_side(self):
        order = {"symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET", "closePosition": True,
                 "stopPrice": "96", "status": "CANCELED"}
        self.trader.fut.futures_get_open_orders.return_value = [order]
        self.trader.fut.futures_get_open_algo_orders.return_value = []
        self.assertFalse(self.trader._has_stop("BTCUSDT", 1, "STOP_MARKET", 96))
        order["status"] = "NEW"
        self.assertTrue(self.trader._has_stop("BTCUSDT", 1, "STOP_MARKET", 96))
        self.assertFalse(self.trader._has_stop("BTCUSDT", -1, "STOP_MARKET", 96))

    def test_uncertain_close_does_not_escalate(self):
        self.trader._position_amt_entry = Mock(return_value=(1, 100))
        self.trader._reduce_only = Mock(side_effect=PositionUnknown("pending"))
        self.assertFalse(self.trader.force_close("BTCUSDT"))
        self.assertEqual(self.trader._reduce_only.call_count, 1)

    def test_close_fallback_rounds_into_valid_price_band(self):
        self.trader._position_amt_entry = Mock(return_value=(15949, 0.04))
        self.trader._reduce_only = Mock(side_effect=[RuntimeError("-2022"), 15949])
        self.trader.fut.futures_mark_price.return_value = {"markPrice": "0.04005254"}
        self.trader._percent_price_bounds = Mock(return_value=(1.05, 0.95))
        self.trader.fut.futures_exchange_info.return_value = {"symbols": [{"symbol": "KERNELUSDT", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.0001000"}]}]}
        self.trader.is_flat = Mock(return_value=True)
        with patch("time.sleep"):
            self.assertTrue(self.trader.force_close("KERNELUSDT"))
        self.assertEqual(self.trader._reduce_only.call_args.kwargs["price"], 0.0381)
        self.assertIsNone(self.trader.last_close_error)

    def test_failed_close_preserves_actual_exchange_errors(self):
        self.trader._position_amt_entry = Mock(return_value=(15949, 0.04))
        self.trader._reduce_only = Mock(side_effect=[RuntimeError("-2022"), RuntimeError("-4014")])
        self.trader.fut.futures_mark_price.return_value = {"markPrice": "0.04"}
        self.trader._percent_price_bounds = Mock(return_value=(1.05, 0.95))
        self.trader._stop_price = Mock(return_value=0.038)
        self.trader.is_flat = Mock(return_value=False)
        with patch("time.sleep"):
            self.assertFalse(self.trader.force_close("KERNELUSDT"))
        self.assertIn("-2022", self.trader.last_close_error)
        self.assertIn("-4014", self.trader.last_close_error)

    def test_unknown_fill_cannot_create_fictional_state(self):
        for result in ({}, {"dry_run": True}, {"margin": 100, "filled_qty": 0}):
            with self.assertRaises(ValueError):
                bot._filled_margin(result, 100)

    def test_watcher_runs_during_slow_read(self):
        with patch.object(bot, "watch_positions") as watch:
            result = bot._read_with_watch(Mock(), {"watch_seconds": 0.1}, {}, lambda: (time.sleep(0.25), 7)[1])
        self.assertEqual(result, 7)
        self.assertGreaterEqual(watch.call_count, 2)

    def test_guard_timers_survive_reload(self):
        now = time.time()
        bot.RECENT_OPENS[:] = [now]
        bot.LAST_NORMAL_CLOSE[0] = now
        bot.LAST_CLOSE["BTCUSDT"] = (now, "4h", False)
        bot._save_reentry()
        bot.RECENT_OPENS.clear()
        bot.LAST_NORMAL_CLOSE[0] = 0
        bot.LAST_CLOSE.clear()
        bot._load_reentry()
        self.assertEqual(bot.RECENT_OPENS, [now])
        self.assertEqual(bot.LAST_NORMAL_CLOSE[0], now)
        self.assertIn("BTCUSDT", bot.LAST_CLOSE)

    def test_gainer_list_updates_with_full_slots(self):
        state = {"A": {"gainer": True}, "B": {"gainer": True}}
        with patch.object(bot, "_gainer_candidates", return_value=[]) as candidates:
            bot._do_gainer_trades(Mock(), {"gainer_slots": 2}, state, 1000, 2)
        candidates.assert_called_once()

    def test_gainer_gap_only_reseeds(self):
        bot.GAINER_PREV_TOP = {"OLD"}
        bot.GAINER_LAST_BAR[0] = 900
        with patch.object(bot, "_gainer_bar_now", return_value=3600), patch.object(data, "top_gainers", return_value=["NEW"]):
            self.assertEqual(bot._gainer_candidates({}, {}), [])
        self.assertEqual(bot.GAINER_PREV_TOP, {"NEW"})

    def test_expired_btc_does_not_confirm_gate(self):
        bot._BTC24.update(ts=1, pct=10)
        with patch.object(data, "_get", side_effect=RuntimeError("offline")):
            self.assertIsNone(bot._btc_24h_change({}))
            self.assertFalse(bot._btc_regime_ok({"gainer_btc24_filter": True, "btc24_fail_closed": True}, "gainer"))

    def test_disabled_news_not_fetched(self):
        news = SimpleNamespace(get_news_signals=Mock())
        with patch.dict("sys.modules", {"news": news}), patch.object(data, "quote_volumes", return_value={}), \
                patch.object(data, "high_volatility_coins", return_value=[]), patch.object(bot, "crypto_perp_symbols", return_value=set()):
            self.assertEqual(bot._special_candidates({"news_enabled": False}, set(), {}), [])
        news.get_news_signals.assert_not_called()

    def test_quantity_desync_keeps_state_and_stop_until_flat(self):
        trader = Mock()
        trader._position_amt_entry.return_value = (4, 100)
        trader.reconcile_flatten.return_value = (False, "pending")
        state = {"BTCUSDT": {"side": 1, "qty": 5}}
        bot._reconcile_positions(trader, state, {})
        trader.reconcile_flatten.assert_called_once_with("BTCUSDT")
        trader.cancel_open_orders.assert_not_called()
        self.assertIn("BTCUSDT", state)

    def test_cci_uses_one_window_mean_deviation(self):
        df = candles(np.arange(1, 31, dtype=float) ** 2)
        window = df.close.iloc[-20:].to_numpy()
        expected = (window[-1] - window.mean()) / (0.015 * np.abs(window - window.mean()).mean())
        self.assertAlmostEqual(cci(df).iloc[-1], expected)

    def test_warmup_does_not_trade(self):
        df = candles(np.arange(1, 221, dtype=float))
        self.assertTrue((signals.strategy_positions(lambda d: np.ones(len(d)), df)[:200] == 0).all())
        self.assertTrue((sma_cross(df)[:49] == 0).all())

    def test_ranking_uses_reliability_score(self):
        df = candles([100] * 220)
        registry = {name: (lambda d: np.ones(len(d)), "Medium") for name in ("A", "B")}
        stats = [{"trades": 20, "return_pct": 10, "win_rate": 90}, {"trades": 20, "return_pct": 15, "win_rate": 10}]
        with patch.object(signals, "STRATEGIES", registry), patch.object(signals, "run", side_effect=stats):
            self.assertEqual(signals.pick_best_strategy(df)["name"], "A")

    def test_exact_duplicate_confirmation_counted_once(self):
        df = candles([100] * 220)
        registry = {name: (lambda d: np.ones(len(d)), "Medium") for name in ("Bollinger Mean-Reversion", "Z-Score Mean-Reversion")}
        with patch.object(signals, "STRATEGIES", registry), patch.object(signals, "run", return_value={"trades": 20, "return_pct": 10, "win_rate": 90}):
            self.assertEqual(signals.pick_best_strategy(df)["confirmations"], 1)

    def test_indicators_are_causal_and_duplicate_votes_match(self):
        rng = np.random.default_rng(7)
        df = candles(100 * np.exp(np.cumsum(rng.normal(0, 0.015, 500))))
        df["high"] *= 1.01
        df["low"] *= 0.99
        df["volume"] = rng.uniform(100, 1000, len(df))
        for name, (fn, _) in STRATEGIES.items():
            with self.subTest(strategy=name):
                full = np.asarray(fn(df))
                prefix = np.asarray(fn(df.iloc[:350]))
                np.testing.assert_allclose(full[:350], prefix, equal_nan=True)
        np.testing.assert_array_equal(bollinger_meanrev(df), zscore_meanrev(df))

    def test_leverage_is_evaluated_inside_configured_band(self):
        with patch.object(signals, "run", return_value={"return_pct": 1, "liquidated": False}) as backtest:
            lev = signals.auto_leverage(candles([100] * 220), np.ones(220), {"normal_leverage_min": 3, "normal_max_leverage": 5})
        self.assertEqual(lev, 3)
        self.assertEqual([call.kwargs["leverage"] for call in backtest.call_args_list], [3, 5])

    def test_daily_trend_toggle_is_honored(self):
        df = candles([100] * 220)
        result = {"return_pct": 10, "gross_return_pct": 11, "fees_pct": 1, "trades": 20, "win_rate": 60, "max_dd": -5}
        chosen = {"name": "A", "signal": 1, "score": 10, "confirmations": 3, "backtest": result}
        with patch.object(autobot, "_fetch", side_effect=lambda task: (*task, df)), \
                patch.object(autobot, "pick_best_strategy", return_value=chosen), \
                patch.object(autobot, "STRATEGIES", {"A": (lambda d: np.ones(len(d)), "Medium")}), \
                patch.object(autobot, "auto_leverage", return_value=3), patch.object(signals, "higher_tf_trend", return_value=-1):
            self.assertEqual(len(autobot.scan_and_rank(["BTCUSDT"], rules={"normal_trend_filter": False})), 1)
            self.assertEqual(autobot.scan_and_rank(["BTCUSDT"], rules={"normal_trend_filter": True}), [])

    def test_next_open_and_fixed_quantity(self):
        df = candles([100, 110, 121])
        result = run(df, [1, 1, 1], fee=0, leverage=2)
        self.assertAlmostEqual(result["return_pct"], 20)
        self.assertEqual(result["trades"], 1)

    def test_zero_policy_and_fees(self):
        df = candles([100, 100, 110, 120])
        hold = run(df, [1, 0, 0, 0], fee=0)
        leave = run(df, [1, 0, 0, 0], fee=0, zero_policy="exit")
        self.assertAlmostEqual(hold["return_pct"], 20)
        self.assertAlmostEqual(leave["return_pct"], 10)
        paid = run(candles([100, 100, 100]), [1, 1, 1], fee=0.001)
        self.assertAlmostEqual(paid["return_pct"], -0.2)
        self.assertAlmostEqual(sum(paid["trade_pnls"]), -0.002)

    def test_ambiguous_candle_stop_first(self):
        df = candles([100, 100])
        df.loc[1, "high"] = 130
        df.loc[1, "low"] = 95
        result = run(df, [1, 1], leverage=5, fee=0,
                     exit_rules={"normal_exit_mode": "ladder", "normal_target_margin_pct": 100, "stop_loss_pct": 20})
        self.assertAlmostEqual(result["return_pct"], -20)

    def test_liquidated_trade_cannot_recover_later(self):
        result = run(candles([100, 100, 40, 200]), [1, 1, 1, 1], leverage=2, fee=0)
        self.assertTrue(result["liquidated"])
        self.assertEqual(result["return_pct"], -100)


    def test_stop_status_accepts_legacy_rounding(self):
        self.trader.fut.futures_get_open_algo_orders.return_value = [{
            "symbol": "ZENUSDT", "side": "BUY", "orderType": "STOP_MARKET",
            "algoStatus": "NEW", "closePosition": True, "triggerPrice": "7.24", "algoId": 12}]
        self.assertEqual(self.trader.stop_status("ZENUSDT", -1, 7.2396929)["status"], "active")

    def test_rejected_stop_preserves_exchange_reason(self):
        self.trader.fut.futures_get_open_algo_orders.return_value = []
        self.trader.fut.futures_get_open_orders.return_value = []
        self.trader.fut.futures_get_all_algo_orders.return_value = [{
            "side": "SELL", "orderType": "STOP_MARKET", "closePosition": True,
            "algoStatus": "REJECTED", "createTime": 10000, "rejectReason": "PERCENT_PRICE"}]
        status = self.trader.stop_status("MINAUSDT", 1, .104477, 9000)
        self.assertEqual(status, {"status": "terminal", "reason": "PERCENT_PRICE", "id": None})

    def test_missing_and_unavailable_stops_are_distinct(self):
        for name in ("futures_get_open_algo_orders", "futures_get_open_orders", "futures_get_all_algo_orders"):
            getattr(self.trader.fut, name).return_value = []
        self.assertEqual(self.trader.stop_status("MINAUSDT", 1, .1)["status"], "missing")
        self.trader.fut.futures_get_open_algo_orders.side_effect = TimeoutError()
        self.assertEqual(self.trader.stop_status("MINAUSDT", 1, .1)["status"], "unknown")

    def test_mina_rejection_retries_after_rebound_and_restart(self):
        pos = {"side": 1, "entry": .1088, "sl": .104477, "qty": 10, "open_epoch": 1,
               "stop_placed": True}
        state = {"MINAUSDT": pos}
        trader = Mock(futures_prices_all=Mock(return_value={"MINAUSDT": .11}),
                      stop_status=Mock(return_value={"status": "terminal", "reason": "PERCENT_PRICE"}),
                      force_close=Mock(return_value=False))
        with patch("time.time", return_value=1000):
            bot.watch_positions(trader, {}, state)
        self.assertIn("close_intent", pos)
        self.assertFalse(bot._entry_safety_ok(trader, state))
        state = json.loads(json.dumps(state))
        trader.futures_prices_all.side_effect = TimeoutError()
        with patch("time.time", return_value=1003):
            bot.watch_positions(trader, {}, state)
        self.assertEqual(trader.force_close.call_count, 2)
        self.assertEqual(trader.stop_status.call_count, 2)
        self.assertIn("MINAUSDT", state)

    def test_unknown_protection_timeout_latches_exit(self):
        pos = {"side": 1, "entry": 1, "sl": .96}
        state = {"TESTUSDT": pos}
        trader = Mock(stop_status=Mock(side_effect=TimeoutError("offline")))
        with patch("time.time", return_value=1000):
            bot._check_protection(trader, "TESTUSDT", pos, {}, state)
        self.assertNotIn("close_intent", pos)
        with patch("time.time", return_value=1016):
            bot._check_protection(trader, "TESTUSDT", pos, {}, state)
        self.assertIn("close_intent", pos)

    def test_close_records_fill_not_ticker_and_retains_missing_history(self):
        pos = {"side": 1, "entry": .1088, "qty": 10, "open_epoch": 1}
        state = {"MINAUSDT": pos}
        trader = Mock(force_close=Mock(return_value=True), exit_fill_price=Mock(side_effect=PositionUnknown("lag")))
        with patch("time.time", return_value=1000), patch.object(bot, "_record_close") as record:
            with self.assertRaises(PositionUnknown):
                bot._safe_close(trader, "MINAUSDT", pos, "stop rejected", .1026, state)
            record.assert_not_called()
        self.assertIn("MINAUSDT", state)
        trader.exit_fill_price.side_effect = None
        trader.exit_fill_price.return_value = .1008
        with patch("time.time", return_value=1003), patch.object(bot, "_record_close") as record:
            self.assertTrue(bot._safe_close(trader, "MINAUSDT", pos, "other", .1026, state))
            self.assertEqual(record.call_args.args[2:4], (.1008, "stop rejected"))
        self.assertFalse(state)

    def test_exit_vwap_covers_chunks_without_previous_partial(self):
        self.trader.fut.futures_account_trades.return_value = [
            {"side": "SELL", "time": 20, "id": 1, "orderId": 1, "qty": "10", "price": ".12"},
            {"side": "SELL", "time": 30, "id": 2, "orderId": 2, "qty": "4", "price": ".102"},
            {"side": "SELL", "time": 31, "id": 3, "orderId": 3, "qty": "6", "price": ".1"}]
        self.assertAlmostEqual(self.trader.exit_fill_price("MINAUSDT", 10, 1, 10), .1008)
        with self.assertRaises(PositionUnknown):
            self.trader.exit_fill_price("MINAUSDT", 10, 1, 21)

    def test_emergency_blocks_all_entry_types(self):
        trader = Mock()
        state = {"MINAUSDT": {"close_intent": {"reason": "rejected"}, "entry": 1}}
        bot.step(trader, "TESTUSDT", 100, {}, state)
        for gainer in (False, True):
            self.assertFalse(bot._open_special_trade(trader, "TESTUSDT", 1, 100, {}, state, "test", gainer))
        self.assertFalse(bot._open_news_trade(trader, "TESTUSDT", 1, 100, {"news_enabled": True}, state, "test"))
        trader.futures_market_order.assert_not_called()

    def test_limit_fallback_is_reduce_only_ioc_with_actual_partial(self):
        self.trader._mkt_lot = Mock(return_value=("1", "1", "100"))
        self.trader._position_amt_entry = Mock(return_value=(10, 1))
        self.trader.fut.futures_create_order.return_value = {"orderId": 1}
        self.trader.fut.futures_get_order.return_value = {"status": "EXPIRED", "executedQty": "4"}
        self.assertEqual(self.trader._reduce_only("TESTUSDT", "SELL", 10, price=1), 4)
        kwargs = self.trader.fut.futures_create_order.call_args.kwargs
        self.assertTrue(kwargs["reduceOnly"])
        self.assertEqual(kwargs["timeInForce"], "IOC")
        self.trader.fut.futures_create_order.assert_called_once()

    def test_entry_liquidity_rejects_spread_depth_and_price_band(self):
        self.trader._percent_price_bounds = Mock(return_value=(1.05, .95))
        self.trader.fut.futures_mark_price.return_value = {"markPrice": "100"}
        book = {"bids": [["100", "100"]], "asks": [["100.1", "100"]]}
        self.trader.fut.futures_order_book.return_value = book
        self.assertTrue(self.trader.check_entry_liquidity("TESTUSDT", 1, 100, 5))
        for asks in ([["101", "100"]], [["100.1", "1"]], [["106", "100"]]):
            book["asks"] = asks
            with self.assertRaises(ValueError):
                self.trader.check_entry_liquidity("TESTUSDT", 1, 100, 5, max_spread_pct=10 if asks[0][0] == "106" else .5)

    def test_entry_state_is_durable_before_stop_submission_failure(self):
        for gainer in (False, True):
            state = {}
            trader = Mock(_pending_entries=Mock(return_value={}), futures_price=Mock(return_value=100),
                          futures_market_order=Mock(return_value={"filled_qty": 5, "avg_price": 100, "margin": 100}))
            def fail_stop(*args):
                with open(bot.STATE_FILE) as handle:
                    self.assertIn("TESTUSDT", json.load(handle))
                raise TimeoutError("stop submission uncertain")
            trader.place_stops.side_effect = fail_stop
            with patch.object(bot, "_special_leverage", return_value=5), patch.object(bot.guards, "check_order"):
                self.assertFalse(bot._open_special_trade(trader, "TESTUSDT", 1, 100, {}, state, "test", gainer))
            self.assertIn("TESTUSDT", state)
            self.assertFalse(bot._entry_safety_ok(trader, state))

    def test_history_failure_keeps_durable_exit(self):
        pos = {"side": 1, "entry": 1, "qty": 10}
        state = {"TESTUSDT": pos}
        trader = Mock(force_close=Mock(return_value=True), exit_fill_price=Mock(return_value=.96))
        with patch.object(bot, "_record_close", return_value=False):
            self.assertFalse(bot._safe_close(trader, "TESTUSDT", pos, "stop", 1, state))
        self.assertIn("close_intent", state["TESTUSDT"])

    def test_pending_exit_updates_quantity_preserves_full_leg_and_backs_off(self):
        pos = {"side": 1, "entry": 1, "qty": 10, "size": 100, "open_epoch": 1}
        state = {"TESTUSDT": pos}
        trader = Mock(force_close=Mock(return_value=False), last_close_error="-2022",
                      _position_amt_entry=Mock(return_value=(6, 1)))
        with patch("time.time", return_value=1000):
            self.assertFalse(bot._safe_close(trader, "TESTUSDT", pos, "stop", 1, state))
        self.assertEqual(pos["qty"], 6)
        self.assertEqual(pos["size"], 60)
        state = json.loads(json.dumps(state))
        pos = state["TESTUSDT"]
        for now in (1003, 1004, 1007):
            with patch("time.time", return_value=now):
                bot._safe_close(trader, "TESTUSDT", pos, "other", 1, state)
        self.assertEqual(trader.force_close.call_count, 3)
        self.assertTrue(pos["close_intent"]["manual_attention"])
        self.assertFalse(bot._entry_safety_ok(trader, state))
        trader.force_close.return_value = True
        trader.exit_fill_price.return_value = .95
        with patch("time.time", return_value=1015), patch.object(bot, "_record_close") as record:
            self.assertTrue(bot._safe_close(trader, "TESTUSDT", pos, "other", 1, state))
            self.assertEqual(record.call_args.args[0]["size"], 100)
            self.assertEqual(record.call_args.args[0]["qty"], 10)
            trader.exit_fill_price.assert_called_once_with("TESTUSDT", 1000, 1, 10)
        self.assertFalse(state)

    def test_failed_close_exception_is_durable_and_position_unknown_not_flat(self):
        pos = {"side": 1, "entry": 1, "qty": 10, "size": 100}
        state = {"TESTUSDT": pos}
        trader = Mock(force_close=Mock(side_effect=TimeoutError("offline")),
                      _position_amt_entry=Mock(side_effect=PositionUnknown("offline")))
        with self.assertRaises(TimeoutError):
            bot._safe_close(trader, "TESTUSDT", pos, "stop", 1, state)
        with open(bot.STATE_FILE) as handle:
            saved = json.load(handle)["TESTUSDT"]
        self.assertEqual(saved["qty"], 10)
        self.assertEqual(saved["close_intent"]["last_error"], "offline")
        self.assertIsNone(saved["quantity_checked_at"])

    def test_quarantine_blocks_direct_special_gainer_and_news_entries(self):
        trader = Mock()
        rules = {"exclude_symbols": ["KERNEL"], "news_enabled": True}
        self.assertTrue(bot._excluded("KERNELUSDT", rules))
        for gainer in (False, True):
            self.assertFalse(bot._open_special_trade(trader, "KERNELUSDT", 1, 100, rules, {}, "test", gainer))
        self.assertFalse(bot._open_news_trade(trader, "KERNELUSDT", 1, 100, rules, {}, "test"))
        trader.futures_market_order.assert_not_called()

    def test_fast_reads_do_not_starve_watcher(self):
        state = {"TESTUSDT": {"entry": 1}}
        with patch.object(bot, "_LAST_WATCH", [0]), patch("time.monotonic", return_value=100), \
                patch.object(bot, "watch_positions") as watch:
            self.assertEqual(bot._read_with_watch(Mock(), {}, state, lambda: 7), 7)
            self.assertGreaterEqual(watch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
