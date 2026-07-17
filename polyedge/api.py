"""Polymarket API client (read-only).

Two public endpoints, no API key needed:
  * Gamma  — event/market metadata:  GET {GAMMA_BASE}/events
  * CLOB   — order books:            GET {CLOB_BASE}/book?token_id=...

Everything is parsed defensively: Polymarket occasionally changes field
shapes, so any market we cannot parse is skipped and counted, never fatal.
"""
import json
import logging
import time
from typing import Optional

import requests

from . import config
from .models import BookLevel, Market, OrderBook

log = logging.getLogger("polyedge.api")


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
        for m in ev.get("markets", []) or []:
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
                    category=category,
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
            return OrderBook(token_id=token_id, asks=asks, bids=bids)
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
