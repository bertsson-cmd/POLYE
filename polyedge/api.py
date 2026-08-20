"""Polymarket API client (read-only).

Two public endpoints, no API key needed:
  * Gamma  — event/market metadata:  GET {GAMMA_BASE}/events
  * CLOB   — order books:            GET {CLOB_BASE}/book?token_id=...

Everything is parsed defensively: Polymarket occasionally changes field
shapes, so any market we cannot parse is skipped and counted, never fatal.
"""
import contextlib
import json
import logging
import socket
import time
from typing import Optional

import requests

from . import config
from .models import BookLevel, Market, OrderBook

log = logging.getLogger("polyedge.api")

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


@contextlib.contextmanager
def _force_ipv4():
    """Temporarily restrict socket.getaddrinfo() to IPv4 (AF_INET) results
    only, for the duration of the `with` block -- used ONLY by
    check_geoblock(force_ipv4=True) below, to diagnostically compare
    default DNS resolution against IPv4-only resolution. Restored in a
    finally block, so a crash mid-request can never leave the process's
    DNS resolution permanently narrowed.

    Real incident this exists to diagnose: a VPS's outbound HTTPS to
    polymarket.com was resolving over IPv6 by default, and that specific
    IPv6 address geolocated to a Polymarket-blocked region even though
    the server's real (IPv4) location was fine -- confirmed directly via
    `curl` (default) vs `curl -4` returning different blocked/country
    values. See LIVE.md section 3 for the full writeup and the actual
    OS-level fix (this function only diagnoses the problem, it does not
    fix outbound trading traffic -- nothing in the rest of this module or
    live.py uses it)."""
    orig = socket.getaddrinfo

    def _ipv4_only(host, *args, **kwargs):
        return [r for r in orig(host, *args, **kwargs) if r[0] == socket.AF_INET]
    socket.getaddrinfo = _ipv4_only
    try:
        yield
    finally:
        socket.getaddrinfo = orig


def check_geoblock(session: Optional[requests.Session] = None,
                   force_ipv4: bool = False) -> Optional[dict]:
    """Hit Polymarket's own geoblock endpoint directly -- NOT the Gamma/CLOB
    bases used elsewhere in this module -- to detect whether outbound
    connections from this machine are being geolocated into a
    trading-restricted region. See LIVE.md section 3 for the real
    incident this exists to catch (an IPv6 routing/geolocation mismatch
    that silently rejected every live order with "Trading restricted in
    your region").

    Returns the parsed JSON response (expected shape: {"blocked": bool,
    "country": str, ...}), or None if the check itself couldn't complete
    (network failure) -- callers must treat None as "couldn't verify",
    never as "confirmed fine" or "confirmed blocked"."""
    http = session or requests.Session()
    try:
        if force_ipv4:
            with _force_ipv4():
                r = http.get(GEOBLOCK_URL, timeout=config.HTTP_TIMEOUT)
        else:
            r = http.get(GEOBLOCK_URL, timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError, OSError) as e:
        log.warning("geoblock check (force_ipv4=%s) failed: %s", force_ipv4, e)
        return None


