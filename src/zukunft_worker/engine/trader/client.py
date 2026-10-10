"""Binance wrapper for spot + futures (testnet or live). One safe place that talks to Binance."""
import math
import time
import json
import os
import uuid
from decimal import Decimal

import requests
from binance.client import Client

from . import settings


class PositionUnknown(RuntimeError):
    """The exchange has not provided an authoritative position snapshot."""


class BinanceTrader:
    def _pending_entries(self, update=None):
        path = "pending_entries_" + ("testnet" if self.testnet else "live") + ".json"
        if update is None:
            if not os.path.exists(path):
                return {}
            with open(path) as handle:
                value = json.load(handle)
            if not isinstance(value, dict):
                raise PositionUnknown("Malformed pending-entry journal")
            return value
        with open(path + ".tmp", "w") as handle:
            json.dump(update, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(path + ".tmp", path)

    def _settled_order(self, symbol, client_id):
        for _ in range(12):
            try:
                order = self.fut.futures_get_order(symbol=symbol, origClientOrderId=client_id)
                if order.get("status") in {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}:
                    return order
            except Exception:
                pass
            time.sleep(0.5)
        raise PositionUnknown(f"{symbol}: order {client_id} has no verified terminal status")

    def __init__(self, testnet=None):
        # testnet=None -> follow the ACTIVE account (dashboard toggle / .env default). Pass True/False to
        # force a specific account (the bot does this on a live switch). Keys are picked to match.
        self.testnet = settings.active_testnet() if testnet is None else bool(testnet)
        self.dry_run = settings.DRY_RUN
        sk, ss, fk, fs = settings.keys_for(self.testnet)
        if not sk and not fk:
            raise RuntimeError("No API keys found for the "
                               + ("DEMO/testnet" if self.testnet else "LIVE")
                               + " account. Fill in the matching keys in your .env file.")
        # Separate clients so spot & futures keys can differ (split testnets).
        self.spot = Client(sk, ss, testnet=self.testnet, requests_params={"timeout": 10})
        self.fut = Client(fk, fs, testnet=self.testnet, requests_params={"timeout": 10})
        self._sync_time()          # align to Binance clock -> avoids APIError(-1021) on drift

    def _sync_time(self):
        """Align each client's timestamp to Binance server time, so signed requests don't fail with
        APIError(-1021) 'Timestamp for this request is outside of the recvWindow' when the PC clock
        drifts. Uses the UNSIGNED server-time endpoints, so it works even while the clock is wrong."""
        for client, getter in ((self.spot, "get_server_time"), (self.fut, "futures_time")):
            try:
                server_ms = getattr(client, getter)()["serverTime"]
                client.timestamp_offset = server_ms - int(time.time() * 1000)
            except Exception:
                pass

    # ---------- READ ----------
    def get_price(self, symbol):
        return float(self.spot.get_symbol_ticker(symbol=symbol.upper())["price"])

    def spot_balances(self):
        acct = self.spot.get_account()
        return {b["asset"]: float(b["free"]) for b in acct["balances"] if float(b["free"]) > 0}

    def futures_balance(self):
        return {b["asset"]: float(b["balance"])
                for b in self.fut.futures_account_balance() if float(b["balance"]) > 0}

    def futures_positions(self):
        rows = self.fut.futures_position_information()
        if not isinstance(rows, list):
            raise PositionUnknown("Malformed account positions")
        positions = []
        for row in rows:
            amount = float(row["positionAmt"])
            if not math.isfinite(amount) or row.get("positionSide", "BOTH") != "BOTH":
                raise PositionUnknown("Invalid or hedge-mode position")
            if amount:
                entry = float(row["entryPrice"])
                if not math.isfinite(entry) or entry <= 0:
                    raise PositionUnknown("Invalid position entry")
                positions.append({"symbol": row["symbol"], "amount": amount,
                                  "entry": entry, "pnl": float(row["unRealizedProfit"])})
        return positions

    def tradable_symbols(self):
        """Symbols that exist and are TRADING on the exchange THIS client is connected to (testnet now,
        live later). Cached 1h. Used to skip candidates the current exchange can't trade — fixes the
        -1121 'Invalid symbol' errors on testnet, and auto-expands to the full catalogue on live (no
        code change: it just re-asks whichever exchange the client is pointed at)."""
        now = time.time()
        cache = getattr(self, "_tradable_cache", None)
        if cache and now - getattr(self, "_tradable_ts", 0) < 3600:
            return cache
        try:
            syms = {s["symbol"] for s in self.fut.futures_exchange_info()["symbols"]
                    if s.get("status") == "TRADING"}
        except Exception:
            return cache or set()                       # on error keep last-known; never block everything
        if syms:
            self._tradable_cache, self._tradable_ts = syms, now
        return syms or cache or set()

    # ---------- TRADE ----------
    def spot_market_order(self, symbol, side, usdt_amount):
        if self.dry_run:
            return {"dry_run": True, "would": f"SPOT {side} {usdt_amount} USDT of {symbol}"}
        return self.spot.create_order(symbol=symbol.upper(), side=side.upper(),
                                      type="MARKET", quoteOrderQty=usdt_amount)

    def _mkt_lot(self, symbol):
        """(stepSize, minQty, maxQty) as strings for MARKET orders on this symbol. MARKET orders must
        obey MARKET_LOT_SIZE (coarser than LOT_SIZE). Using quantityPrecision instead was the bug: it let
        through fractional / oversized quantities that Binance rejects (-4023 step, -4005 max)."""
        for s in self.fut.futures_exchange_info()["symbols"]:
            if s["symbol"] == symbol.upper():
                f = {ff["filterType"]: ff for ff in s["filters"]}
                lot = f.get("MARKET_LOT_SIZE") or f.get("LOT_SIZE") or {}
                return (lot.get("stepSize", "0.001"), lot.get("minQty", "0"), lot.get("maxQty", "9e18"))
        return ("0.001", "0", "9e18")

    def _fut_qty(self, symbol, usdt_amount, leverage):
        """Coins for a (margin x leverage) notional, made VALID for the exchange: capped at the symbol's
        max quantity and floored to its step size, so the order can't be rejected for size."""
        price = float(self.fut.futures_symbol_ticker(symbol=symbol.upper())["price"])
        step_s, min_s, max_s = self._mkt_lot(symbol)
        step, mn, mx = float(step_s), float(min_s), float(max_s)
        raw = min((usdt_amount * leverage) / price, mx)                 # never exceed the symbol max (-4005)
        qty = math.floor(raw / step + 1e-9) * step if step > 0 else raw  # align DOWN to the step size (-4023)
        decimals = max(0, -Decimal(step_s).normalize().as_tuple().exponent)
        qty = round(qty, decimals)
        return qty if qty >= mn else 0.0                               # below the symbol minimum -> skip

    def _set_isolated(self, symbol):
        """Force ISOLATED margin before opening, so a bad trade can only lose its OWN margin —
        never the whole wallet, as cross margin could. Harmless if already isolated (Binance -4046)."""
        try:
            self.fut.futures_change_margin_type(symbol=symbol.upper(), marginType="ISOLATED")
        except Exception as exc:
            if getattr(exc, "code", None) != -4046:
                raise

    def futures_market_order(self, symbol, side, usdt_amount, leverage=1, split=True):
        """Open a MARKET position for a (usdt_amount margin x leverage) notional.

        Binance caps a SINGLE market order at the symbol's MARKET_LOT_SIZE maxQty. If the intended
        quantity exceeds that, the old code silently shrank the order (so a cheap coin got far less
        than the 10%-of-capital rule intended, while the bot still recorded the full size). With
        split=True we place the intended quantity in MULTIPLE market orders (each <= maxQty) so the
        FULL size is deployed. Returns a summary dict with the ACTUAL filled qty / notional / margin,
        so the caller records the REAL margin used (not the intended amount). split=False restores the
        old single-capped-order behaviour."""
        sym = symbol.upper()
        if self.dry_run:
            return {"dry_run": True,
                    "would": f"FUTURES {side} {usdt_amount} USDT of {sym} at {leverage}x"}
        pending = self._pending_entries()
        if sym in pending:
            for client_id in pending[sym]:
                self._settled_order(sym, client_id)
            pending.pop(sym)
            self._pending_entries(pending)
        base_amt, _ = self._position_amt_entry(sym)
        if base_amt != 0:
            raise ValueError(f"{sym}: an exchange position already exists; refusing another entry")
        self._set_isolated(sym)                          # isolated margin: one trade can't drain the wallet
        self.fut.futures_change_leverage(symbol=sym, leverage=leverage)
        price = float(self.fut.futures_symbol_ticker(symbol=sym)["price"])
        step_s, min_s, max_s = self._mkt_lot(sym)
        step, mn, mx = float(step_s), float(min_s), float(max_s)
        decimals = max(0, -Decimal(step_s).normalize().as_tuple().exponent)

        def _floor(q):
            return round(math.floor(q / step + 1e-9) * step, decimals) if step > 0 else round(q, decimals)

        total_qty = _floor((usdt_amount * leverage) / price)
        if total_qty < mn:
            raise ValueError(f"{sym}: computed quantity below the exchange minimum — skipped")
        per_order = _floor(mx)
        # build the chunk list (one order if it already fits, or split into <=maxQty pieces)
        if not split:
            chunks, remaining = [min(total_qty, per_order)], 0.0   # OLD behaviour: single order, capped at maxQty
        elif total_qty <= per_order:
            chunks, remaining = [total_qty], 0.0
        else:
            chunks, remaining = [], total_qty
            while remaining >= mn and len(chunks) < 50:   # 50-chunk backstop (never silently drop size)
                q = _floor(min(remaining, per_order))
                if q < mn:
                    break
                chunks.append(q)
                remaining = round(remaining - q, decimals)

        base_amt, _ = self._position_amt_entry(sym)       # baseline (one-per-coin guard -> normally 0)
        if base_amt != 0:
            raise ValueError(f"{sym}: an exchange position already exists; refusing another entry")
        placed, last = 0, None
        confirmed_qty = 0.0
        for i, q in enumerate(chunks, 1):
            client_id = "entry-" + uuid.uuid4().hex[:28]
            pending.setdefault(sym, []).append(client_id)
            self._pending_entries(pending)
            try:
                last = self.fut.futures_create_order(symbol=sym, side=side.upper(), type="MARKET", quantity=q,
                                                     newClientOrderId=client_id)
                placed += 1
            except Exception as e:
                # NEVER trust the order response/exception for the fill: a -1007 'execution status
                # unknown' (or an async ack) can fill server-side anyway. The POSITION read below decides.
                print(f"{sym}: order chunk {i}/{len(chunks)} raised ({e}) — verifying via position", flush=True)
            last = self._settled_order(sym, client_id)
            executed = float(last.get("executedQty", 0))
            if not math.isfinite(executed) or executed < 0 or executed > q + step / 2:
                raise PositionUnknown(f"{sym}: invalid executed quantity")
            confirmed_qty += executed
            if executed < q - step / 2:
                break
        if remaining >= mn:                               # hit the 50-chunk backstop: report, never hide it
            print(f"{sym}: WARNING order capped at 50 chunks — {remaining:g} qty not placed", flush=True)
        # SOURCE OF TRUTH = the resulting POSITION. Binance/testnet market orders report executedQty
        # async (the create response often shows '0'), so we READ the position — this is the fix for the
        # old 'no quantity filled' false-negative that opened untracked (orphaned) positions.
        amt, entry = base_amt, price
        previous, stable = None, 0
        for _ in range(12):                               # poll up to ~6s for the fill to settle
            time.sleep(0.5)
            a, e = self._position_amt_entry(sym)
            if a and (a > 0) != (side.upper() == "BUY"):
                raise PositionUnknown(f"{sym}: unexpected position direction after entry")
            if abs(a - base_amt) > 0:
                amt, entry = a, (e or price)
                stable = stable + 1 if a == previous else 0
                if stable >= 2 and abs(abs(a - base_amt) - confirmed_qty) <= step / 2:
                    break
            previous = a
        filled_qty = abs(amt - base_amt)
        if abs(filled_qty - confirmed_qty) > step / 2:
            raise PositionUnknown(f"{sym}: position does not match confirmed fills")
        pending.pop(sym, None)
        self._pending_entries(pending)
        if filled_qty <= 0:                               # genuinely nothing filled -> real rejection
            raise ValueError(f"{sym}: no fill detected on the exchange after placing — treated as rejected")
        notional = filled_qty * entry
        return {"filled_qty": filled_qty, "notional": notional, "avg_price": entry,
                "margin": (notional / leverage if leverage else notional),
                "chunks": placed, "intended_qty": total_qty, "order": last}

    def close_futures(self, symbol):
        amt, _ = self._position_amt_entry(symbol)
        if amt == 0:
            return {"info": f"No open futures position for {symbol}."}
        if self.dry_run:
            return {"dry_run": True, "would": f"CLOSE {symbol} ({amt})"}
        side = "SELL" if amt > 0 else "BUY"
        return {"closed_qty": self._reduce_only(symbol, side, abs(amt))}   # chunked <=maxQty

    def partial_close(self, symbol, fraction):
        """Market-close a FRACTION (0<f<=1) of the open futures position (reduceOnly), split into
        <=maxQty chunks. Used by gainer/special to bank 50% while letting the rest run."""
        fraction = float(fraction)
        if not 0 < fraction <= 1:
            raise ValueError("Partial fraction must be in (0, 1]")
        amt, _ = self._position_amt_entry(symbol)
        if amt == 0:
            return {"info": f"No open futures position for {symbol}."}
        if self.dry_run:
            return {"dry_run": True, "would": f"CLOSE {fraction*100:.0f}% of {symbol} ({amt})"}
        side = "SELL" if amt > 0 else "BUY"
        step_s, min_s, _ = self._mkt_lot(symbol)
        step = Decimal(step_s)
        qty = float((Decimal(str(abs(amt) * fraction)) // step) * step)
        if qty <= 0:
            return {"info": f"{symbol}: fraction too small to close a valid quantity."}
        if qty < float(min_s):
            return {"partial_closed_qty": 0.0, "remaining_qty": abs(amt)}
        error = None
        try:
            self._reduce_only(symbol, side, qty)
        except Exception as exc:
            error = exc
        time.sleep(0.5)
        remaining, _ = self._position_amt_entry(symbol)
        if remaining and (remaining > 0) != (amt > 0):
            raise PositionUnknown(f"{symbol}: direction changed during partial close")
        filled = abs(amt) - abs(remaining)
        if filled < 0 or filled > qty + float(step):
            raise PositionUnknown(f"{symbol}: unexpected quantity after partial close")
        orders = getattr(self, "_last_reductions", {}).get(symbol.upper(), [])
        confirmed = sum(float(x.get("executedQty", 0)) for x in orders)
        quote = sum(float(x.get("cumQuote", 0) or 0) for x in orders)
        if filled and (abs(confirmed - filled) > float(step) / 2 or quote <= 0):
            raise PositionUnknown(f"{symbol}: partial quantity changed but fill details are not verified")
        if error and not filled:
            raise error
        return {"partial_closed_qty": filled, "remaining_qty": abs(remaining),
                "fraction_filled": filled / abs(amt), "intended_qty": qty,
                "avg_price": quote / confirmed if confirmed else None}

    def _qty_precision(self, symbol):
        for s in self.fut.futures_exchange_info()["symbols"]:
            if s["symbol"] == symbol.upper():
                return s["quantityPrecision"]
        return 3

    def _percent_price_bounds(self, symbol):
        """(multiplierUp, multiplierDown) from the PERCENT_PRICE filter — the most aggressive
        LIMIT price the exchange will accept (used to force-close thin coins that reject markets)."""
        for s in self.fut.futures_exchange_info()["symbols"]:
            if s["symbol"] == symbol.upper():
                for f in s.get("filters", []):
                    if f.get("filterType") == "PERCENT_PRICE":
                        return float(f.get("multiplierUp", 1.05)), float(f.get("multiplierDown", 0.95))
        return 1.05, 0.95

    def is_flat(self, symbol):
        """True if there is NO open position for this symbol on the exchange (source of truth)."""
        try:
            return self._position_amt_entry(symbol)[0] == 0
        except Exception:
            return False

    def _position_amt_entry(self, symbol):
        """(positionAmt, entryPrice) straight from the exchange — the SOURCE OF TRUTH for fills.
        Returns (0.0, 0.0) only for a verified flat position. Market fills report async and a -1007 timeout can
        fill server-side, so this — not the create-order response — is what decides whether we filled."""
        try:
            # V3 omits flat symbols. V2 gives an explicit zero row, avoiding ambiguous absence.
            rows = self.fut._request_futures_api("get", "positionRisk", True, 2,
                                                data={"symbol": symbol.upper()})
            if not isinstance(rows, list) or not rows:
                raise ValueError("Empty or malformed position response")
            for p in rows:
                if p.get("symbol") != symbol.upper():
                    raise ValueError("Position response is for a different symbol")
                if p.get("positionSide", "BOTH") != "BOTH":
                    raise ValueError("Hedge-mode positions are not supported")
                if "positionAmt" not in p:
                    raise ValueError("Position quantity is missing")
                amt = float(p["positionAmt"])
                if not math.isfinite(amt):
                    raise ValueError("Non-finite position quantity")
                if amt != 0:
                    entry = float(p.get("entryPrice") or 0)
                    if not math.isfinite(entry) or entry <= 0:
                        raise ValueError("Invalid entry price")
                    return amt, entry
        except Exception as exc:
            raise PositionUnknown(f"{symbol}: position could not be verified") from exc
        return 0.0, 0.0

    def _reduce_only(self, symbol, side, qty, price=None):
        """Submit reduceOnly close order(s) for `qty`, SPLIT into <=maxQty chunks (Binance caps a
        single order at MARKET_LOT_SIZE maxQty — the same cap that limits opens). MARKET when price is
        None, else LIMIT IOC. Raises on the first rejection (caller decides the fallback); returns the
        quantity confirmed executed."""
        sym = symbol.upper()
        step_s, min_s, max_s = self._mkt_lot(sym)
        step, mn, mx = float(step_s), float(min_s), float(max_s)
        dec = max(0, -Decimal(step_s).normalize().as_tuple().exponent)

        def _floor(q):
            return round(math.floor(q / step + 1e-9) * step, dec) if step > 0 else round(q, dec)

        per, remaining, submitted, guard = _floor(mx), _floor(qty), 0.0, 0
        responses = []
        self._last_reductions = getattr(self, "_last_reductions", {})
        self._last_reductions[sym] = responses
        while remaining >= mn and guard < 80:
            guard += 1
            live, _ = self._position_amt_entry(sym)
            if not live:
                break
            if (live > 0) != (side == "SELL"):
                raise PositionUnknown(f"{sym}: close direction no longer matches the position")
            q = _floor(min(remaining, per, abs(live)))
            if q < mn:
                break
            if price is None:
                response = self.fut.futures_create_order(symbol=sym, side=side, type="MARKET",
                                                        quantity=q, reduceOnly=True)
                responses.append(response)
            else:
                response = self.fut.futures_create_order(symbol=sym, side=side, type="LIMIT", timeInForce="IOC",
                                                        quantity=q, price=price, reduceOnly=True)
                responses.append(response)
            for _ in range(10):
                actual = self.fut.futures_get_order(symbol=sym, orderId=response["orderId"])
                if actual.get("status") in ("FILLED", "CANCELED", "EXPIRED", "REJECTED"):
                    break
                time.sleep(0.2)
            else:
                raise PositionUnknown(f"{sym}: reduction execution is still pending")
            executed = float(actual.get("executedQty", 0))
            responses[-1] = actual
            if executed <= 0:
                break
            remaining = round(remaining - executed, dec); submitted += executed
            if executed < q:
                break
        return submitted

    def force_close(self, symbol):
        """Close the WHOLE position and return True only if it's actually flat afterwards.
        Market reduceOnly first; if the exchange rejects it (thin-coin PERCENT_PRICE / -4131),
        retry with a reduce-only LIMIT at the aggressive edge of the allowed price band."""
        sym = symbol.upper()
        self.last_close_error = None
        amt, _ = self._position_amt_entry(sym)
        if amt == 0:
            return True                                    # already flat
        if self.dry_run:
            return True
        side = "SELL" if amt > 0 else "BUY"
        try:
            self._reduce_only(sym, side, abs(amt))         # MARKET reduceOnly, chunked <=maxQty
        except PositionUnknown as exc:
            self.last_close_error = f"Close status unknown: {exc}"
            return False
        except Exception as exc:
            self.last_close_error = f"Reduce-only market rejected: {exc}"
            try:                                           # aggressive reduceOnly LIMIT for whatever remains
                a2, _ = self._position_amt_entry(sym)
                if a2 != 0:
                    s2 = "SELL" if a2 > 0 else "BUY"
                    mark = float(self.fut.futures_mark_price(symbol=sym)["markPrice"])
                    up, dn = self._percent_price_bounds(sym)
                    # Round into the permitted band using tickSize, not display precision.
                    px = self._stop_price(sym, mark * (up if s2 == "BUY" else dn),
                                          1 if s2 == "SELL" else -1)
                    self._reduce_only(sym, s2, abs(a2), price=px)
            except Exception as exc:
                self.last_close_error += f"; reduce-only limit rejected: {exc}"
        time.sleep(1)
        flat = self.is_flat(sym)
        if flat:
            self.last_close_error = None
        elif self.last_close_error is None:
            self.last_close_error = "Exchange has not confirmed a flat position after the close"
        return flat

    def _plain_market_close(self, symbol, side, qty):
        raise PositionUnknown("Non-reduce-only recovery is disabled: it can reverse a position")

    def reconcile_flatten(self, symbol):
        """Retain protection and state until a reduce-only close is confirmed."""
        try:
            flat = self.force_close(symbol)
            return flat, "verified flat" if flat else "not verified flat; will retry"
        except Exception as exc:
            return False, str(exc)

    # ---------- exchange-sourced history / P&L (the bot never computes these itself) ----------
    def income_history(self, days=30):
        """ALL futures income events straight from the EXCHANGE (realized pnl + commission +
        funding) — the real account changes. Returns [{type, symbol, amount, time}] oldest first."""
        cur = int((time.time() - days * 86400) * 1000)
        out = []
        try:
            for _ in range(15):                        # paginate (endpoint caps at 1000/call)
                batch = self.fut.futures_income_history(startTime=cur, limit=1000)
                if not batch:
                    break
                for x in batch:
                    out.append({"type": x.get("incomeType", ""), "symbol": x.get("symbol", ""),
                                "amount": float(x.get("income", 0) or 0), "time": int(x.get("time", 0)),
                                "tranId": int(x.get("tranId", 0) or 0)})   # unique id -> lets us drop glitch entries
                if len(batch) < 1000:
                    break
                cur = int(batch[-1]["time"]) + 1
        except Exception:
            pass
        return out

    def realized_pnl(self, symbol, start_ms):
        """The ACTUAL realized profit/loss (USDT) for ONE symbol since start_ms, straight from the
        exchange (sum of REALIZED_PNL income events). This is the true $ result of the trade — the
        bot never computes it itself. Returns None if the exchange can't be reached."""
        try:
            tot = 0.0
            batch = self.fut.futures_income_history(symbol=symbol.upper(), incomeType="REALIZED_PNL",
                                                    startTime=int(start_ms), limit=1000)
            for x in batch:
                tot += float(x.get("income", 0) or 0)
            return round(tot, 4)
        except Exception:
            return None

    def oi_rising(self, symbol, pct=2.0, lookback_min=45):
        """Coinalyze first, direct Binance fallback; None means neither is usable."""
        from .coinalyze import oi_change
        change, _ = oi_change(symbol, float(lookback_min) / 60, self)
        return None if change is None else change >= pct

    # ---------- helpers for the auto-bot ----------
    def oi_change_pct(self, symbol, hours=4):
        """Direct Binance fallback using exact, fresh five-minute OI timestamps."""
        from .coinalyze import _pct_change
        minutes = round(float(hours) * 60)
        if minutes <= 0 or minutes % 5 or minutes // 5 + 1 > 500:
            raise ValueError("OI lookback must fit 500 five-minute samples")
        end = int(time.time()) // 300 * 300 - 300
        for attempt in range(2):                                   # 2 tries: original + one retry
            try:
                response = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
                                        params={"symbol": symbol.upper(), "period": "5m",
                                                "startTime": (end - minutes * 60) * 1000,
                                                "endTime": end * 1000,
                                                "limit": minutes // 5 + 1}, timeout=10)
                response.raise_for_status()
                d = response.json()
                history = [{"t": int(x["timestamp"]) // 1000, "c": x["sumOpenInterest"]} for x in d]
                change = _pct_change(history, minutes, end)
                if change is None:
                    raise ValueError("incomplete or stale OI history")
                return change
            except Exception:
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                return None                                        # caller fails closed

    def futures_price(self, symbol):
        sym = symbol.upper()
        t = self.fut.futures_symbol_ticker(symbol=sym)
        if isinstance(t, list):                                  # some responses come as a list
            t = next((x for x in t if x.get("symbol") == sym), t[0] if t else {})
        if isinstance(t, dict) and "price" in t:
            return float(t["price"])
        m = self.fut.futures_mark_price(symbol=sym)              # fallback: mark price
        if isinstance(m, list):
            m = m[0] if m else {}
        return float(m["markPrice"])

    def futures_prices_all(self):
        """EVERY futures price in ONE request -> {symbol: price}.
        Used by the fast 1-second watcher: 10 open trades still cost a single call."""
        t = self.fut.futures_symbol_ticker()
        out = {}
        for x in (t if isinstance(t, list) else [t]):
            try:
                out[x["symbol"]] = float(x["price"])
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def _price_precision(self, symbol):
        for s in self.fut.futures_exchange_info()["symbols"]:
            if s["symbol"] == symbol.upper():
                return s["pricePrecision"]
        return 2

    def check_entry_liquidity(self, symbol, side, margin, leverage, max_spread_pct=0.5, max_slippage_pct=0.5):
        if side not in (-1, 1) or not all(math.isfinite(float(x)) and float(x) > 0
                for x in (margin, leverage, max_spread_pct, max_slippage_pct)):
            raise ValueError("Invalid entry liquidity parameters")
        book = self.fut.futures_order_book(symbol=symbol.upper(), limit=100)
        bid, ask = float(book["bids"][0][0]), float(book["asks"][0][0])
        if not 0 < bid <= ask or not math.isfinite(ask):
            raise ValueError("Invalid or crossed order book")
        if (ask / bid - 1) * 100 > max_spread_pct:
            raise ValueError("Entry spread exceeds safety limit")
        mark = float(self.fut.futures_mark_price(symbol=symbol.upper())["markPrice"])
        if not math.isfinite(mark) or mark <= 0:
            raise ValueError("Invalid mark price")
        up, down = self._percent_price_bounds(symbol)
        best = ask if side > 0 else bid
        remaining = float(margin) * leverage / best
        quote = quantity = 0.0
        levels = book["asks"] if side > 0 else book["bids"]
        for px, size in levels:
            px, size = float(px), float(size)
            if not math.isfinite(px) or not math.isfinite(size) or px <= 0 or size <= 0:
                raise ValueError("Invalid book depth")
            if (side > 0 and px > mark * up) or (side < 0 and px < mark * down):
                break
            take = min(size, remaining)
            quantity += take
            quote += take * px
            remaining -= take
            if remaining <= 1e-9:
                break
        if remaining > 1e-9 or quantity <= 0:
            raise ValueError("Insufficient executable depth inside Binance price band")
        if side * (quote / quantity / best - 1) * 100 > max_slippage_pct:
            raise ValueError("Estimated entry slippage exceeds safety limit")
        return True

    def stop_status(self, symbol, side, stop_price, opened_ms=0):
        """Inspect the exchange stop lifecycle, not just its placement acknowledgement."""
        sym = symbol.upper()
        close_side = "SELL" if side > 0 else "BUY"
        errors = []
        for method in ("futures_get_open_algo_orders", "futures_get_open_orders"):
            try:
                rows = getattr(self.fut, method)(symbol=sym)
                if isinstance(rows, dict):
                    rows = rows.get("orders", [rows])
                if not isinstance(rows, list):
                    raise ValueError("Malformed open-stop response")
                for order in rows:
                    trigger = float(order.get("triggerPrice", order.get("stopPrice", 0)))
                    # Legacy stops were rounded to display precision. Allow rounding noise only.
                    tolerance = max(abs(stop_price) * 0.0002, 1e-8)
                    if (order.get("symbol") == sym and order.get("side") == close_side
                            and order.get("orderType", order.get("type")) == "STOP_MARKET"
                            and order.get("algoStatus", order.get("status")) == "NEW"
                            and order.get("positionSide", "BOTH") == "BOTH"
                            and str(order.get("closePosition")).lower() == "true"
                            and trigger > 0 and side * (trigger - stop_price) >= -tolerance):
                        return {"status": "active", "id": order.get("algoId", order.get("orderId")),
                                "trigger": trigger}
            except Exception as exc:
                errors.append(str(exc))
        try:
            orders = self.fut.futures_get_all_algo_orders(symbol=sym, limit=100)
            relevant = [o for o in orders if o.get("side") == close_side
                        and o.get("orderType") == "STOP_MARKET"
                        and str(o.get("closePosition")).lower() == "true"
                        and int(o.get("createTime", 0)) >= opened_ms - 5000]
            latest = max(relevant, key=lambda o: int(o.get("createTime", 0)), default={})
            if latest.get("algoStatus") in {"REJECTED", "EXPIRED", "CANCELED", "FINISHED", "TRIGGERED"}:
                return {"status": "terminal", "reason": latest.get("rejectReason") or latest["algoStatus"],
                        "id": latest.get("algoId")}
        except Exception as exc:
            errors.append(str(exc))
        return {"status": "unknown" if errors else "missing", "reason": "; ".join(errors) or "No active stop"}

    def take_profit_filled(self, symbol, opened_ms, side):
        """Identify an executed native TP, not merely a triggered/rejected conditional."""
        close_side = "SELL" if side > 0 else "BUY"
        try:
            rows = self.fut.futures_get_all_algo_orders(symbol=symbol.upper(), limit=100)
            for order in rows:
                if (order.get("symbol") == symbol.upper() and order.get("side") == close_side
                        and order.get("orderType") == "TAKE_PROFIT_MARKET"
                        and str(order.get("closePosition")).lower() == "true"
                        and int(order.get("createTime", 0)) >= opened_ms - 5000
                        and order.get("algoStatus") == "FINISHED" and order.get("actualOrderId")):
                    child = self.fut.futures_get_order(symbol=symbol.upper(), orderId=order["actualOrderId"])
                    if child.get("status") == "FILLED":
                        return True
        except Exception:
            pass
        try:
            rows = self.fut.futures_get_all_orders(symbol=symbol.upper(), startTime=opened_ms, limit=100)
            return any(o.get("side") == close_side and o.get("type") == "TAKE_PROFIT_MARKET"
                       and str(o.get("closePosition")).lower() == "true" and o.get("status") == "FILLED"
                       for o in rows)
        except Exception:
            return False

    def exit_fill_price(self, symbol, opened_ms, side, quantity=None):
        """VWAP of the latest closing fills covering the remaining tracked quantity."""
        rows = self.fut.futures_account_trades(symbol=symbol.upper(), startTime=int(opened_ms), limit=1000)
        if not isinstance(rows, list) or len(rows) == 1000:
            raise PositionUnknown("Exit fill history unavailable or incomplete")
        rows = [x for x in rows if x.get("side") == ("SELL" if side > 0 else "BUY")
                and x.get("positionSide", "BOTH") == "BOTH" and int(x["time"]) >= opened_ms]
        rows.sort(key=lambda x: (int(x["time"]), int(x["id"])), reverse=True)
        if not rows:
            raise PositionUnknown("No verified closing executions yet")
        if quantity is None:
            rows = [x for x in rows if x["orderId"] == rows[0]["orderId"]]
        remaining = float(quantity) if quantity is not None else float("inf")
        quote = filled = 0.0
        for row in rows:
            qty, price = float(row["qty"]), float(row["price"])
            if not math.isfinite(qty) or not math.isfinite(price) or qty <= 0 or price <= 0:
                raise PositionUnknown("Invalid closing execution")
            take = min(remaining, qty)
            quote += take * price
            filled += take
            remaining -= take
            if remaining <= 1e-9:
                break
        if quantity is not None and remaining > max(1e-8, float(quantity) * 1e-7):
            raise PositionUnknown("Closing executions do not cover tracked quantity")
        return quote / filled

    def _stop_price(self, symbol, price, side):
        from decimal import ROUND_CEILING, ROUND_FLOOR
        info = next(x for x in self.fut.futures_exchange_info()["symbols"] if x["symbol"] == symbol.upper())
        tick = Decimal(next(x["tickSize"] for x in info["filters"] if x["filterType"] == "PRICE_FILTER"))
        value = Decimal(str(price))
        if not value.is_finite() or value <= 0 or tick <= 0:
            raise ValueError("Invalid stop price or tick")
        rounding = ROUND_CEILING if side > 0 else ROUND_FLOOR
        return float((value / tick).to_integral_value(rounding=rounding) * tick)

    def _has_stop(self, symbol, side, kind, price):
        close_side = "SELL" if side > 0 else "BUY"
        for method in ("futures_get_open_orders", "futures_get_open_algo_orders"):
            try:
                rows = getattr(self.fut, method)(symbol=symbol.upper())
                if isinstance(rows, dict):
                    rows = rows.get("orders", [rows])
                for order in rows:
                    if (order.get("symbol") != symbol.upper() or order.get("side") != close_side
                            or order.get("orderType", order.get("type")) != kind
                            or str(order.get("closePosition")).lower() != "true"
                            or order.get("positionSide", "BOTH") != "BOTH"
                            or order.get("algoStatus", order.get("status")) != "NEW"):
                        continue
                    trigger = float(order.get("triggerPrice", order.get("stopPrice", 0)))
                    if kind == "STOP_MARKET" and trigger > 0 and side * (trigger - price) >= -1e-10:
                        return True
                    if kind != "STOP_MARKET" and abs(trigger - price) <= 1e-10:
                        return True
            except Exception:
                continue
        return False

    def place_stops(self, symbol, position_side, tp_price, sl_price):
        """An acknowledgement is insufficient: verify the stop in exchange open orders."""
        if self.dry_run:
            return {"dry_run": True}
        out = {}
        for kind, price in [("STOP_MARKET", sl_price), ("TAKE_PROFIT_MARKET", tp_price)]:
            if price is None:
                continue
            try:
                price = self._stop_price(symbol, price, position_side)
                if not self._has_stop(symbol, position_side, kind, price):
                    close_side = "SELL" if position_side > 0 else "BUY"
                    try:
                        self.fut.futures_create_order(symbol=symbol.upper(), side=close_side, type=kind,
                                                      stopPrice=price, closePosition=True)
                    except Exception as exc:
                        if getattr(exc, "code", None) != -4120:
                            raise
                        self.fut.futures_create_algo_order(symbol=symbol.upper(), side=close_side,
                                                          algoType="CONDITIONAL", type=kind,
                                                          triggerPrice=price, closePosition="true")
                out[kind] = "placed" if self._has_stop(symbol, position_side, kind, price) else "unverified"
            except Exception as exc:
                out[kind] = f"failed: {exc}"
        return out

    def cancel_open_orders(self, symbol):
        for method in ("futures_cancel_all_open_orders", "futures_cancel_all_algo_open_orders"):
            try:
                getattr(self.fut, method)(symbol=symbol.upper())
            except Exception:
                pass
