"""
Binance Spot TESTNET weekly W4 momentum portfolio bot
=====================================================

SAFETY / ENVIRONMENT
--------------------
This file is deliberately hard-locked to Binance Spot Testnet for authenticated
account reads and order writes. It cannot submit authenticated orders to Binance
mainnet unless its safety guard is intentionally removed from the source.

Strategy (matches the research harness variant: Top7 / BTC_4W_POS_AND_MA8 / OFF)
-------------------------------------------------------------------------------
- Intended schedule: run once per week on Monday, shortly after the Sunday UTC
  daily candle is complete.
- Signal date = latest COMPLETED Sunday in UTC.
- Universe = CoinMarketCap historical Top 50 for that Sunday, stablecoins removed.
- Investable candidates must have a production Binance USDT Spot pair and enough
  Binance daily history for a 4-week return.
- Rank candidates by positive 4-week Binance close-to-close momentum:
      W4 = Sunday close / close 4 Sundays earlier - 1
- Hold up to the highest-ranked 7 positive-W4 coins.
- BTC regime must be ON:
      BTC 4-week return > 0
      AND
      BTC Sunday close > mean(BTC Sunday closes for current + prior 7 weeks)
- Volatility scaling is OFF.
- Regime ON: equal-weight selected coins.
- Regime OFF: target is 100% USDT.

Execution model
---------------
- Strategy signals use public production Binance market data.
- Account reads and writes use Binance Spot Testnet only.
- Testnet target selection walks down the production ranking until it has up to
  seven pairs that also exist on Spot Testnet; skipped names are printed.
- Existing target holdings are rebalanced, unlike the eToro bot's no-top-up rule.
- Non-target USDT-pair holdings in this dedicated TESTNET account can be cleaned up.
- Sells are submitted before buys.
- Market BUYs use quoteOrderQty (USDT amount).
- Market SELLs use filter-compliant base-asset quantities.
- Network/5xx uncertainty during an order write HALTS all further writes; no blind
  automatic retry is attempted.
- A deterministic clientOrderId plus Binance order history prevents duplicate
  same-week symbol/side submissions after a partial rerun.
- A small local JSON state file marks a fully completed weekly rebalance so repeated
  scheduled launches do not rebalance again later in the week because prices moved.

Testnet notes
-------------
Binance Spot Testnet periodically resets balances/orders and provides virtual assets.
MAX_MANAGED_EQUITY_USDT limits the strategy notional so those faucet balances do not
cause an enormous test portfolio. Set it to 0 only if you intentionally want to use
all testnet account value.

Dependencies:
    pip install requests pandas numpy beautifulsoup4
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode

DEPENDENCY_IMPORT_ERROR: Optional[Exception] = None
try:
    import numpy as np
    import pandas as pd
    import requests
    from bs4 import BeautifulSoup
except Exception as _dependency_error:
    DEPENDENCY_IMPORT_ERROR = _dependency_error
    np = None  # type: ignore[assignment]
    pd = None  # type: ignore[assignment]
    requests = None  # type: ignore[assignment]
    BeautifulSoup = None  # type: ignore[assignment]


# =============================================================================
# CONFIG
# =============================================================================

# -----------------------------------------------------------------------------
# PASTE YOUR BINANCE SPOT TESTNET HMAC CREDENTIALS HERE
# -----------------------------------------------------------------------------
BINANCE_API_KEY = ""
BINANCE_API_SECRET = ""

# Environment variable fallbacks if the paste-ready strings above are blank.
BINANCE_API_KEY_ENV = "BINANCE_API_KEY"
BINANCE_API_SECRET_ENV = "BINANCE_API_SECRET"

# Hard safety boundary: authenticated account/order traffic is TESTNET only.
TESTNET_BASE_URL = "https://testnet.binance.vision"
PUBLIC_MARKET_BASE_URL = "https://data-api.binance.vision"

EXECUTE_TESTNET_ORDERS = True
KEEP_WINDOW_OPEN = True

# Strategy parameters from the selected backtest row.
MARKET_CAP_TOP_N = 50
TARGET_POSITIONS = 7
SIGNAL_LOOKBACK_WEEKS = 4
REQUIRE_POSITIVE_MOMENTUM = True
REGIME_NAME = "BTC_4W_POS_AND_MA8"
BINANCE_QUOTE_ASSET = "USDT"

# Spot Testnet often starts with many virtual asset balances. This cap keeps the
# demonstration portfolio bounded. 0 means no cap.
MAX_MANAGED_EQUITY_USDT = 10_000.0
CASH_RESERVE_USDT = 10.0

# Avoid tiny churn. Orders below the larger of these thresholds are skipped.
MIN_REBALANCE_USDT = 10.0
MIN_REBALANCE_FRACTION_OF_EQUITY = 0.001  # 0.10%

# Dedicated TESTNET account behavior.
CLEAN_NON_TARGET_USDT_ASSETS = True
CANCEL_MANAGED_OPEN_ORDERS_FIRST = True

# Request / execution controls.
REQUEST_TIMEOUT_SECONDS = 30
READ_RETRIES = 4
READ_RETRY_BASE_SECONDS = 1.0
ORDER_WRITE_DELAY_SECONDS = 0.25
MAX_ORDER_WRITES_PER_RUN = 60
RECV_WINDOW_MS = 10_000

# CMC historical snapshot source used by the research harness.
CMC_WEB_API_URL = "https://web-api.coinmarketcap.com/v1/cryptocurrency/listings/historical"
CMC_HISTORICAL_URL = "https://coinmarketcap.com/historical/{datecode}/"
CMC_SNAPSHOT_DEPTH = 100
CMC_MAX_RETRIES = 4
CMC_RETRY_DELAY_SECONDS = 1.0

# Local state is only a weekly completion marker / audit snapshot; no database.
STATE_FILE = Path(__file__).with_name("binance_weekly_w4_testnet_state.json")

# If True, refuses authenticated order writes when launched outside UTC Monday.
# Leave False while validating the Testnet bot manually; schedule Monday in production-like testing.
REQUIRE_UTC_MONDAY_FOR_WRITES = False

EXCLUDE_STABLECOINS = True
STABLECOIN_SYMBOLS = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "PAX", "GUSD",
    "FRAX", "LUSD", "USDD", "FDUSD", "PYUSD", "USDN", "UST", "USTC",
    "EURS", "EURT", "USDE", "SUSD", "HUSD", "CUSD", "MIM", "RLUSD",
    "USD1", "USDS", "USDX", "FDUSD", "EURC",
}
EXCLUDE_SYMBOLS: set[str] = set()

# Add only after verifying a current CMC-symbol/Binance-pair mismatch.
# Example: {1234: "NEWNAMEUSDT"}
BINANCE_PAIR_OVERRIDES_BY_CMC_ID: Dict[int, str] = {}


# =============================================================================
# SMALL HELPERS
# =============================================================================


def print_rule(title: str, width: int = 118) -> None:
    print("\n" + "=" * width)
    print(title)
    print("=" * width)


def wait_for_keypress() -> None:
    print("\nPress any key to close this window...", flush=True)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.getch()
        else:
            input()
    except Exception:
        pass


def safe_float(value: Any, default: float = float("nan")) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def decimal_from(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def decimal_str(value: Decimal) -> str:
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def normalize_symbol(value: str) -> str:
    return str(value or "").strip().upper()


def date_code(ts: "pd.Timestamp") -> str:
    return pd.Timestamp(ts).strftime("%Y%m%d")


def latest_completed_sunday_utc(now: Optional[datetime] = None) -> "pd.Timestamp":
    now = now or datetime.now(timezone.utc)
    today = pd.Timestamp(now).tz_convert("UTC").normalize().tz_localize(None)
    last_completed_day = today - pd.Timedelta(days=1)
    offset = (last_completed_day.weekday() - 6) % 7
    return (last_completed_day - pd.Timedelta(days=offset)).normalize()


def utc_ms(ts: "pd.Timestamp") -> int:
    x = pd.Timestamp(ts)
    if x.tzinfo is None:
        x = x.tz_localize("UTC")
    else:
        x = x.tz_convert("UTC")
    return int(x.timestamp() * 1000)


def stable_asset_id(cmc_id: Any, slug: str, symbol: str, name: str) -> str:
    try:
        x = int(float(cmc_id))
        if x > 0:
            return f"cmc:{x}"
    except Exception:
        pass
    slug = str(slug or "").strip().lower()
    if slug:
        return f"slug:{slug}"
    return f"fallback:{normalize_symbol(symbol)}::{str(name or '').strip().lower()}"


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as error:
        print(f"WARNING: could not read state file {STATE_FILE}: {error}")
        return {}


def save_state(data: Dict[str, Any]) -> None:
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(STATE_FILE)


# =============================================================================
# COINMARKETCAP HISTORICAL TOP-50
# =============================================================================


def _clean_number(text: Any) -> float:
    if text is None:
        return float("nan")
    raw = str(text).strip().replace("$", "").replace(",", "")
    if not raw or raw in {"--", "—", "-", "N/A", "nan", "None"}:
        return float("nan")
    import re
    match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", raw)
    return safe_float(match.group(0), float("nan")) if match else float("nan")


def _normalize_cmc_snapshot(df: "pd.DataFrame", snapshot_date: "pd.Timestamp") -> "pd.DataFrame":
    x = df.copy()
    for col in ("cmc_id", "slug", "symbol", "name", "rank", "price", "market_cap"):
        if col not in x.columns:
            x[col] = np.nan if col not in {"slug", "symbol", "name"} else ""
    x["date"] = pd.Timestamp(snapshot_date).normalize()
    x["symbol"] = x["symbol"].astype(str).str.upper().str.strip()
    x["slug"] = x["slug"].astype(str).replace("nan", "").str.lower().str.strip()
    x["name"] = x["name"].astype(str).replace("nan", "").str.strip()
    for col in ("rank", "price", "market_cap", "cmc_id"):
        x[col] = pd.to_numeric(x[col], errors="coerce")
    x["asset_id"] = [
        stable_asset_id(cid, slug, sym, name)
        for cid, slug, sym, name in zip(x["cmc_id"], x["slug"], x["symbol"], x["name"])
    ]
    x = x.loc[
        x["rank"].notna()
        & (x["rank"] > 0)
        & x["symbol"].ne("")
    ].copy()
    x = x.sort_values(["rank", "asset_id"]).drop_duplicates("asset_id", keep="first")
    return x.reset_index(drop=True)


def parse_cmc_web_api(payload: Any, snapshot_date: "pd.Timestamp") -> "pd.DataFrame":
    if not isinstance(payload, dict):
        raise RuntimeError("CMC response is not a JSON object")

    data = payload.get("data")
    if isinstance(data, dict):
        items = data.get("cryptoCurrencyList") or data.get("items") or data.get("list") or []
    else:
        items = data or []

    if not isinstance(items, list) or not items:
        raise RuntimeError("CMC web API returned no listing rows")

    rows: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue

        quote = item.get("quote") or item.get("quotes") or {}
        usd = quote.get("USD") if isinstance(quote, dict) else None
        if usd is None and isinstance(item.get("quotes"), list):
            for q in item.get("quotes") or []:
                if str(q.get("name") or q.get("symbol") or "").upper() == "USD":
                    usd = q
                    break
        usd = usd or {}

        rank = item.get("cmc_rank", item.get("rank"))
        cmc_id = item.get("id", item.get("cmcId"))
        symbol = normalize_symbol(item.get("symbol", ""))
        name = str(item.get("name") or "").strip()
        slug = str(item.get("slug") or "").strip().lower()
        price = usd.get("price", item.get("price")) if isinstance(usd, dict) else item.get("price")
        market_cap = usd.get("market_cap", usd.get("marketCap", item.get("marketCap"))) if isinstance(usd, dict) else item.get("marketCap")

        rank_f = safe_float(rank)
        if not symbol or not math.isfinite(rank_f):
            continue
        rows.append({
            "date": pd.Timestamp(snapshot_date).normalize(),
            "rank": int(rank_f),
            "name": name,
            "symbol": symbol,
            "slug": slug,
            "cmc_id": safe_float(cmc_id),
            "market_cap": safe_float(market_cap),
            "price": safe_float(price),
        })

    if not rows:
        raise RuntimeError("CMC web API payload contained no usable rows")
    return _normalize_cmc_snapshot(pd.DataFrame(rows), snapshot_date)


def parse_cmc_html(html: str, snapshot_date: "pd.Timestamp") -> "pd.DataFrame":
    soup = BeautifulSoup(html, "html.parser")
    rows: List[Dict[str, Any]] = []

    for table in soup.find_all("table"):
        headers = [str(th.get_text(" ", strip=True)).lower().strip() for th in table.find_all("th")]
        normalized = [" ".join(h.split()) for h in headers]
        needed = {"rank", "name", "symbol", "market cap", "price"}
        if not needed.issubset(set(normalized)):
            continue
        idx = {h: i for i, h in enumerate(normalized)}
        max_idx = max(idx[h] for h in needed)

        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if not cells or len(cells) <= max_idx:
                continue
            import re
            m = re.search(r"\d+", cells[idx["rank"]].get_text(" ", strip=True))
            if not m:
                continue
            rank = int(m.group(0))
            symbol = normalize_symbol(cells[idx["symbol"]].get_text(" ", strip=True))
            if not symbol:
                continue

            name_cell = cells[idx["name"]]
            slug = ""
            name = name_cell.get_text(" ", strip=True)
            for a in name_cell.find_all("a", href=True):
                href = a.get("href", "")
                sm = re.search(r"/currencies/([^/]+)/?", href)
                if sm:
                    slug = sm.group(1).strip().lower()
                    if a.get_text(" ", strip=True):
                        name = a.get_text(" ", strip=True)

            cmc_id = float("nan")
            for key in ("data-id", "data-coin-id", "data-cmc-id", "data-cryptocurrency-id"):
                raw = tr.attrs.get(key)
                if raw is not None:
                    try:
                        cmc_id = int(str(raw))
                        break
                    except Exception:
                        pass

            rows.append({
                "date": pd.Timestamp(snapshot_date).normalize(),
                "rank": rank,
                "name": name,
                "symbol": symbol,
                "slug": slug,
                "cmc_id": cmc_id,
                "market_cap": _clean_number(cells[idx["market cap"]].get_text(" ", strip=True)),
                "price": _clean_number(cells[idx["price"]].get_text(" ", strip=True)),
            })
        if rows:
            break

    if not rows:
        raise RuntimeError("Could not parse CoinMarketCap historical table")
    return _normalize_cmc_snapshot(pd.DataFrame(rows), snapshot_date)


def download_cmc_snapshot(snapshot_date: "pd.Timestamp") -> "pd.DataFrame":
    snapshot_date = pd.Timestamp(snapshot_date).normalize()
    params = {
        "convert": "USD",
        "date": snapshot_date.strftime("%Y-%m-%d"),
        "limit": int(CMC_SNAPSHOT_DEPTH),
        "start": 1,
    }
    headers_json = {
        "User-Agent": "Mozilla/5.0 Chrome/124.0 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://coinmarketcap.com/",
    }
    headers_html = {
        "User-Agent": "Mozilla/5.0 Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    last_error: Optional[Exception] = None
    for attempt in range(1, CMC_MAX_RETRIES + 1):
        try:
            response = requests.get(
                CMC_WEB_API_URL,
                params=params,
                headers=headers_json,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code == 200:
                out = parse_cmc_web_api(response.json(), snapshot_date)
                if len(out):
                    return out
            last_error = RuntimeError(f"CMC web API HTTP {response.status_code}: {response.text[:160]}")
        except Exception as error:
            last_error = error

        try:
            url = CMC_HISTORICAL_URL.format(datecode=date_code(snapshot_date))
            response = requests.get(url, headers=headers_html, timeout=REQUEST_TIMEOUT_SECONDS)
            if response.status_code != 200:
                raise RuntimeError(f"CMC HTML HTTP {response.status_code}")
            out = parse_cmc_html(response.text, snapshot_date)
            if len(out):
                return out
        except Exception as error:
            last_error = error

        if attempt < CMC_MAX_RETRIES:
            time.sleep(CMC_RETRY_DELAY_SECONDS * (2 ** (attempt - 1)))

    raise RuntimeError(f"Failed to load CMC snapshot for {snapshot_date.date()}: {last_error}")


def make_cmc_universe(snapshot: "pd.DataFrame") -> "pd.DataFrame":
    x = snapshot.loc[snapshot["rank"] <= MARKET_CAP_TOP_N].copy()
    if EXCLUDE_STABLECOINS:
        x = x.loc[~x["symbol"].isin(STABLECOIN_SYMBOLS)].copy()
    if EXCLUDE_SYMBOLS:
        x = x.loc[~x["symbol"].isin(EXCLUDE_SYMBOLS)].copy()
    return x.sort_values("rank").reset_index(drop=True)


def pair_for_cmc_row(row: "pd.Series") -> str:
    try:
        cmc_id = int(float(row.get("cmc_id", float("nan"))))
    except Exception:
        cmc_id = -1
    if cmc_id in BINANCE_PAIR_OVERRIDES_BY_CMC_ID:
        return normalize_symbol(BINANCE_PAIR_OVERRIDES_BY_CMC_ID[cmc_id])
    return f"{normalize_symbol(row['symbol'])}{BINANCE_QUOTE_ASSET}"


# =============================================================================
# BINANCE CLIENT
# =============================================================================


class UnknownExecutionStateError(RuntimeError):
    """An order write may have reached Binance but the response was not trustworthy."""


class BinanceClient:
    def __init__(self, api_key: str, api_secret: str):
        if "testnet.binance.vision" not in TESTNET_BASE_URL.lower():
            raise RuntimeError("SAFETY LOCK: TESTNET_BASE_URL is not Binance Spot Testnet")
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.session = requests.Session()
        self.time_offset_ms = 0
        self._public_exchange_info: Optional[Dict[str, Any]] = None
        self._testnet_exchange_info: Optional[Dict[str, Any]] = None

    def _request_read(
        self,
        base_url: str,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        api_key: bool = False,
        retries: int = READ_RETRIES,
    ) -> Any:
        headers = {"X-MBX-APIKEY": self.api_key} if api_key and self.api_key else {}
        url = base_url.rstrip("/") + path
        last_error: Optional[Exception] = None

        for attempt in range(retries):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as error:
                last_error = error
                if attempt == retries - 1:
                    raise
                time.sleep(READ_RETRY_BASE_SECONDS * (2 ** attempt))
                continue

            if response.status_code in {418, 429}:
                wait = safe_float(response.headers.get("Retry-After"), 2 ** (attempt + 1))
                if attempt == retries - 1:
                    raise RuntimeError(f"Binance rate limit HTTP {response.status_code}: {response.text}")
                time.sleep(max(1.0, wait))
                continue

            if response.status_code >= 500:
                last_error = RuntimeError(f"Binance HTTP {response.status_code}: {response.text[:300]}")
                if attempt == retries - 1:
                    raise last_error
                time.sleep(READ_RETRY_BASE_SECONDS * (2 ** attempt))
                continue

            if not response.ok:
                raise RuntimeError(f"Binance API error {method} {path} HTTP {response.status_code}: {response.text}")

            if not response.text:
                return {}
            return response.json()

        raise RuntimeError(f"Binance read retries exhausted: {last_error}")

    def _encode_for_signature(self, params: Dict[str, Any]) -> str:
        clean: List[Tuple[str, str]] = []
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            clean.append((str(key), str(value)))
        # Binance 2026 docs require percent-encoding before signature computation.
        return urlencode(clean, doseq=True, quote_via=quote, safe="")

    def _signed_request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        execution_write: bool = False,
    ) -> Any:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Binance Testnet API key/secret are blank")

        payload = dict(params or {})
        payload["timestamp"] = int(time.time() * 1000) + int(self.time_offset_ms)
        payload["recvWindow"] = int(RECV_WINDOW_MS)
        encoded = self._encode_for_signature(payload)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            encoded.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        url = f"{TESTNET_BASE_URL.rstrip('/')}{path}?{encoded}&signature={signature}"
        headers = {"X-MBX-APIKEY": self.api_key}

        attempts = 1 if execution_write else READ_RETRIES
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as error:
                if execution_write:
                    raise UnknownExecutionStateError(
                        f"Network error during Binance order write; result UNKNOWN and was not retried: {error}"
                    ) from error
                if attempt == attempts - 1:
                    raise
                time.sleep(READ_RETRY_BASE_SECONDS * (2 ** attempt))
                continue

            if execution_write and response.status_code >= 500:
                raise UnknownExecutionStateError(
                    f"Binance returned HTTP {response.status_code} during order write; result may be UNKNOWN: "
                    f"{response.text[:300]}"
                )

            if response.status_code in {418, 429}:
                if execution_write:
                    # An explicit rate-limit rejection is safe to treat as not accepted,
                    # but we still halt this run rather than recursively retrying writes.
                    raise RuntimeError(f"Binance rate limit on order write HTTP {response.status_code}: {response.text}")
                wait = safe_float(response.headers.get("Retry-After"), 2 ** (attempt + 1))
                if attempt == attempts - 1:
                    raise RuntimeError(f"Binance rate limit HTTP {response.status_code}: {response.text}")
                time.sleep(max(1.0, wait))
                continue

            if not response.ok:
                # -1021 can happen when local clock drifts. For reads, resync and retry.
                try:
                    body = response.json()
                except Exception:
                    body = {"msg": response.text}
                if not execution_write and body.get("code") == -1021 and attempt < attempts - 1:
                    self.sync_time()
                    continue
                raise RuntimeError(
                    f"Binance signed API error {method} {path} HTTP {response.status_code}: {response.text}"
                )

            if not response.text:
                return {}
            return response.json()

        raise RuntimeError(f"Signed request retries exhausted: {method} {path}")

    # -------------------- public market reads --------------------

    def public_exchange_info(self) -> Dict[str, Any]:
        if self._public_exchange_info is None:
            self._public_exchange_info = self._request_read(
                PUBLIC_MARKET_BASE_URL, "GET", "/api/v3/exchangeInfo"
            )
        return self._public_exchange_info

    def testnet_exchange_info(self) -> Dict[str, Any]:
        if self._testnet_exchange_info is None:
            self._testnet_exchange_info = self._request_read(
                TESTNET_BASE_URL, "GET", "/api/v3/exchangeInfo"
            )
        return self._testnet_exchange_info

    def production_klines(self, symbol: str, signal_date: "pd.Timestamp", days: int = 70) -> "pd.DataFrame":
        signal_date = pd.Timestamp(signal_date).normalize()
        start = signal_date - pd.Timedelta(days=days)
        end = signal_date + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
        payload = self._request_read(
            PUBLIC_MARKET_BASE_URL,
            "GET",
            "/api/v3/klines",
            params={
                "symbol": normalize_symbol(symbol),
                "interval": "1d",
                "startTime": utc_ms(start),
                "endTime": utc_ms(end),
                "limit": 1000,
            },
        )
        if not isinstance(payload, list) or not payload:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "quote_volume"])

        rows = []
        for k in payload:
            if not isinstance(k, list) or len(k) < 8:
                continue
            ts = pd.to_datetime(int(k[0]), unit="ms", utc=True).tz_convert(None).normalize()
            rows.append({
                "date": ts,
                "open": safe_float(k[1]),
                "high": safe_float(k[2]),
                "low": safe_float(k[3]),
                "close": safe_float(k[4]),
                "volume": safe_float(k[5]),
                "quote_volume": safe_float(k[7]),
            })
        frame = pd.DataFrame(rows)
        if frame.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "quote_volume"])
        frame = frame.dropna(subset=["date", "close"]).set_index("date").sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        return frame

    def testnet_ticker_price(self, symbol: str) -> float:
        data = self._request_read(
            TESTNET_BASE_URL,
            "GET",
            "/api/v3/ticker/price",
            params={"symbol": normalize_symbol(symbol)},
        )
        return safe_float(data.get("price")) if isinstance(data, dict) else float("nan")

    # -------------------- private reads --------------------

    def sync_time(self) -> None:
        data = self._request_read(TESTNET_BASE_URL, "GET", "/api/v3/time")
        server_ms = int(data["serverTime"])
        self.time_offset_ms = server_ms - int(time.time() * 1000)

    def account(self) -> Dict[str, Any]:
        return self._signed_request("GET", "/api/v3/account")

    def open_orders(self) -> List[Dict[str, Any]]:
        data = self._signed_request("GET", "/api/v3/openOrders")
        return data if isinstance(data, list) else []

    def all_orders(self, symbol: str, start_time_ms: Optional[int] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"symbol": normalize_symbol(symbol), "limit": 1000}
        if start_time_ms is not None:
            params["startTime"] = int(start_time_ms)
        data = self._signed_request("GET", "/api/v3/allOrders", params=params)
        return data if isinstance(data, list) else []

    # -------------------- writes (TESTNET ONLY) --------------------

    def cancel_all_open_orders(self, symbol: str) -> Any:
        return self._signed_request(
            "DELETE",
            "/api/v3/openOrders",
            params={"symbol": normalize_symbol(symbol)},
            execution_write=True,
        )

    def market_buy_quote(self, symbol: str, quote_usdt: Decimal, client_order_id: str) -> Any:
        return self._signed_request(
            "POST",
            "/api/v3/order",
            params={
                "symbol": normalize_symbol(symbol),
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": decimal_str(quote_usdt),
                "newClientOrderId": client_order_id,
                "newOrderRespType": "FULL",
            },
            execution_write=True,
        )

    def market_sell_qty(self, symbol: str, quantity: Decimal, client_order_id: str) -> Any:
        return self._signed_request(
            "POST",
            "/api/v3/order",
            params={
                "symbol": normalize_symbol(symbol),
                "side": "SELL",
                "type": "MARKET",
                "quantity": decimal_str(quantity),
                "newClientOrderId": client_order_id,
                "newOrderRespType": "FULL",
            },
            execution_write=True,
        )


# =============================================================================
# EXCHANGE INFO / FILTERS
# =============================================================================


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    quote_order_qty_market_allowed: bool
    min_qty: Decimal
    max_qty: Decimal
    step_size: Decimal
    min_notional: Decimal


def build_symbol_rules(exchange_info: Dict[str, Any]) -> Dict[str, SymbolRules]:
    result: Dict[str, SymbolRules] = {}
    for item in exchange_info.get("symbols", []) if isinstance(exchange_info, dict) else []:
        symbol = normalize_symbol(item.get("symbol", ""))
        base = normalize_symbol(item.get("baseAsset", ""))
        quote_asset = normalize_symbol(item.get("quoteAsset", ""))
        if not symbol or not base or not quote_asset:
            continue

        filters = {f.get("filterType"): f for f in item.get("filters", []) if isinstance(f, dict)}
        lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE") or {}
        # Some MARKET_LOT_SIZE records use stepSize=0. Fall back to LOT_SIZE then.
        if decimal_from(lot.get("stepSize")) <= 0 and filters.get("LOT_SIZE"):
            lot = filters["LOT_SIZE"]

        min_notional = Decimal("0")
        if "NOTIONAL" in filters:
            min_notional = decimal_from(filters["NOTIONAL"].get("minNotional"))
        elif "MIN_NOTIONAL" in filters:
            min_notional = decimal_from(filters["MIN_NOTIONAL"].get("minNotional"))

        result[symbol] = SymbolRules(
            symbol=symbol,
            base_asset=base,
            quote_asset=quote_asset,
            status=normalize_symbol(item.get("status", "")),
            quote_order_qty_market_allowed=bool(item.get("quoteOrderQtyMarketAllowed", True)),
            min_qty=decimal_from(lot.get("minQty")),
            max_qty=decimal_from(lot.get("maxQty"), "999999999999"),
            step_size=decimal_from(lot.get("stepSize")),
            min_notional=min_notional,
        )
    return result


def tradable_usdt_symbols(rules: Dict[str, SymbolRules]) -> set[str]:
    return {
        s for s, r in rules.items()
        if r.quote_asset == BINANCE_QUOTE_ASSET and r.status == "TRADING"
    }


# =============================================================================
# STRATEGY SIGNALS
# =============================================================================


@dataclass(frozen=True)
class Candidate:
    cmc_rank: int
    cmc_id: Optional[int]
    asset_id: str
    symbol: str
    name: str
    pair: str
    w4_momentum: float


@dataclass(frozen=True)
class StrategySnapshot:
    signal_date: "pd.Timestamp"
    btc_close: float
    btc_4w_return: float
    btc_ma8: float
    btc_above_ma8: bool
    regime_on: bool
    production_ranking: List[Candidate]
    selected_testnet: List[Candidate]
    skipped_testnet: List[Candidate]


def exact_close(frame: "pd.DataFrame", date: "pd.Timestamp") -> float:
    date = pd.Timestamp(date).normalize()
    if frame is None or frame.empty or date not in frame.index:
        return float("nan")
    return safe_float(frame.at[date, "close"])


def build_strategy_snapshot(
    client: BinanceClient,
    signal_date: "pd.Timestamp",
    cmc_universe: "pd.DataFrame",
    production_symbols: set[str],
    testnet_symbols: set[str],
) -> StrategySnapshot:
    print_rule(f"BUILDING WEEKLY SIGNAL — SUNDAY {signal_date.date()}")

    # BTC regime, exactly matching the research harness definition.
    btc_hist = client.production_klines("BTCUSDT", signal_date, days=70)
    c0 = exact_close(btc_hist, signal_date)
    c4 = exact_close(btc_hist, signal_date - pd.Timedelta(weeks=4))
    weekly_closes = [exact_close(btc_hist, signal_date - pd.Timedelta(weeks=i)) for i in range(8)]
    if not (math.isfinite(c0) and math.isfinite(c4) and c4 > 0 and all(math.isfinite(x) for x in weekly_closes)):
        raise RuntimeError("BTC history is incomplete for 4W + MA8 regime calculation")
    btc_r4 = c0 / c4 - 1.0
    btc_ma8 = float(np.mean(weekly_closes))
    btc_above_ma8 = c0 > btc_ma8
    regime_on = bool(btc_r4 > 0 and btc_above_ma8)

    print(
        f"BTC close={c0:,.4f} | BTC 4W={btc_r4:+.2%} | MA8={btc_ma8:,.4f} | "
        f"above_MA8={btc_above_ma8} | REGIME={'ON' if regime_on else 'OFF'}"
    )

    candidates: List[Candidate] = []
    failures: List[str] = []

    for _, row in cmc_universe.iterrows():
        pair = pair_for_cmc_row(row)
        if pair not in production_symbols:
            failures.append(f"{row['symbol']}: no production Binance {pair}")
            continue
        try:
            hist = client.production_klines(pair, signal_date, days=40)
            p0 = exact_close(hist, signal_date)
            p4 = exact_close(hist, signal_date - pd.Timedelta(weeks=SIGNAL_LOOKBACK_WEEKS))
            if not (math.isfinite(p0) and math.isfinite(p4) and p0 > 0 and p4 > 0):
                failures.append(f"{row['symbol']}: missing exact Sunday W4 prices")
                continue
            w4 = p0 / p4 - 1.0
            if REQUIRE_POSITIVE_MOMENTUM and w4 <= 0:
                continue
            cmc_id_val: Optional[int]
            try:
                cmc_id_val = int(float(row.get("cmc_id")))
            except Exception:
                cmc_id_val = None
            candidates.append(Candidate(
                cmc_rank=int(row["rank"]),
                cmc_id=cmc_id_val,
                asset_id=str(row["asset_id"]),
                symbol=normalize_symbol(row["symbol"]),
                name=str(row.get("name") or ""),
                pair=pair,
                w4_momentum=float(w4),
            ))
        except Exception as error:
            failures.append(f"{row['symbol']}: {error}")

    candidates.sort(key=lambda c: (-c.w4_momentum, c.cmc_rank, c.symbol))
    if not candidates:
        raise RuntimeError("No positive-W4 candidates could be constructed")

    selected: List[Candidate] = []
    skipped: List[Candidate] = []
    for candidate in candidates:
        if candidate.pair not in testnet_symbols:
            skipped.append(candidate)
            continue
        selected.append(candidate)
        if len(selected) >= TARGET_POSITIONS:
            break

    if failures:
        print(f"Candidate-data exclusions: {len(failures)} (showing first 12)")
        for msg in failures[:12]:
            print(f"  - {msg}")

    return StrategySnapshot(
        signal_date=pd.Timestamp(signal_date).normalize(),
        btc_close=c0,
        btc_4w_return=btc_r4,
        btc_ma8=btc_ma8,
        btc_above_ma8=btc_above_ma8,
        regime_on=regime_on,
        production_ranking=candidates,
        selected_testnet=selected,
        skipped_testnet=skipped,
    )


def print_strategy(snapshot: StrategySnapshot) -> None:
    print_rule("PRODUCTION BINANCE W4 RANKING")
    rows = []
    for i, c in enumerate(snapshot.production_ranking[:25], 1):
        rows.append({
            "W4Rank": i,
            "CMC": c.cmc_rank,
            "Coin": c.symbol,
            "Pair": c.pair,
            "W4": c.w4_momentum,
            "Testnet": "YES" if c in snapshot.selected_testnet or c.pair else "",
        })
    view = pd.DataFrame(rows)
    if not view.empty:
        view["W4"] = view["W4"].map(lambda x: f"{x:+.2%}")
        selected_pairs = {c.pair for c in snapshot.selected_testnet}
        view["Testnet"] = view["Pair"].map(lambda p: "SELECT" if p in selected_pairs else "")
        print(view.to_string(index=False))

    print_rule(f"TESTNET TARGET — TOP {TARGET_POSITIONS} AVAILABLE")
    if not snapshot.regime_on:
        print("BTC_4W_POS_AND_MA8 is OFF -> target portfolio is USDT cash.")
        return
    if not snapshot.selected_testnet:
        print("No selected strategy pairs exist on Testnet. No buys can be made.")
        return
    for i, c in enumerate(snapshot.selected_testnet, 1):
        print(f"#{i}: {c.pair:<14} W4={c.w4_momentum:+.2%} | CMC rank={c.cmc_rank}")
    if snapshot.skipped_testnet:
        print("\nHigher-ranked production names skipped because Spot Testnet lacks the pair:")
        for c in snapshot.skipped_testnet[:15]:
            print(f"  {c.pair:<14} W4={c.w4_momentum:+.2%}")


# =============================================================================
# PORTFOLIO STATE / VALUATION
# =============================================================================


@dataclass
class Balance:
    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass
class PortfolioSnapshot:
    balances: Dict[str, Balance]
    prices: Dict[str, float]
    managed_values: Dict[str, float]
    total_managed_equity: float
    effective_managed_equity: float
    usdt_free: float


def parse_balances(account: Dict[str, Any]) -> Dict[str, Balance]:
    out: Dict[str, Balance] = {}
    for item in account.get("balances", []) if isinstance(account, dict) else []:
        asset = normalize_symbol(item.get("asset", ""))
        if not asset:
            continue
        free = decimal_from(item.get("free"))
        locked = decimal_from(item.get("locked"))
        if free > 0 or locked > 0 or asset == BINANCE_QUOTE_ASSET:
            out[asset] = Balance(asset=asset, free=free, locked=locked)
    return out


def managed_base_assets_from_testnet_rules(rules: Dict[str, SymbolRules]) -> Dict[str, str]:
    # base asset -> pair; keep one direct USDT spot pair per base asset.
    result: Dict[str, str] = {}
    for pair, rule in rules.items():
        if rule.quote_asset == BINANCE_QUOTE_ASSET and rule.status == "TRADING":
            result.setdefault(rule.base_asset, pair)
    return result


def read_portfolio_snapshot(
    client: BinanceClient,
    testnet_rules: Dict[str, SymbolRules],
) -> PortfolioSnapshot:
    account = client.account()
    balances = parse_balances(account)
    base_to_pair = managed_base_assets_from_testnet_rules(testnet_rules)

    prices: Dict[str, float] = {BINANCE_QUOTE_ASSET: 1.0}
    values: Dict[str, float] = {}

    usdt_balance = balances.get(BINANCE_QUOTE_ASSET, Balance(BINANCE_QUOTE_ASSET, Decimal("0"), Decimal("0")))
    values[BINANCE_QUOTE_ASSET] = float(usdt_balance.total)

    for asset, bal in balances.items():
        if asset == BINANCE_QUOTE_ASSET or bal.total <= 0:
            continue
        pair = base_to_pair.get(asset)
        if not pair:
            continue
        try:
            px = client.testnet_ticker_price(pair)
        except Exception:
            continue
        if not math.isfinite(px) or px <= 0:
            continue
        prices[asset] = px
        values[asset] = float(bal.total) * px

    total = float(sum(v for v in values.values() if math.isfinite(v) and v >= 0))
    effective = total
    if MAX_MANAGED_EQUITY_USDT > 0:
        effective = min(total, float(MAX_MANAGED_EQUITY_USDT))

    return PortfolioSnapshot(
        balances=balances,
        prices=prices,
        managed_values=values,
        total_managed_equity=total,
        effective_managed_equity=effective,
        usdt_free=float(usdt_balance.free),
    )


def print_portfolio(portfolio: PortfolioSnapshot, title: str) -> None:
    print_rule(title)
    print(
        f"Managed Testnet equity (all direct USDT-pair balances): ${portfolio.total_managed_equity:,.2f}\n"
        f"Strategy equity after cap:                         ${portfolio.effective_managed_equity:,.2f}\n"
        f"Free USDT:                                         ${portfolio.usdt_free:,.2f}"
    )
    rows = []
    for asset, value in sorted(portfolio.managed_values.items(), key=lambda kv: -kv[1]):
        if value < 0.01:
            continue
        rows.append({"Asset": asset, "ValueUSDT": value})
    if rows:
        view = pd.DataFrame(rows).head(40)
        view["ValueUSDT"] = view["ValueUSDT"].map(lambda x: f"${x:,.2f}")
        print("\n" + view.to_string(index=False))


# =============================================================================
# ACTION PLANNING
# =============================================================================


@dataclass(frozen=True)
class Action:
    priority: int
    action: str
    asset: str
    pair: str
    amount_usdt: float
    quantity: Decimal
    reason: str


def effective_rebalance_threshold(equity: float) -> float:
    return max(float(MIN_REBALANCE_USDT), float(equity) * float(MIN_REBALANCE_FRACTION_OF_EQUITY))


def target_assets(snapshot: StrategySnapshot) -> List[str]:
    return [c.symbol for c in snapshot.selected_testnet] if snapshot.regime_on else []


def target_pairs(snapshot: StrategySnapshot) -> Dict[str, str]:
    return {c.symbol: c.pair for c in snapshot.selected_testnet} if snapshot.regime_on else {}


def build_sell_plan(
    strategy: StrategySnapshot,
    portfolio: PortfolioSnapshot,
    rules: Dict[str, SymbolRules],
) -> List[Action]:
    desired_assets = set(target_assets(strategy))
    pair_by_asset = managed_base_assets_from_testnet_rules(rules)
    n_targets = len(desired_assets)
    investable = max(0.0, portfolio.effective_managed_equity - float(CASH_RESERVE_USDT))
    slot = investable / n_targets if n_targets else 0.0
    threshold = effective_rebalance_threshold(portfolio.effective_managed_equity)
    actions: List[Action] = []

    for asset, balance in portfolio.balances.items():
        if asset == BINANCE_QUOTE_ASSET or balance.free <= 0:
            continue
        pair = pair_by_asset.get(asset)
        if not pair or pair not in rules:
            continue
        current_value = portfolio.managed_values.get(asset, 0.0)
        if current_value <= 0:
            continue

        if asset not in desired_assets:
            if not CLEAN_NON_TARGET_USDT_ASSETS:
                continue
            sell_value = current_value
            reason = "not in this week's target" if strategy.regime_on else "BTC regime OFF -> cash"
            priority = 1
        else:
            sell_value = current_value - slot
            if sell_value <= threshold:
                continue
            reason = f"over target slot ${slot:,.2f} by ${sell_value:,.2f}"
            priority = 2

        px = portfolio.prices.get(asset)
        if not px or not math.isfinite(px) or px <= 0:
            continue
        qty_needed = decimal_from(sell_value / px)
        qty = min(balance.free, qty_needed)
        rule = rules[pair]
        qty = floor_to_step(qty, rule.step_size)
        if qty <= 0 or qty < rule.min_qty:
            continue
        notional = float(qty) * px
        if rule.min_notional > 0 and Decimal(str(notional)) < rule.min_notional:
            continue
        if notional < threshold and asset in desired_assets:
            continue

        actions.append(Action(
            priority=priority,
            action="SELL",
            asset=asset,
            pair=pair,
            amount_usdt=notional,
            quantity=qty,
            reason=reason,
        ))

    return sorted(actions, key=lambda a: (a.priority, a.asset))


def build_buy_plan(
    strategy: StrategySnapshot,
    portfolio: PortfolioSnapshot,
    rules: Dict[str, SymbolRules],
) -> List[Action]:
    if not strategy.regime_on or not strategy.selected_testnet:
        return []

    desired = target_pairs(strategy)
    n = len(desired)
    investable = max(0.0, portfolio.effective_managed_equity - float(CASH_RESERVE_USDT))
    slot = investable / n if n else 0.0
    threshold = effective_rebalance_threshold(portfolio.effective_managed_equity)
    usable_cash = max(0.0, portfolio.usdt_free - float(CASH_RESERVE_USDT))
    actions: List[Action] = []

    for asset, pair in desired.items():
        current = portfolio.managed_values.get(asset, 0.0)
        gap = slot - current
        if gap <= threshold:
            continue
        rule = rules.get(pair)
        if rule is None or rule.status != "TRADING" or not rule.quote_order_qty_market_allowed:
            continue
        buy_amount = min(gap, usable_cash)
        if buy_amount <= 0:
            break
        if rule.min_notional > 0 and Decimal(str(buy_amount)) < rule.min_notional:
            continue
        if buy_amount < threshold:
            continue
        actions.append(Action(
            priority=5,
            action="BUY",
            asset=asset,
            pair=pair,
            amount_usdt=buy_amount,
            quantity=Decimal("0"),
            reason=f"under target slot ${slot:,.2f} by ${gap:,.2f}",
        ))
        usable_cash -= buy_amount

    return sorted(actions, key=lambda a: a.asset)


def print_plan(actions: Sequence[Action], title: str) -> None:
    print_rule(title)
    if not actions:
        print("No write actions required.")
        return
    rows = []
    for a in actions:
        rows.append({
            "Action": a.action,
            "Asset": a.asset,
            "Pair": a.pair,
            "ApproxUSDT": a.amount_usdt,
            "Qty": decimal_str(a.quantity) if a.quantity > 0 else "",
            "Reason": a.reason,
        })
    view = pd.DataFrame(rows)
    view["ApproxUSDT"] = view["ApproxUSDT"].map(lambda x: f"${x:,.2f}")
    print(view.to_string(index=False))


# =============================================================================
# EXECUTION / IDEMPOTENCY
# =============================================================================


def week_start_ms(signal_date: "pd.Timestamp") -> int:
    # Monday immediately after the Sunday signal, UTC.
    monday = pd.Timestamp(signal_date).normalize() + pd.Timedelta(days=1)
    return utc_ms(monday)


def client_order_id(signal_date: "pd.Timestamp", side: str, pair: str) -> str:
    side_letter = "B" if normalize_symbol(side) == "BUY" else "S"
    raw = f"w4{pd.Timestamp(signal_date).strftime('%y%m%d')}{side_letter}{normalize_symbol(pair)}"
    return raw[:36]


def already_submitted_this_week(
    client: BinanceClient,
    signal_date: "pd.Timestamp",
    pair: str,
    side: str,
) -> bool:
    cid = client_order_id(signal_date, side, pair)
    try:
        # Search the symbol's most recent 1000 orders. The date is encoded in our
        # deterministic clientOrderId, so no start/end window is needed. Avoiding a
        # time window also avoids Binance's 24-hour startTime/endTime restriction.
        orders = client.all_orders(pair)
    except Exception as error:
        raise RuntimeError(f"Cannot verify duplicate-safe order history for {pair}: {error}") from error
    return any(str(o.get("clientOrderId") or "") == cid for o in orders)


def maybe_cancel_managed_open_orders(
    client: BinanceClient,
    managed_pairs: set[str],
) -> int:
    if not CANCEL_MANAGED_OPEN_ORDERS_FIRST:
        return 0
    open_orders = client.open_orders()
    symbols = sorted({normalize_symbol(o.get("symbol", "")) for o in open_orders} & managed_pairs)
    writes = 0
    for symbol in symbols:
        if writes >= MAX_ORDER_WRITES_PER_RUN:
            raise RuntimeError("MAX_ORDER_WRITES_PER_RUN reached while cancelling open orders")
        print(f"CANCEL OPEN ORDERS: {symbol}")
        if EXECUTE_TESTNET_ORDERS:
            response = client.cancel_all_open_orders(symbol)
            print(response)
            time.sleep(ORDER_WRITE_DELAY_SECONDS)
        writes += 1
    return writes


def execute_actions(
    client: BinanceClient,
    strategy: StrategySnapshot,
    actions: Sequence[Action],
    writes_already: int,
) -> int:
    writes = writes_already
    for action in actions:
        if writes >= MAX_ORDER_WRITES_PER_RUN:
            raise RuntimeError(f"MAX_ORDER_WRITES_PER_RUN={MAX_ORDER_WRITES_PER_RUN} reached")

        if already_submitted_this_week(client, strategy.signal_date, action.pair, action.action):
            print(f"SKIP DUPLICATE-SAFE: {action.action} {action.pair} already has this week's bot clientOrderId")
            continue

        cid = client_order_id(strategy.signal_date, action.action, action.pair)
        if action.action == "SELL":
            print(
                f"\nTESTNET SELL {action.pair} qty={decimal_str(action.quantity)} "
                f"~${action.amount_usdt:,.2f} | {action.reason} | clientOrderId={cid}"
            )
            if EXECUTE_TESTNET_ORDERS:
                response = client.market_sell_qty(action.pair, action.quantity, cid)
                print(response)
                writes += 1
                time.sleep(ORDER_WRITE_DELAY_SECONDS)
        elif action.action == "BUY":
            amount = Decimal(str(action.amount_usdt)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            print(
                f"\nTESTNET BUY {action.pair} quoteOrderQty={decimal_str(amount)} USDT "
                f"| {action.reason} | clientOrderId={cid}"
            )
            if EXECUTE_TESTNET_ORDERS:
                response = client.market_buy_quote(action.pair, amount, cid)
                print(response)
                writes += 1
                time.sleep(ORDER_WRITE_DELAY_SECONDS)
        else:
            raise RuntimeError(f"Unknown action: {action.action}")

    return writes


# =============================================================================
# MAIN
# =============================================================================


def validate_runtime() -> Tuple[str, str]:
    if DEPENDENCY_IMPORT_ERROR is not None:
        raise RuntimeError(
            "Missing dependency. Run: pip install requests pandas numpy beautifulsoup4\n"
            f"Original import error: {DEPENDENCY_IMPORT_ERROR}"
        )

    if "testnet.binance.vision" not in TESTNET_BASE_URL.lower():
        raise RuntimeError("SAFETY LOCK FAILED: authenticated base URL is not Spot Testnet")

    api_key = BINANCE_API_KEY.strip() or os.getenv(BINANCE_API_KEY_ENV, "").strip()
    api_secret = BINANCE_API_SECRET.strip() or os.getenv(BINANCE_API_SECRET_ENV, "").strip()
    if not api_key or not api_secret:
        raise RuntimeError(
            "Paste Binance SPOT TESTNET HMAC credentials into BINANCE_API_KEY and "
            "BINANCE_API_SECRET, or set matching environment variables."
        )
    return api_key, api_secret


def main() -> None:
    api_key, api_secret = validate_runtime()
    now_utc = datetime.now(timezone.utc)
    signal_date = latest_completed_sunday_utc(now_utc)

    print_rule("BINANCE SPOT TESTNET — WEEKLY W4 MOMENTUM BOT")
    print(f"UTC now:               {now_utc.isoformat(timespec='seconds')}")
    print(f"Signal Sunday:         {signal_date.date()}")
    print(f"Strategy:              Top{TARGET_POSITIONS} positive W4 | {REGIME_NAME} | vol OFF")
    print(f"Authenticated trading: {TESTNET_BASE_URL} (TESTNET ONLY)")
    print(f"Public signal data:    {PUBLIC_MARKET_BASE_URL}")
    print(f"Execute writes:        {EXECUTE_TESTNET_ORDERS}")
    print(f"Managed equity cap:    ${MAX_MANAGED_EQUITY_USDT:,.2f}" if MAX_MANAGED_EQUITY_USDT > 0 else "Managed equity cap:    NONE")

    if REQUIRE_UTC_MONDAY_FOR_WRITES and EXECUTE_TESTNET_ORDERS and now_utc.weekday() != 0:
        raise RuntimeError("Writes are restricted to UTC Monday by REQUIRE_UTC_MONDAY_FOR_WRITES")
    if now_utc.weekday() != 0:
        print("WARNING: this strategy was backtested as Sunday signal -> Monday execution; today is not UTC Monday.")

    state = load_state()
    signal_key = signal_date.strftime("%Y-%m-%d")
    if EXECUTE_TESTNET_ORDERS and state.get("last_completed_signal") == signal_key:
        print_rule("ALREADY COMPLETED")
        print(
            f"State file says the {signal_key} weekly rebalance completed successfully.\n"
            "No repeat rebalance will be submitted. Delete/edit the state file only if you intentionally want to rerun this TESTNET week."
        )
        return

    client = BinanceClient(api_key, api_secret)
    client.sync_time()

    # Current production and testnet trading universes.
    production_rules = build_symbol_rules(client.public_exchange_info())
    testnet_rules = build_symbol_rules(client.testnet_exchange_info())
    production_symbols = tradable_usdt_symbols(production_rules)
    testnet_symbols = tradable_usdt_symbols(testnet_rules)

    print_rule(f"LOADING CMC HISTORICAL TOP-{MARKET_CAP_TOP_N}")
    cmc_snapshot = download_cmc_snapshot(signal_date)
    cmc_universe = make_cmc_universe(cmc_snapshot)
    print(f"CMC rows after rank/stablecoin exclusions: {len(cmc_universe)}")

    strategy = build_strategy_snapshot(
        client=client,
        signal_date=signal_date,
        cmc_universe=cmc_universe,
        production_symbols=production_symbols,
        testnet_symbols=testnet_symbols,
    )
    print_strategy(strategy)

    before = read_portfolio_snapshot(client, testnet_rules)
    print_portfolio(before, "TESTNET PORTFOLIO BEFORE REBALANCE")

    managed_pairs = set(testnet_symbols)
    writes = 0

    try:
        # Cancel any existing open orders on directly-managed USDT pairs so balances
        # are not locked and the weekly portfolio can be reconstructed cleanly.
        if EXECUTE_TESTNET_ORDERS:
            writes = maybe_cancel_managed_open_orders(client, managed_pairs)
            if writes:
                before = read_portfolio_snapshot(client, testnet_rules)

        sells = build_sell_plan(strategy, before, testnet_rules)
        print_plan(sells, "SELL PLAN — EXECUTED BEFORE BUYS")
        if EXECUTE_TESTNET_ORDERS:
            writes = execute_actions(client, strategy, sells, writes)

        # Refresh after sells. This mirrors the eToro bot's close-before-buy behavior
        # and prevents buying against stale cash/balance assumptions.
        after_sells = read_portfolio_snapshot(client, testnet_rules) if EXECUTE_TESTNET_ORDERS else before
        print_portfolio(after_sells, "TESTNET PORTFOLIO AFTER SELL PHASE")

        buys = build_buy_plan(strategy, after_sells, testnet_rules)
        print_plan(buys, "BUY PLAN — AFTER SELL REFRESH")
        if EXECUTE_TESTNET_ORDERS:
            writes = execute_actions(client, strategy, buys, writes)

        final_portfolio = read_portfolio_snapshot(client, testnet_rules) if EXECUTE_TESTNET_ORDERS else after_sells
        print_portfolio(final_portfolio, "TESTNET PORTFOLIO FINAL")

    except UnknownExecutionStateError as error:
        print_rule("EXECUTION HALTED — UNKNOWN BINANCE WRITE RESULT")
        print(error)
        print(
            "No additional orders will be submitted. The next run will re-read Testnet balances, open orders, "
            "and this week's clientOrderIds before acting. The weekly completion marker was NOT written."
        )
        return
    except Exception as error:
        print_rule("EXECUTION ERROR")
        print(f"{type(error).__name__}: {error}")
        traceback.print_exc()
        print("The weekly completion marker was NOT written; rerun after diagnosing the error.")
        return

    if EXECUTE_TESTNET_ORDERS:
        save_state({
            "last_completed_signal": signal_key,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "regime": REGIME_NAME,
            "regime_on": strategy.regime_on,
            "targets": [c.pair for c in strategy.selected_testnet] if strategy.regime_on else [],
            "writes_this_run": writes,
            "btc_4w_return": strategy.btc_4w_return,
            "btc_close": strategy.btc_close,
            "btc_ma8": strategy.btc_ma8,
        })

    print_rule("RUN COMPLETE")
    if EXECUTE_TESTNET_ORDERS:
        print(f"Binance Spot Testnet writes submitted: {writes}")
        print(f"Weekly completion state saved to: {STATE_FILE}")
    else:
        print("DRY RUN only; no Testnet order writes were submitted and no completion marker was written.")


if __name__ == "__main__":
    exit_code = 0
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        exit_code = 130
    except Exception as fatal_error:
        exit_code = 1
        print_rule("FATAL ERROR")
        print(f"{type(fatal_error).__name__}: {fatal_error}")
        traceback.print_exc()
    finally:
        if KEEP_WINDOW_OPEN:
            wait_for_keypress()
    sys.exit(exit_code)