class PolymarketClient:
    def __init__(self, session: Optional[requests.Session] = None):
        self.http = session or requests.Session()
        # pool sized to the concurrent worker count, otherwise urllib3
        # discards and reopens connections constantly under load
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=config.BOOK_FETCH_WORKERS,
            pool_maxsize=config.BOOK_FETCH_WORKERS)
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)
        self.http.headers.update({"User-Agent": "PolyEdge95/2.0 (paper-trading scanner)"})
        self.skipped_markets = 0

    # ------------------------------------------------------------ low level
    def _get(self, url: str, params: Optional[dict] = None):
        last_err = None
        for attempt in range(config.HTTP_RETRIES):
            try:
                r = self.http.get(url, params=params, timeout=config.HTTP_TIMEOUT)
                if r.status_code == 429:          # rate limited — back off
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as e:
                last_err = e
                time.sleep(1 + attempt)
        log.warning("GET %s failed after retries: %s", url, last_err)
        return None

    # ------------------------------------------------------------ events
    def fetch_events(self, limit: int = 100, max_events: Optional[int] = None) -> list:
        """Fetch open events (each event = list of markets). Paginates."""
        max_events = max_events or config.MAX_EVENTS_PER_SCAN
        events, offset = [], 0
        while len(events) < max_events:
            page = self._get(f"{config.GAMMA_BASE}/events", params={
                "closed": "false", "active": "true", "archived": "false",
                "limit": limit, "offset": offset, "order": "volume24hr",
                "ascending": "false",
            })
            if not page:
                break
            batch = page if isinstance(page, list) else page.get("data", [])
            if not batch:
                break
            events.extend(batch)
            offset += limit
            if len(batch) < limit:
                break
        return events[:max_events]

    # ------------------------------------------------------------ parsing
    @staticmethod
    def _tokens_of(m: dict):
        """Extract (yes_token, no_token) from a raw market dict."""
        raw = m.get("clobTokenIds") or m.get("clob_token_ids")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return None
        if isinstance(raw, list) and len(raw) == 2:
            return str(raw[0]), str(raw[1])
        return None

    @staticmethod
    def _yes_price_of(m: dict) -> Optional[float]:
        raw = m.get("outcomePrices") or m.get("outcome_prices")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None
        if isinstance(raw, list) and raw:
            try:
                return float(raw[0])
            except (TypeError, ValueError):
                pass
        for k in ("lastTradePrice", "bestAsk"):
            if m.get(k) is not None:
                try:
                    return float(m[k])
                except (TypeError, ValueError):
                    continue
        return None

    def parse_event(self, ev: dict) -> list:
        """Turn a raw Gamma event into a list[Market]. Unparseable markets skipped."""
        out = []
        ev_id = str(ev.get("id", ""))
        ev_title = ev.get("title", "") or ev.get("slug", "")
        neg_risk = bool(ev.get("negRisk") or ev.get("neg_risk"))
        # collect event tags/category into one lowercase string, defensively
        tag_bits = []
        for t in ev.get("tags", []) or []:
            if isinstance(t, dict):
                tag_bits.append(str(t.get("label", "") or t.get("slug", "")))
            elif isinstance(t, str):
                tag_bits.append(t)
        if ev.get("category"):
            tag_bits.append(str(ev["category"]))
        category = " ".join(tag_bits).lower()
        raw_markets = ev.get("markets", []) or []
        event_total_markets = len(raw_markets)
        for m in raw_markets:
            try:
                if m.get("closed") or not m.get("active", True):
                    continue
                tokens = self._tokens_of(m)
                price = self._yes_price_of(m)
                if tokens is None or price is None:
                    self.skipped_markets += 1
                    continue
                out.append(Market(
                    market_id=str(m.get("id", m.get("conditionId", ""))),
                    question=m.get("question", "") or m.get("groupItemTitle", ""),
                    yes_token=tokens[0], no_token=tokens[1],
                    yes_price=price,
                    liquidity=float(m.get("liquidityNum", m.get("liquidity", 0)) or 0),
                    end_date=m.get("endDate", "") or m.get("end_date_iso", "") or "",
                    event_id=ev_id, event_title=ev_title, neg_risk=neg_risk,
                    category=category, event_total_markets=event_total_markets,
                ))
            except (TypeError, ValueError, KeyError):
                self.skipped_markets += 1
        return out

    # ------------------------------------------------------------ resolution
    @staticmethod
    def parse_resolution(raw: dict) -> Optional[str]:
        """Return the winning side ('YES'/'NO') for a raw Gamma market dict
        if it has genuinely resolved, else None. Shared by the active-events
        pass and the direct by-id lookup below, so both agree on what
        counts as 'resolved'.
        """
        if not raw.get("closed"):
            return None
        if raw.get("umaResolutionStatus") not in ("resolved", "settled"):
            return None
        prices = raw.get("outcomePrices")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except ValueError:
                return None
        if isinstance(prices, list) and len(prices) == 2:
            try:
                return "YES" if float(prices[0]) > 0.5 else "NO"
            except (TypeError, ValueError):
                return None
        return None

    def fetch_market(self, market_id: str) -> Optional[dict]:
        """Fetch a single market by id directly.

        Unlike fetch_events(), this works even after a market has closed
        and dropped out of the `active:true,closed:false` feed — which
        every market eventually does the moment it resolves. This is how
        we learn a held position actually settled, instead of it sitting
        'open' forever waiting for a feed that will never show it again.
        """
        data = self._get(f"{config.GAMMA_BASE}/markets/{market_id}")
        if not data:
            return None
        if isinstance(data, list):
            data = data[0] if data else None
        return data

    def fetch_resolutions(self, market_ids) -> dict:
        """market_id -> 'YES'/'NO' for every id in market_ids that has
        actually resolved. Unresolved or unfetchable ids are simply
        absent from the result (never guessed at)."""
        out = {}
        for mid in market_ids:
            raw = self.fetch_market(mid)
            if raw:
                r = self.parse_resolution(raw)
                if r:
                    out[str(mid)] = r
        return out
    def fetch_book(self, token_id: str) -> Optional[OrderBook]:
        data = self._get(f"{config.CLOB_BASE}/book", params={"token_id": token_id})
        if not data:
            return None
        try:
            asks = sorted(
                (BookLevel(float(x["price"]), float(x["size"])) for x in data.get("asks", [])),
                key=lambda l: l.price)
            bids = sorted(
                (BookLevel(float(x["price"]), float(x["size"])) for x in data.get("bids", [])),
                key=lambda l: -l.price)
            # "min_order_size" is the raw JSON key exactly as returned --
            # confirmed against Polymarket's own /book response schema, no
            # camelCase alias. In SHARES, not dollars (see risk.py, where
            # it's actually used) -- missing/unparseable is left as None,
            # not defaulted to 0, since 0 would silently disable the check
            # this field exists to support.
            raw_min = data.get("min_order_size")
            min_order_size = float(raw_min) if raw_min is not None else None
            return OrderBook(token_id=token_id, asks=asks, bids=bids,
                             min_order_size=min_order_size)
        except (TypeError, ValueError, KeyError):
            return None

    def fetch_books(self, token_ids) -> dict:
        """Fetch many order books concurrently. Logs progress; never blocks
        for more than HTTP_TIMEOUT * HTTP_RETRIES per token because each
        worker has its own connection.
        """
        import concurrent.futures as cf

        token_ids = list(dict.fromkeys(token_ids))  # dedupe, keep order
        books: dict = {}
        if not token_ids:
            return books
        n = len(token_ids)
        done = 0
        log.info("fetching %d order books (up to %d workers)...",
                 n, config.BOOK_FETCH_WORKERS)
        with cf.ThreadPoolExecutor(max_workers=config.BOOK_FETCH_WORKERS) as ex:
            futures = {ex.submit(self.fetch_book, tid): tid for tid in token_ids}
            for fut in cf.as_completed(futures):
                tid = futures[fut]
                try:
                    b = fut.result()
                except Exception:  # noqa: BLE001 - a single token must never kill the scan
                    b = None
                if b:
                    books[tid] = b
                done += 1
                if done % 100 == 0 or done == n:
                    log.info("  ...%d/%d books fetched (%d ok)", done, n, len(books))
        return books
