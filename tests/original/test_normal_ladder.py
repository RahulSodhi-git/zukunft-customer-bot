import unittest

from exit_rules import normal_stop, normal_target


class NormalLadderTests(unittest.TestCase):
    def setUp(self):
        self.rules = {
            "normal_exit_mode": "ladder",
            "normal_stop_ladder": [[25, 0], [50, 25], [75, 50], [90, 75]],
            "normal_target_margin_pct": 100,
        }

    def position(self, side, leverage):
        return {"entry": 100, "side": side, "leverage": leverage,
                "sl": 100 * (1 - side * .2 / leverage), "peak": 100}

    def test_below_ninety_keeps_fifty_floor(self):
        for side in (1, -1):
            for leverage in (3, 5):
                with self.subTest(side=side, leverage=leverage):
                    pos = self.position(side, leverage)
                    price = 100 * (1 + side * .899 / leverage)
                    stop, armed, _ = normal_stop(pos, price, self.rules)
                    self.assertTrue(armed)
                    self.assertAlmostEqual(stop, 100 * (1 + side * .5 / leverage))

    def test_above_ninety_raises_floor_to_seventy_five(self):
        for side in (1, -1):
            for leverage in (3, 5):
                with self.subTest(side=side, leverage=leverage):
                    pos = self.position(side, leverage)
                    price = 100 * (1 + side * .901 / leverage)
                    stop, armed, _ = normal_stop(pos, price, self.rules)
                    self.assertTrue(armed)
                    self.assertAlmostEqual(stop, 100 * (1 + side * .75 / leverage))

    def test_persisted_peak_keeps_new_floor_on_retracement(self):
        for side in (1, -1):
            pos = self.position(side, 3)
            pos["peak"] = 100 * (1 + side * .91 / 3)
            price = 100 * (1 + side * .70 / 3)
            stop, armed, peak = normal_stop(pos, price, self.rules)
            self.assertTrue(armed)
            self.assertEqual(peak, pos["peak"])
            self.assertAlmostEqual(stop, 100 * (1 + side * .75 / 3))
            self.assertLess((price - stop) * side, 0)

    def test_full_target_remains_one_hundred(self):
        for side in (1, -1):
            for leverage in (3, 5):
                target = normal_target(self.position(side, leverage), self.rules)
                self.assertAlmostEqual((target / 100 - 1) * side * leverage * 100, 100)


if __name__ == "__main__":
    unittest.main()
