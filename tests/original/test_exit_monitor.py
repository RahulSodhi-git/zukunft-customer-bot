import ast
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import bot
from exit_rules import normal_stop
import runtime_control as control
from trader.client import BinanceTrader
from trader.guards import RiskError, check_order

ROOT = Path(bot.__file__).parent


class ExitMonitorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        cwd = os.getcwd()
        os.chdir(tmp.name)
        Path("risk_rules.json").write_bytes((ROOT / "risk_rules.json").read_bytes())
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.chdir, cwd)
        for target in ("bot.log", "bot.note"):
            p = patch(target)
            p.start()
            self.addCleanup(p.stop)
        p = patch("requests.sessions.Session.request", side_effect=AssertionError("Network disabled"))
        p.start()
        self.addCleanup(p.stop)
        self.rules = {"normal_exit_mode": "ladder", "normal_target_margin_pct": 100,
                      "normal_stop_ladder": [[25, 0], [50, 25], [75, 50], [90, 75]],
                      "special_stop_ladder": [[25, 0], [50, 25], [75, 50]],
                      "special_stop_margin_pct": 10, "gainer_stop_margin_pct": 20}

    def position(self, side=1, kind="normal", lev=5):
        pos = {"entry": 100, "side": side, "leverage": lev, "qty": 5, "size": 100,
               "sl": 100 * (1 - side * .2 / lev), "stop_placed": True, "peak": 100}
        if kind != "normal":
            pos.update(special=True, target_margin=100, peak_margin=0, partial_done=True)
        if kind == "gainer":
            pos["gainer"] = True
        return pos

    def test_pause_persists_and_requires_authorization(self):
        self.assertFalse(control.entries_paused())
        with self.assertRaises(PermissionError):
            control.set_entries_paused(True, authorized=False)
        self.assertFalse(Path(control.CONTROL_FILE).exists())
        control.set_entries_paused(True, authorized=True)
        self.assertTrue(control.entries_paused())
        self.assertTrue(json.loads(Path(control.CONTROL_FILE).read_text())["entries_paused"])
        control.set_entries_paused(False, authorized=True)
        self.assertFalse(control.entries_paused())

    def test_invalid_control_fails_closed(self):
        for data in ("broken", "{}", 'null', '[]', '{"entries_paused":"false"}'):
            Path(control.CONTROL_FILE).write_text(data)
            self.assertTrue(control.entries_paused())

    def test_pause_blocks_every_entry_type_and_cli(self):
        control.set_entries_paused(True, authorized=True)
        trader = Mock()
        bot.step(trader, "TESTUSDT", 100, self.rules, {})
        for gainer in (False, True):
            self.assertFalse(bot._open_special_trade(trader, "TESTUSDT", 1, 100, self.rules, {}, "test", gainer))
        self.assertFalse(bot._open_news_trade(trader, "TESTUSDT", 1, 100, {"news_enabled": True}, {}, "test"))
        with self.assertRaises(RiskError):
            check_order("TESTUSDT", 100, 5)
        trader.futures_market_order.assert_not_called()

    def test_paused_watcher_closes_exact_target_for_all_types_and_sides(self):
        control.set_entries_paused(True, authorized=True)
        for kind in ("normal", "special", "gainer"):
            for side in (1, -1):
                for lev in (3, 5, 7):
                    with self.subTest(kind=kind, side=side, lev=lev):
                        pos = self.position(side, kind, lev)
                        price = 100 * (1 + side / lev)
                        trader = Mock(futures_prices_all=Mock(return_value={"TESTUSDT": price}))
                        with patch.object(bot, "_check_protection"), patch.object(bot, "_safe_close", return_value=True) as close, \
                                patch.object(bot, "_maintain_native_target"):
                            bot.watch_positions(trader, self.rules, {"TESTUSDT": pos})
                        close.assert_called_once()
                        self.assertIn("TP", close.call_args.args[3])
                        self.assertTrue(control.monitor_is_fresh())

    def test_all_ladder_boundaries_and_persisted_retracements(self):
        control.set_entries_paused(True, authorized=True)
        for kind in ("normal", "special", "gainer"):
            for side in (1, -1):
                for lev in (3, 5, 7):
                    ladder = self.rules["normal_stop_ladder" if kind == "normal" else "special_stop_ladder"]
                    for trigger, floor in ladder:
                        with self.subTest(kind=kind, side=side, lev=lev, trigger=trigger):
                            pos = self.position(side, kind, lev)
                            peak = 100 * (1 + side * trigger / lev / 100)
                            if kind == "normal":
                                stop, armed, pos["peak"] = normal_stop(pos, peak, self.rules)
                                self.assertTrue(armed)
                                self.assertAlmostEqual(stop, 100 * (1 + side * floor / lev / 100))
                            else:
                                with patch.object(bot, "_safe_close") as close:
                                    bot._special_manage_stop(Mock(), "TESTUSDT", pos, peak, self.rules, {"TESTUSDT": pos})
                                    close.assert_not_called()
                            pos = json.loads(json.dumps(pos))  # Restart preserves high-water marks.
                            price = 100 * (1 + side * floor / lev / 100)
                            trader = Mock(futures_prices_all=Mock(return_value={"TESTUSDT": price}))
                            with patch.object(bot, "_check_protection"), patch.object(bot, "_safe_close", return_value=True) as close, \
                                    patch.object(bot, "_maintain_native_target"):
                                bot.watch_positions(trader, self.rules, {"TESTUSDT": pos})
                            close.assert_called_once()
                            self.assertIn("stop", close.call_args.args[3])

    def test_paused_hard_stops_still_close(self):
        control.set_entries_paused(True, authorized=True)
        for kind in ("normal", "special", "gainer"):
            for side in (1, -1):
                pos = self.position(side, kind)
                loss = 10 if kind == "special" else 20
                trader = Mock(futures_prices_all=Mock(return_value={"TESTUSDT": 100 * (1 - side * loss / 500)}))
                with patch.object(bot, "_check_protection"), patch.object(bot, "_safe_close", return_value=True) as close, \
                        patch.object(bot, "_maintain_native_target"):
                    bot.watch_positions(trader, self.rules, {"TESTUSDT": pos})
                close.assert_called_once()

    def test_target_maintenance_retries_without_cancelling_hard_stop(self):
        pos = self.position()
        state = {"TESTUSDT": pos}
        trader = Mock(_position_amt_entry=Mock(return_value=(5, 100)),
                      place_stops=Mock(return_value={"TAKE_PROFIT_MARKET": "unverified"}))
        with patch("time.time", return_value=1000):
            bot._maintain_native_target(trader, self.rules, state)
            bot._maintain_native_target(trader, self.rules, state)
        trader.place_stops.assert_called_once_with("TESTUSDT", 1, 120, None)
        self.assertFalse(pos["tp_placed"])
        self.assertTrue(pos["stop_placed"])
        trader.place_stops.return_value = {"TAKE_PROFIT_MARKET": "placed"}
        with patch("time.time", return_value=1060):
            bot._maintain_native_target(trader, self.rules, state)
        self.assertTrue(pos["tp_placed"])
        trader.cancel_open_orders.assert_not_called()

    def test_flat_or_pending_position_does_not_get_new_target(self):
        pos = self.position()
        trader = Mock(_position_amt_entry=Mock(return_value=(0, 0)))
        with patch.object(bot, "_safe_close") as close:
            bot._maintain_native_target(trader, self.rules, {"TESTUSDT": pos})
            close.assert_called_once()
        pos.update(close_intent={"reason": "SL"}, tp_checked_at=0)
        bot._maintain_native_target(trader, self.rules, {"TESTUSDT": pos})
        trader.place_stops.assert_not_called()

    def test_software_exits_precede_native_maintenance(self):
        calls = []
        with patch.object(bot, "_watch_positions", side_effect=lambda *a: calls.append("exits")), \
                patch.object(bot, "_maintain_native_target", side_effect=lambda *a: calls.append("backstop")):
            bot.watch_positions(Mock(), {}, {})
        self.assertEqual(calls, ["exits", "backstop"])

    def test_heartbeat_expires_and_failed_pass_does_not_refresh_it(self):
        with patch("time.time", return_value=1000):
            control.record_monitor_pass(999, 1)
            self.assertTrue(control.monitor_is_fresh())
        with patch("time.time", return_value=1031), patch.object(bot, "_watch_positions", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                bot.watch_positions(Mock(), {}, {})
            self.assertFalse(control.monitor_is_fresh())

    def test_native_target_placement_verifies_without_cancellation(self):
        trader = BinanceTrader.__new__(BinanceTrader)
        trader.dry_run = False
        trader.fut = Mock()
        trader._stop_price = Mock(side_effect=lambda symbol, price, side: price)
        trader._has_stop = Mock(side_effect=[False, True])
        result = trader.place_stops("TESTUSDT", 1, 120, None)
        self.assertEqual(result, {"TAKE_PROFIT_MARKET": "placed"})
        trader.fut.futures_create_order.assert_called_once_with(
            symbol="TESTUSDT", side="SELL", type="TAKE_PROFIT_MARKET", stopPrice=120, closePosition=True)
        trader.fut.futures_cancel_all_open_orders.assert_not_called()
        trader.fut.futures_cancel_all_algo_open_orders.assert_not_called()

    def test_new_special_and_gainer_get_both_native_orders(self):
        for gainer in (False, True):
            trader = Mock(_pending_entries=Mock(return_value={}), futures_price=Mock(return_value=100),
                          futures_market_order=Mock(return_value={"filled_qty": 5, "avg_price": 100, "margin": 100}),
                          place_stops=Mock(return_value={"STOP_MARKET": "placed", "TAKE_PROFIT_MARKET": "placed"}))
            with patch.object(bot, "_special_leverage", return_value=5), patch.object(bot, "_can_open_now", return_value=True), \
                    patch.object(bot.guards, "check_order"), patch.object(bot, "_save_reentry"), \
                    patch.object(bot, "RECENT_OPENS", []):
                state = {}
                self.assertTrue(bot._open_special_trade(trader, "TESTUSDT", 1, 100, self.rules, state, "test", gainer))
            expected_sl = 96 if gainer else 98
            trader.place_stops.assert_called_once_with("TESTUSDT", 1, 120, expected_sl)
            self.assertTrue(state["TESTUSDT"]["tp_placed"])
            self.assertTrue(state["TESTUSDT"]["stop_placed"])

    def test_native_tp_history_requires_verified_child_fill(self):
        trader = BinanceTrader.__new__(BinanceTrader)
        trader.fut = Mock()
        trader.fut.futures_get_all_algo_orders.return_value = [{"symbol": "TESTUSDT", "side": "SELL",
            "orderType": "TAKE_PROFIT_MARKET", "closePosition": True, "createTime": 10000,
            "algoStatus": "FINISHED", "actualOrderId": 42}]
        trader.fut.futures_get_all_orders.return_value = []
        trader.fut.futures_get_order.return_value = {"status": "REJECTED"}
        self.assertFalse(trader.take_profit_filled("TESTUSDT", 10000, 1))
        trader.fut.futures_get_order.return_value = {"status": "FILLED"}
        self.assertTrue(trader.take_profit_filled("TESTUSDT", 10000, 1))

    def test_native_tp_preserves_special_cooldown_classification(self):
        pos = dict(self.position(kind="special"), tp_placed=True)
        trader = Mock(force_close=Mock(return_value=True), exit_fill_price=Mock(return_value=119.99),
                      take_profit_filled=Mock(return_value=True))
        with patch.object(bot, "_record_close", return_value=True) as record:
            self.assertTrue(bot._safe_close(trader, "TESTUSDT", pos, "closed on exchange", 119.99, {"TESTUSDT": pos}))
        self.assertTrue(record.call_args.args[3].startswith("special TP"))

    @unittest.skip("Original Streamlit dashboard is intentionally not part of the customer engine migration")
    def test_dashboard_has_no_process_stop_and_pause_is_authenticated(self):
        source = next((ROOT / "pages").glob("1_*Auto_Bot.py")).read_text(encoding="utf-8")
        self.assertNotIn('"stop", "vivek-bot"', source)
        self.assertNotIn("taskkill", source)
        tree = ast.parse(source)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "set_entries_paused"]
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(k.arg == "authorized" and isinstance(k.value, ast.Name)
                            and k.value.id == "can_trade" for k in calls[0].keywords))


if __name__ == "__main__":
    unittest.main()
