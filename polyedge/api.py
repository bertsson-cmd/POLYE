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
                ))
            except (TypeError, ValueError, KeyError):
                self.skipped_markets += 1
        return out

    # ------------------------------------------------------------ books
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
