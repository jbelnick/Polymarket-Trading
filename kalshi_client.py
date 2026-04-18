"""
Kalshi Trading Bot — API Client

Thin wrapper around the Kalshi v2 REST API with RSA-PSS authentication.
Uses pykalshi when available, falls back to direct HTTP.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import KALSHI_API_BASE, KALSHI_API_KEY_ID, KALSHI_BASE_URL, KALSHI_PRIVATE_KEY_PATH

logger = logging.getLogger(__name__)


def _load_private_key(path: str):
    """Load an RSA private key from a PEM file."""
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


class KalshiClient:
    """
    Authenticated Kalshi API client.

    Auth: RSA-PSS signature per request.
    Every request gets three headers:
      KALSHI-ACCESS-KEY        — your API key ID
      KALSHI-ACCESS-SIGNATURE  — RSA-PSS(timestamp + METHOD + path)
      KALSHI-ACCESS-TIMESTAMP  — milliseconds since epoch
    """

    def __init__(
        self,
        key_id: str = KALSHI_API_KEY_ID,
        private_key_path: str = KALSHI_PRIVATE_KEY_PATH,
    ):
        self.key_id = key_id
        self.private_key = _load_private_key(private_key_path) if private_key_path else None
        self.base_url = KALSHI_BASE_URL
        self.session = requests.Session()

    def _sign(self, message: str) -> str:
        """Sign a message with RSA-PSS SHA256."""
        signature = self.private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _headers(self, method: str, path: str) -> dict:
        """Build authenticated headers for a request."""
        ts = str(int(time.time() * 1000))
        # Strip query params for signing
        sign_path = path.split("?")[0]
        signature = self._sign(ts + method.upper() + sign_path)
        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = self.base_url + path
        headers = self._headers("GET", path) if self.private_key else {}
        resp = self.session.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        url = self.base_url + path
        headers = self._headers("POST", path)
        resp = self.session.post(url, headers=headers, json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> dict:
        url = self.base_url + path
        headers = self._headers("DELETE", path)
        resp = self.session.delete(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ── Exchange ───────────────────────────────────────────────────────────────

    def get_exchange_status(self) -> dict:
        return self._get("/trade-api/v2/exchange/status")

    # ── Markets ────────────────────────────────────────────────────────────────

    def get_markets(
        self,
        limit: int = 200,
        cursor: str | None = None,
        status: str = "open",
        series_ticker: str | None = None,
        event_ticker: str | None = None,
    ) -> dict:
        """List markets. Returns {"markets": [...], "cursor": "..."}."""
        params: dict = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        return self._get("/trade-api/v2/markets", params)

    def get_all_markets(self, status: str = "open", limit: int = 500) -> list[dict]:
        """Paginate through all markets up to limit."""
        all_markets: list[dict] = []
        cursor = None
        while len(all_markets) < limit:
            batch_size = min(200, limit - len(all_markets))
            data = self.get_markets(limit=batch_size, cursor=cursor, status=status)
            markets = data.get("markets", [])
            if not markets:
                break
            all_markets.extend(markets)
            cursor = data.get("cursor")
            if not cursor:
                break
        return all_markets

    def get_market(self, ticker: str) -> dict:
        """Get a single market by ticker."""
        data = self._get(f"/trade-api/v2/markets/{ticker}")
        return data.get("market", data)

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """Get the order book for a market. Returns {"orderbook": {"yes": [...], "no": [...]}}."""
        return self._get(f"/trade-api/v2/markets/{ticker}/orderbook", {"depth": depth})

    def get_trades(
        self,
        ticker: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict:
        params: dict = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        return self._get("/trade-api/v2/markets/trades", params)

    # ── Portfolio ──────────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        """Returns {"balance": <cents>}."""
        return self._get("/trade-api/v2/portfolio/balance")

    def get_balance_dollars(self) -> float:
        data = self.get_balance()
        return data.get("balance", 0) / 100

    def get_positions(self, **kwargs) -> dict:
        return self._get("/trade-api/v2/portfolio/positions", kwargs or None)

    def get_fills(self, limit: int = 100, **kwargs) -> dict:
        params = {"limit": limit, **kwargs}
        return self._get("/trade-api/v2/portfolio/fills", params)

    def get_orders(self, **kwargs) -> dict:
        return self._get("/trade-api/v2/portfolio/orders", kwargs or None)

    # ── Order management ───────────────────────────────────────────────────────

    def place_order(
        self,
        ticker: str,
        action: str,         # "buy" or "sell"
        side: str,           # "yes" or "no"
        count: int,          # number of contracts
        type: str = "limit", # "limit" or "market"
        yes_price: int | None = None,  # cents (1-99), required for limit
        no_price: int | None = None,
        client_order_id: str | None = None,
        expiration_ts: int | None = None,
    ) -> dict:
        """
        Place an order on Kalshi.

        Prices are in CENTS (1-99).
        count is the number of contracts.
        Each contract pays $1 if it resolves in your favor.
        """
        body: dict = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": type,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        if expiration_ts is not None:
            body["expiration_ts"] = expiration_ts

        logger.info(
            "ORDER: %s %s %s %d contracts @ %sc — %s",
            action.upper(),
            side.upper(),
            ticker,
            count,
            yes_price or no_price or "market",
            type,
        )
        return self._post("/trade-api/v2/portfolio/orders", body)

    def cancel_order(self, order_id: str) -> dict:
        return self._delete(f"/trade-api/v2/portfolio/orders/{order_id}")

    # ── Convenience ────────────────────────────────────────────────────────────

    def get_midpoint(self, ticker: str) -> float:
        """Get the midpoint price in dollars (0.01–0.99) for a market."""
        book = self.get_orderbook(ticker)
        ob = book.get("orderbook", book)

        yes_bids = ob.get("yes", [])
        no_bids = ob.get("no", [])

        best_yes_bid = yes_bids[0][0] if yes_bids else 50
        best_yes_ask = (100 - no_bids[0][0]) if no_bids else 50

        return ((best_yes_bid + best_yes_ask) / 2) / 100

    def get_book_depth_dollars(self, ticker: str) -> tuple[float, float]:
        """Return (bid_depth, ask_depth) in dollars."""
        book = self.get_orderbook(ticker, depth=50)
        ob = book.get("orderbook", book)

        yes_levels = ob.get("yes", [])
        no_levels = ob.get("no", [])

        # Each level is [price_cents, count]
        bid_depth = sum(p * c / 100 for p, c in yes_levels) if yes_levels else 0
        ask_depth = sum((100 - p) * c / 100 for p, c in no_levels) if no_levels else 0

        return bid_depth, ask_depth
