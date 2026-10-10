"""Pure exit calculations shared by normal execution and backtesting."""


def normal_floor_pct(rules, leverage):
    return round(max(float(rules.get('stop_loss_pct', 20)) / max(1, leverage), 0.3), 2)


def normal_stop(pos, price, rules):
    side, entry = pos['side'], pos['entry']
    previous = pos.get('peak', entry)
    peak = max(previous, price) if side > 0 else min(previous, price)
    gain = (peak / entry - 1) * 100 * side
    stop, armed = pos['sl'], False
    if rules.get('normal_exit_mode', 'trail') == 'ladder':
        leverage = pos.get('leverage', 1) or 1
        for trigger, locked in rules.get('normal_stop_ladder', [[25, 0], [50, 25], [75, 50]]):
            if gain * leverage >= trigger - 1e-9:
                level = entry * (1 + side * locked / leverage / 100)
                stop = max(stop, level) if side > 0 else min(stop, level)
                armed = True
    elif gain >= rules.get('trail_activation_pct', 2):
        armed = True
        width = rules.get('trail_pct_tight', 1.2) if gain >= rules.get('trail_tighten_at_pct', 5) else rules.get('trail_pct_normal', 2)
        level = peak * (1 - side * width / 100)
        stop = max(stop, level, entry) if side > 0 else min(stop, level, entry)
    return stop, armed, peak


def normal_target(pos, rules):
    target = float(rules.get('normal_target_margin_pct', 0) or 0)
    if rules.get('normal_exit_mode') != 'ladder' or target <= 0:
        return None
    return pos['entry'] * (1 + pos['side'] * target / (pos.get('leverage', 1) or 1) / 100)
