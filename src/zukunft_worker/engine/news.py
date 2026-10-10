"""Free crypto-news scanner (RSS, no API key). Reads fresh headlines from major crypto
sources, detects a coin + bullish/bearish sentiment, and returns news trade signals.

Note: this covers major crypto news RSS feeds. Real-time X/Twitter (e.g. Elon Musk tweets)
needs a PAID X API — not included. The code is structured so an X source can be added later.
"""
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait
from email.utils import parsedate_to_datetime

import requests

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://cryptoslate.com/feed/",
    "https://decrypt.co/feed",
]

BULLISH = ["surge", "soar", "rally", "jump", "bullish", "partnership", "adopt", "approval",
           "approved", "integration", "launch", "upgrade", "all-time high", "record high",
           "breakout", "gains", "skyrocket", "spike", "boost", "listing", "lists", "bullrun"]
BEARISH = ["crash", "plunge", "dump", "hack", "hacked", "exploit", "ban", "banned", "lawsuit",
           "sues", "bearish", "sell-off", "selloff", "collapse", "tumble", "slump", "fraud",
           "delist", "rug", "liquidation", "outage", "halt"]

# coin -> keywords (full names are safe; short tickers only where distinctive)
COINS = {
    "BTCUSDT": ["bitcoin", "btc"], "ETHUSDT": ["ethereum", "ether", "eth"],
    "SOLUSDT": ["solana", "sol"], "BNBUSDT": ["binance coin", "bnb"],
    "XRPUSDT": ["ripple", "xrp"], "DOGEUSDT": ["dogecoin", "doge"],
    "ADAUSDT": ["cardano", "ada"], "AVAXUSDT": ["avalanche", "avax"],
    "LINKUSDT": ["chainlink"], "DOTUSDT": ["polkadot"], "TRXUSDT": ["tron"],
    "LTCUSDT": ["litecoin", "ltc"], "SHIBUSDT": ["shiba inu", "shiba", "shib"],
    "PEPEUSDT": ["pepe"], "WIFUSDT": ["dogwifhat"], "BONKUSDT": ["bonk"],
    "FLOKIUSDT": ["floki"], "ARBUSDT": ["arbitrum"], "OPUSDT": ["optimism"],
    "INJUSDT": ["injective"], "SUIUSDT": ["sui network"], "APTUSDT": ["aptos"],
    "NEARUSDT": ["near protocol"], "TIAUSDT": ["celestia"], "WLDUSDT": ["worldcoin"],
    "ORDIUSDT": ["ordinals"],
}


def _has(word, text):
    return re.search(r"\b" + re.escape(word) + r"\b", text) is not None


def _child(el, name):
    """Text of a child tag by local name (namespace-agnostic); falls back to href for Atom links."""
    for c in el:
        if c.tag.split("}")[-1] == name:
            return (c.text or c.get("href") or "").strip()
    return ""


def _items(url):
    try:
        r = requests.get(url, timeout=(4, 6), headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(r.content)
    except Exception:
        return []
    out = []
    for el in root.iter():
        if el.tag.split("}")[-1] in ("item", "entry"):      # RSS <item> or Atom <entry>
            out.append({
                "title": _child(el, "title"),
                "summary": _child(el, "summary") or _child(el, "description") or _child(el, "content"),
                "pub": _child(el, "pubDate") or _child(el, "published") or _child(el, "updated"),
                "link": _child(el, "link"),
            })
    return out


def _fresh(pub, max_age_min):
    try:
        return (time.time() - parsedate_to_datetime(pub).timestamp()) <= max_age_min * 60
    except Exception:
        return True   # no/unparseable date → treat as fresh (still deduped by id upstream)


def get_news_signals(max_age_min=30, min_sentiment=1, budget=12):
    """Fresh news trade signals: [{symbol, direction(+1/-1), headline, source, id}].
    Never blocks longer than `budget` seconds — slow feeds are just skipped this round."""
    signals = []
    ex = ThreadPoolExecutor(max_workers=len(FEEDS))         # fetch all feeds in parallel
    futs = {ex.submit(_items, u): u for u in FEEDS}
    done, _ = wait(list(futs), timeout=budget)              # hard wall-clock cap
    feeds = []
    for f in done:
        try:
            feeds.append((futs[f], f.result()))
        except Exception:
            pass
    ex.shutdown(wait=False, cancel_futures=True)
    for url, items in feeds:
        for it in items[:20]:
            if not _fresh(it["pub"], max_age_min):
                continue
            text = (it["title"] + " " + it["summary"]).lower()
            sym = next((s for s, kws in COINS.items() if any(_has(k, text) for k in kws)), None)
            if not sym:
                continue
            bull = sum(1 for w in BULLISH if w in text)
            bear = sum(1 for w in BEARISH if w in text)
            if bull >= min_sentiment and bull > bear:
                d = 1
            elif bear >= min_sentiment and bear > bull:
                d = -1
            else:
                continue
            signals.append({"symbol": sym, "direction": d, "headline": it["title"][:140],
                            "source": url.split("/")[2], "id": it["link"] or it["title"]})
    return signals
