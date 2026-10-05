"""
eToro DEMO REL_ABS_GOLD_SILVER_BTC_8W bot
==========================================

Strategy
--------
- Run this script once per week (intended: Monday around 15:45, Europe/Ljubljana).
- The signal is reconstructed from market data on every run; no local state/database.
- Strategy universe is exactly:
      GOLD   -> GLD
      SILVER -> SLV
      BTC    -> BTC-USD for signals / BTC on eToro
- Build a common daily calendar using dates on which GLD, SLV and BTC all have data.
- Resample common closes to Friday-ending weeks and use only the latest COMPLETED
  Friday-ending week before the current week's Monday.
- Compute each asset's 8-week total return:
      8W return = latest weekly close / weekly close 8 weeks earlier - 1
- Relative + absolute momentum rule:
      * choose the asset with the highest 8-week return;
      * if that best return is > 0, target 100% of strategy capital in that asset;
      * otherwise target CASH.
- On a target change, close all non-target direct holdings and buy/top-up the target
  using available cash after close settlement, while retaining CASH_RESERVE_USD.
- There is NO 50/200-week entry filter and NO separate moving-average stop. Weekly
  rotation (including a cash signal) is the complete exit/risk rule for this strategy.

Idempotency / no external database
----------------------------------
Every run reconstructs state from:
- the latest completed 8-week momentum signal;
- current eToro positions;
- current eToro pending open/close orders.

Automatic-account cleanup
-------------------------
This DEMO account is assumed to be fully controlled by this bot. Any direct
position outside GLD / SLV / BTC is treated as stale portfolio clutter and closed
automatically. Any stale pending opening order outside the active target is
cancelled. Existing short, leveraged, or unsupported-settlement direct positions
are also treated as cleanup items; NEW purchases remain REAL-or-CFD / LONG / x1.

Credentials
-----------
Paste your DEMO API credentials into ETORO_API_KEY and ETORO_USER_KEY in the
CONFIG section below. Environment variables with the same names are accepted as
a fallback when the paste-ready variables are left blank.

Dependencies:
    pip install requests pandas numpy yfinance
"""

from __future__ import annotations

import math
import os
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

DEPENDENCY_IMPORT_ERROR: Optional[Exception] = None
try:
    import numpy as np
    import pandas as pd
    import requests
    import yfinance as yf
except Exception as _dependency_error:
    # Keep the module alive so the top-level try/finally can print the problem
    # and still honor KEEP_WINDOW_OPEN instead of the console disappearing.
    DEPENDENCY_IMPORT_ERROR = _dependency_error
    np = None  # type: ignore[assignment]
    pd = None  # type: ignore[assignment]
    requests = None  # type: ignore[assignment]
    yf = None  # type: ignore[assignment]


# =============================================================================
# CONFIG
# =============================================================================

BASE_URL = "https://public-api.etoro.com"
LOCAL_TZ = ZoneInfo("Europe/Ljubljana")

# -----------------------------------------------------------------------------
# PASTE YOUR eTORO DEMO API CREDENTIALS HERE
# -----------------------------------------------------------------------------
ETORO_API_KEY = ""
ETORO_USER_KEY = ""

# Demo only. There are no real-account execution endpoints anywhere in this bot.
EXECUTE_TRADES = True

STRATEGY_ASSETS = ("GLD", "SLV", "BTC")
MOMENTUM_WEEKS = 8
TARGET_WEIGHT = 1.0
TOPUP_TOLERANCE_USD = 1.00

CASH_RESERVE_USD = 5.00
MAX_WRITES_PER_RUN = 18  # below eToro's shared 20 writes / 60 sec quota
WRITE_DELAY_SECONDS = 3.2
CLOSE_FILL_WAIT_SECONDS = 45.0
CLOSE_FILL_POLL_SECONDS = 2.0
REQUEST_TIMEOUT = 30
READ_RETRIES = 4
HISTORY_PAGE_SIZE = 200
MAX_HISTORY_PAGES = 10

DOWNLOAD_PERIOD = "6y"  # ample buffer for the 8-week signal and data-quality gaps
YF_RETRIES = 3
YF_RETRY_DELAY_SECONDS = 1.25

CLEAN_NON_STRATEGY_POSITIONS = True
CLEAN_POLICY_VIOLATIONS = True
ALLOWED_SETTLEMENT_TYPES = ("REAL", "CFD")
REQUIRED_LEVERAGE = 1
ALLOWED_SETTLEMENT_TYPE_IDS = {0, 1}  # 0=CFD, 1=REAL

# Keep the terminal visible at the end, including after a fatal exception.
KEEP_WINDOW_OPEN = True

# -----------------------------------------------------------------------------
# Strategy universe. These symbols match the backtest proxies.
# -----------------------------------------------------------------------------
RAW_UNIVERSE = [
    "GLD",  # Gold proxy used by the backtest
    "SLV",  # Silver proxy used by the backtest
    "BTC",  # Yahoo signal series resolves to BTC-USD
]

# Yahoo Finance symbol candidates. First successful candidate is used.
YAHOO_CANDIDATES: Dict[str, List[str]] = {
    "BTC": ["BTC-USD"],
    "ETH": ["ETH-USD"],
    "SOL": ["SOL-USD"],
    "BNB": ["BNB-USD"],
    "XRP": ["XRP-USD"],
    "LINK": ["LINK-USD"],
    "DOT": ["DOT-USD"],
    "NESM": ["NESN.SW"],
    "1810": ["1810.HK"],
    "SMSN": ["SMSN.IL"],
    "ADS": ["ADS.DE"],
    "PUM": ["PUM.DE"],
    "SND": ["SU.PA"],  # Schneider Electric
    "XDJP.L": ["XDJP.L"],
    "2800.HK": ["2800.HK"],
    "COPA.L": ["COPA.L"],
    "U3O8.DE": ["U3O8.DE", "IE0005YK6564.SG"],  # same fund / EUR fallback on Yahoo
    "SXEPEX.DE": ["EXH1.DE", "SXEPEX.DE"],  # same Xetra fund; Yahoo uses EXH1.DE
    "0700.HK": ["0700.HK"],
    "OR.PA": ["OR.PA"],
    "SIE.DE": ["SIE.DE"],
    "7974.T": ["7974.T"],
    "VOLV-B.ST": ["VOLV-B.ST"],
    "VOW.DE": ["VOW.DE"],
    "HEIA.NV": ["HEIA.AS", "HEIA.NV"],
    "0992.HK": ["0992.HK"],
    "01211.HK": ["1211.HK", "01211.HK"],
    "FRES.L": ["FRES.L"],
}

# eToro symbol aliases. The code first searches the complete eToro instrument
# catalogue, then falls back to eToro's search endpoint for unresolved assets.
ETORO_CANDIDATES: Dict[str, List[str]] = {
    "BTC": ["BTC", "BTCUSD"],
    "ETH": ["ETH", "ETHUSD"],
    "SOL": ["SOL", "SOLUSD"],
    "BNB": ["BNB", "BNBUSD"],
    "NESM": ["NESM", "NESN.ZU", "NESN.SW", "NESN"],
    "1810": ["1810.HK", "1810"],
    "SMSN": ["SMSN", "SMSN.L"],
    "ADS": ["ADS.DE", "ADS"],
    "PUM": ["PUM.DE", "PUM"],
    "SND": ["SND", "SU.PA", "SU"],
    "XDJP.L": ["XDJP.L", "XDJP"],
    "2800.HK": ["2800.HK", "2800"],
    "COPA.L": ["COPA.L", "COPA"],
    "U3O8.DE": ["U3O8.DE", "U3O8"],
    "SXEPEX.DE": ["SXEPEX.DE", "SXEPEX"],
    "0700.HK": ["0700.HK", "0700"],
    "OR.PA": ["OR.PA", "OR"],
    "SIE.DE": ["SIE.DE", "SIE"],
    "7974.T": ["7974.T", "7974"],
    "VOLV-B.ST": ["VOLV-B.ST", "VOLV-B"],
    "VOW.DE": ["VOW.DE", "VOW"],
    "HEIA.NV": ["HEIA.NV", "HEIA"],
    "0992.HK": ["0992.HK", "0992"],
    "01211.HK": ["01211.HK", "1211.HK", "1211"],
    "FRES.L": ["FRES.L", "FRES"],
}


# =============================================================================
# SMALL HELPERS
# =============================================================================


def dedupe_preserve_order(values: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for raw in values:
        value = str(raw).strip().upper()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


UNIVERSE = dedupe_preserve_order(RAW_UNIVERSE)


def value_from(dictionary: Dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in dictionary:
            return dictionary[name]
    return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def parse_utc_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return ts.to_pydatetime()
    except Exception:
        return None


def print_rule(title: str, width: int = 118) -> None:
    print("\n" + "=" * width)
    print(title)
    print("=" * width)


def month_regime_start(now_local: datetime) -> datetime:
    """Start of the current calendar month in Europe/Ljubljana."""
    return datetime(now_local.year, now_local.month, 1, tzinfo=LOCAL_TZ)


def monday_of_week(dt_local: datetime) -> datetime:
    day = dt_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


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


# =============================================================================
# MARKET DATA / STRATEGY SIGNALS
# =============================================================================


def _extract_ohlc(data: pd.DataFrame, yahoo_symbol: str) -> pd.DataFrame:
    if data is None or data.empty:
        raise RuntimeError("empty Yahoo response")

    d = data.copy()
    if isinstance(d.columns, pd.MultiIndex):
        # yfinance may return either OHLC->ticker or ticker->OHLC.
        if yahoo_symbol in d.columns.get_level_values(-1):
            d = d.xs(yahoo_symbol, axis=1, level=-1)
        elif yahoo_symbol in d.columns.get_level_values(0):
            d = d.xs(yahoo_symbol, axis=1, level=0)
        else:
            # Single ticker frequently has the ticker level but not always under
            # exactly the requested spelling. Collapse the level containing OHLC.
            if "Close" in d.columns.get_level_values(0):
                d = d.droplevel(-1, axis=1)
            elif "Close" in d.columns.get_level_values(-1):
                d = d.droplevel(0, axis=1)

    required = ["Open", "Close"]
    for column in required:
        if column not in d.columns:
            raise RuntimeError(f"{column} column missing")

    out = d[["Open", "Close"]].copy()
    out["Open"] = pd.to_numeric(out["Open"], errors="coerce")
    out["Close"] = pd.to_numeric(out["Close"], errors="coerce")
    out = out.replace([np.inf, -np.inf], np.nan).dropna(subset=["Open", "Close"])
    out = out[(out["Open"] > 0) & (out["Close"] > 0)]
    out.index = pd.to_datetime(out.index)
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_localize(None)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    if len(out) < 20:
        raise RuntimeError("too little usable history")
    return out


def download_one_asset(asset: str) -> Tuple[pd.DataFrame, str]:
    candidates = YAHOO_CANDIDATES.get(asset, [asset])
    errors: List[str] = []

    for yahoo_symbol in candidates:
        last_error: Optional[Exception] = None
        for attempt in range(YF_RETRIES):
            try:
                data = yf.download(
                    yahoo_symbol,
                    period=DOWNLOAD_PERIOD,
                    interval="1d",
                    auto_adjust=True,
                    actions=False,
                    progress=False,
                    threads=False,
                    group_by="column",
                    timeout=30,
                )
                return _extract_ohlc(data, yahoo_symbol), yahoo_symbol
            except Exception as error:
                last_error = error
                if attempt < YF_RETRIES - 1:
                    time.sleep(YF_RETRY_DELAY_SECONDS)
        errors.append(f"{yahoo_symbol}: {last_error}")

    raise RuntimeError(" | ".join(errors))


def download_universe() -> Tuple[Dict[str, pd.DataFrame], Dict[str, str], Dict[str, str]]:
    print_rule(f"DOWNLOADING {len(UNIVERSE)} ASSETS")
    market_data: Dict[str, pd.DataFrame] = {}
    resolved_yahoo: Dict[str, str] = {}
    failures: Dict[str, str] = {}

    for index, asset in enumerate(UNIVERSE, 1):
        try:
            frame, yahoo_symbol = download_one_asset(asset)
            market_data[asset] = frame
            resolved_yahoo[asset] = yahoo_symbol
            print(f"[{index:02d}/{len(UNIVERSE)}] {asset:<10} -> {yahoo_symbol:<14} OK ({len(frame)} daily bars)")
        except Exception as error:
            failures[asset] = str(error)
            print(f"[{index:02d}/{len(UNIVERSE)}] {asset:<10} FAILED: {error}")

    if not market_data:
        raise RuntimeError("No market data could be downloaded.")
    return market_data, resolved_yahoo, failures


@dataclass(frozen=True)
class MomentumSignal:
    signal_week: pd.Timestamp
    start_week: pd.Timestamp
    returns_8w: Dict[str, float]
    start_closes: Dict[str, float]
    latest_closes: Dict[str, float]
    winner: str
    best_return: float
    target_asset: Optional[str]


def completed_weekly_closes(
    market_data: Dict[str, pd.DataFrame],
    now_local: datetime,
) -> pd.DataFrame:
    """
    Recreate the weekly signal calendar without using any current-week data.

    The strategy is intended to run on Monday.  To keep repeated runs during the
    same week idempotent, freeze the signal cutoff at Monday 00:00 Europe/Ljubljana.
    Daily closes are first aligned to dates shared by GLD, SLV and BTC, matching
    the backtest's common executable-calendar idea for the assets actually used by
    this strategy.  Friday-ending weeks then use the last common close in the week.
    """
    missing = [asset for asset in STRATEGY_ASSETS if asset not in market_data]
    if missing:
        raise RuntimeError(f"Missing market data for required strategy assets: {missing}")

    cutoff_local = monday_of_week(now_local)
    cutoff_naive = pd.Timestamp(cutoff_local.replace(tzinfo=None))

    closes = pd.concat(
        {asset: market_data[asset]["Close"] for asset in STRATEGY_ASSETS},
        axis=1,
        join="inner",
    )
    closes = closes.replace([np.inf, -np.inf], np.nan).dropna(how="any")
    closes = closes[closes.index < cutoff_naive]
    if closes.empty:
        raise RuntimeError("No common GLD/SLV/BTC closes are available before this week's Monday cutoff.")

    weekly = closes.resample("W-FRI").last().dropna(how="any")
    weekly = weekly[weekly.index < cutoff_naive]
    if len(weekly) < MOMENTUM_WEEKS + 1:
        raise RuntimeError(
            f"Need at least {MOMENTUM_WEEKS + 1} completed weekly observations; got {len(weekly)}."
        )
    return weekly.astype(float)


def build_8w_signal(
    market_data: Dict[str, pd.DataFrame],
    now_local: datetime,
) -> MomentumSignal:
    weekly = completed_weekly_closes(market_data, now_local)
    latest = weekly.iloc[-1]
    start = weekly.iloc[-(MOMENTUM_WEEKS + 1)]

    returns = latest / start - 1.0
    if not np.isfinite(returns.to_numpy(dtype=float)).all():
        raise RuntimeError("Non-finite 8-week momentum return in required strategy assets.")

    winner = str(returns.idxmax())
    best_return = float(returns.loc[winner])
    target_asset = winner if best_return > 0.0 else None

    return MomentumSignal(
        signal_week=pd.Timestamp(weekly.index[-1]),
        start_week=pd.Timestamp(weekly.index[-(MOMENTUM_WEEKS + 1)]),
        returns_8w={asset: float(returns.loc[asset]) for asset in STRATEGY_ASSETS},
        start_closes={asset: float(start.loc[asset]) for asset in STRATEGY_ASSETS},
        latest_closes={asset: float(latest.loc[asset]) for asset in STRATEGY_ASSETS},
        winner=winner,
        best_return=best_return,
        target_asset=target_asset,
    )


# =============================================================================
# ETORO CLIENT
# =============================================================================


class UnknownExecutionStateError(RuntimeError):
    """A write may have reached eToro but the client did not get a response."""


class EtoroClient:
    def __init__(self, api_key: str, user_key: str):
        self.api_key = api_key
        self.user_key = user_key
        self.session = requests.Session()
        self.catalog_by_symbol: Dict[str, Dict[str, Any]] = {}
        self.instrument_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def _headers(
        self,
        request_id: str,
        json_request: bool = False,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        headers = {
            "x-api-key": self.api_key,
            "x-user-key": self.user_key,
            "x-request-id": request_id,
        }
        if json_request:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        execution_write: bool = False,
        max_retries: int = READ_RETRIES,
    ) -> Any:
        """
        Read-like requests retry network failures and 429 responses.

        Execution writes deliberately DO NOT retry a network exception: if the
        request was accepted by eToro but the response was lost, blindly retrying
        could duplicate a trade. The caller aborts remaining writes and the next
        scheduled run reconstructs state from eToro.
        """
        url = BASE_URL + path
        request_id = str(uuid.uuid4())  # one logical operation -> one request id
        attempts = 1 if execution_write else max_retries

        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=payload,
                    headers=self._headers(
                        request_id=request_id,
                        json_request=payload is not None,
                        extra_headers=extra_headers,
                    ),
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as error:
                if execution_write:
                    raise UnknownExecutionStateError(
                        f"Network error during execution write. Result is UNKNOWN; "
                        f"no automatic retry was attempted. {method} {path}: {error}"
                    ) from error
                if attempt == attempts - 1:
                    raise
                wait = 2 ** attempt
                print(f"Read network error: {error}; retrying in {wait}s...")
                time.sleep(wait)
                continue

            if response.status_code == 429:
                # A 429 explicitly means the operation was not accepted for
                # execution under the current rate budget, so retry is safe.
                if attempt == attempts - 1 and execution_write:
                    raise RuntimeError(f"eToro rate limit on execution write: {response.text}")
                wait = 2 ** (attempt + 1)
                print(f"eToro rate limit; waiting {wait}s...")
                time.sleep(wait)
                if execution_write:
                    # make one controlled retry after an explicit 429 only
                    return self.request(
                        method,
                        path,
                        params=params,
                        payload=payload,
                        extra_headers=extra_headers,
                        execution_write=True,
                        max_retries=1,
                    )
                continue

            if not response.ok:
                raise RuntimeError(
                    f"eToro API error | {method} {path} | HTTP {response.status_code} | {response.text}"
                )

            if not response.text:
                return {}
            try:
                return response.json()
            except Exception:
                return {"raw": response.text}

        raise RuntimeError(f"Maximum retries exceeded for {method} {path}")

    # -------------------- reads --------------------

    def get_aggregate(self) -> Dict[str, Any]:
        return self.request("GET", "/api/v1/trading/info/demo/aggregate-portfolio")

    def get_portfolio(self) -> Dict[str, Any]:
        return self.request("GET", "/api/v1/trading/info/demo/portfolio")

    def load_instrument_catalog(self) -> Dict[str, Dict[str, Any]]:
        data = self.request("GET", "/api/v1/market-data/instruments")
        items = data.get("instrumentDisplayDatas", []) if isinstance(data, dict) else []
        if not items and isinstance(data, dict):
            items = data.get("instruments", []) or data.get("items", []) or []

        result: Dict[str, Dict[str, Any]] = {}
        for item in items:
            symbol = normalize_symbol(value_from(item, "symbolFull", "internalSymbolFull", "symbol", default=""))
            instrument_id = value_from(item, "instrumentID", "instrumentId", default=None)
            if symbol and instrument_id is not None:
                result[symbol] = {
                    "symbol": symbol,
                    "instrument_id": int(instrument_id),
                    "raw": item,
                }
        self.catalog_by_symbol = result
        return result

    def resolve_instrument(self, asset: str) -> Optional[Dict[str, Any]]:
        asset = normalize_symbol(asset)
        if asset in self.instrument_cache:
            return self.instrument_cache[asset]
        if not self.catalog_by_symbol:
            self.load_instrument_catalog()

        candidates = ETORO_CANDIDATES.get(asset, [asset])
        expanded: List[str] = []
        for candidate in candidates:
            candidate = normalize_symbol(candidate)
            expanded.extend([candidate, candidate.replace("-", "."), candidate.replace(".", "-")])
        expanded = dedupe_preserve_order(expanded)

        for candidate in expanded:
            if candidate in self.catalog_by_symbol:
                found = dict(self.catalog_by_symbol[candidate])
                found["asset"] = asset
                self.instrument_cache[asset] = found
                return found

        # Fallback to eToro search for symbols not present / named differently
        # in the catalogue response.
        for candidate in expanded:
            try:
                data = self.request(
                    "GET",
                    "/api/v1/market-data/search",
                    params={"internalSymbolFull": candidate},
                )
            except Exception as error:
                print(f"eToro search warning for {asset}/{candidate}: {error}")
                continue

            for item in data.get("items", []) if isinstance(data, dict) else []:
                symbol = normalize_symbol(value_from(item, "internalSymbolFull", "symbolFull", "symbol", default=""))
                instrument_id = value_from(item, "instrumentId", "instrumentID", default=None)
                if instrument_id is None or not symbol:
                    continue
                if symbol == candidate or candidate in {symbol.replace("-", "."), symbol.replace(".", "-")}:
                    found = {"asset": asset, "symbol": symbol, "instrument_id": int(instrument_id), "raw": item}
                    self.instrument_cache[asset] = found
                    return found

        self.instrument_cache[asset] = None
        return None

    def get_eligibility(self, instrument_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
        unique_ids = sorted(set(int(x) for x in instrument_ids))
        result: Dict[int, Dict[str, Any]] = {}
        for start in range(0, len(unique_ids), 100):
            chunk = unique_ids[start:start + 100]
            if not chunk:
                continue
            data = self.request(
                "POST",
                "/api/v2/trading/info/demo/eligibility",
                payload={"instrumentIds": chunk, "symbols": [], "currency": "USD"},
            )
            for item in data.get("eligibilities", []) if isinstance(data, dict) else []:
                instrument_id = value_from(item, "instrumentId", "instrumentID", default=None)
                if instrument_id is not None:
                    result[int(instrument_id)] = item
        return result

    def get_history_since(self, start_local: datetime) -> List[Dict[str, Any]]:
        start_date = start_local.astimezone(LOCAL_TZ).date().isoformat()
        start_utc = start_local.astimezone(timezone.utc)
        rows: List[Dict[str, Any]] = []

        for page in range(1, MAX_HISTORY_PAGES + 1):
            data = self.request(
                "GET",
                "/api/v1/trading/info/trade/demo/history",
                params={"minDate": start_date, "page": page, "pageSize": HISTORY_PAGE_SIZE},
            )
            if isinstance(data, list):
                batch = data
            elif isinstance(data, dict):
                batch = data.get("items", []) or data.get("trades", []) or data.get("data", []) or []
            else:
                batch = []

            for trade in batch:
                closed_at = parse_utc_datetime(value_from(trade, "closeTimestamp", "closeDateTime", default=None))
                if closed_at is None or closed_at >= start_utc:
                    rows.append(trade)

            if len(batch) < HISTORY_PAGE_SIZE:
                break
        return rows

    # -------------------- execution writes --------------------

    def open_buy(self, instrument_id: int, amount_usd: float, settlement_type: str) -> Any:
        settlement = normalize_symbol(settlement_type)
        if settlement not in ALLOWED_SETTLEMENT_TYPES:
            raise RuntimeError(f"Unsupported settlement type for strategy: {settlement}")

        payload = {
            "action": "open",
            "transaction": "buy",
            "instrumentId": int(instrument_id),
            "settlementType": settlement.lower(),
            "orderType": "mkt",
            "leverage": REQUIRED_LEVERAGE,
            "amount": round(float(amount_usd), 2),
            "orderCurrency": "usd",
        }
        return self.request(
            "POST",
            "/api/v2/trading/execution/demo/orders",
            payload=payload,
            execution_write=True,
        )

    def close_position(self, position_id: int, instrument_id: int, units_to_deduct: Optional[float]) -> Any:
        payload = {
            "InstrumentID": int(instrument_id),
            "UnitsToDeduct": None if units_to_deduct is None else float(units_to_deduct),
        }
        return self.request(
            "POST",
            f"/api/v1/trading/execution/demo/market-close-orders/positions/{int(position_id)}",
            payload=payload,
            execution_write=True,
        )

    def cancel_order(self, order_id: int) -> Any:
        return self.request(
            "DELETE",
            f"/api/v2/trading/execution/demo/orders/{int(order_id)}",
            execution_write=True,
        )


# =============================================================================
# PORTFOLIO STATE
# =============================================================================


@dataclass
class PortfolioState:
    cid: int
    account_currency: str
    equity: float
    available_cash: float
    positions_by_instrument: Dict[int, List[Dict[str, Any]]]
    pending_open: Dict[int, List[Dict[str, Any]]]
    pending_close: Dict[int, List[Dict[str, Any]]]
    aggregate_values: Dict[int, float]
    aggregate_pnl: Dict[int, float]
    policy_violations: Dict[int, List[str]]
    raw_aggregate: Dict[str, Any]
    raw_portfolio: Dict[str, Any]

    @property
    def current_ids(self) -> set[int]:
        return set(self.positions_by_instrument)

    @property
    def pending_open_ids(self) -> set[int]:
        return set(self.pending_open)

    @property
    def pending_close_ids(self) -> set[int]:
        return set(self.pending_close)


def extract_pending_orders(client_portfolio: Dict[str, Any]) -> Tuple[Dict[int, List[Dict[str, Any]]], Dict[int, List[Dict[str, Any]]]]:
    pending_open: Dict[int, List[Dict[str, Any]]] = {}
    pending_close: Dict[int, List[Dict[str, Any]]] = {}
    seen_open = set()
    seen_close = set()

    def add_open(order: Dict[str, Any], source: str) -> None:
        instrument_id = value_from(order, "instrumentId", "instrumentID", default=None)
        if instrument_id is None:
            return
        instrument_id = int(instrument_id)
        order_id = value_from(order, "orderId", "orderID", default=None)
        unique_key = (source, order_id if order_id is not None else id(order))
        if unique_key in seen_open:
            return
        seen_open.add(unique_key)
        pending_open.setdefault(instrument_id, []).append({
            "source": source,
            "order_id": order_id,
            "amount": safe_float(value_from(order, "amount", default=0.0)),
            "raw": order,
        })

    def add_close(order: Dict[str, Any], source: str) -> None:
        instrument_id = value_from(order, "instrumentId", "instrumentID", default=None)
        if instrument_id is None:
            return
        instrument_id = int(instrument_id)
        order_id = value_from(order, "orderId", "orderID", default=None)
        unique_key = (source, order_id if order_id is not None else id(order))
        if unique_key in seen_close:
            return
        seen_close.add(unique_key)
        pending_close.setdefault(instrument_id, []).append({
            "source": source,
            "order_id": order_id,
            "position_id": value_from(order, "positionId", "positionID", default=None),
            "units_to_deduct": value_from(order, "unitsToDeduct", "UnitsToDeduct", default=None),
            "raw": order,
        })

    for source in ["ordersForOpen", "entryOrders", "delayedOrderForOpen"]:
        for order in client_portfolio.get(source, []) or []:
            add_open(order, source)

    for source in ["ordersForClose", "ordersForCloseMultiple", "exitOrders", "delayedOrderForClose"]:
        for order in client_portfolio.get(source, []) or []:
            add_close(order, source)

    for source in ["orders", "stockOrders"]:
        for order in client_portfolio.get(source, []) or []:
            is_buy = value_from(order, "isBuy", default=None)
            if is_buy is True:
                add_open(order, source)
            elif is_buy is False:
                add_close(order, source)

    return pending_open, pending_close


def read_portfolio_state(client: EtoroClient) -> PortfolioState:
    aggregate = client.get_aggregate()
    detailed = client.get_portfolio()
    cp = detailed.get("clientPortfolio", {}) if isinstance(detailed, dict) else {}
    pending_open, pending_close = extract_pending_orders(cp)

    positions_by_instrument: Dict[int, List[Dict[str, Any]]] = {}
    policy_violations: Dict[int, List[str]] = {}
    for position in cp.get("positions", []) or []:
        mirror_id = int(value_from(position, "mirrorID", "mirrorId", default=0) or 0)
        if mirror_id != 0:
            # Copy/mirror positions use a different ownership model and are not
            # expected in this dedicated automatic account. Leave them untouched.
            print(f"WARNING: mirror/copy position ignored (mirrorID={mirror_id}).")
            continue

        instrument_id = int(value_from(position, "instrumentID", "instrumentId"))
        is_buy = bool(value_from(position, "isBuy", default=True))
        leverage = safe_float(value_from(position, "leverage", default=1), 1.0)
        settlement_type_id = value_from(position, "settlementTypeID", "settlementTypeId", default=None)

        violations: List[str] = []
        if not is_buy:
            violations.append("short position")
        if abs(leverage - 1.0) > 1e-9:
            violations.append(f"leveraged x{leverage:g}")
        if settlement_type_id is not None and int(settlement_type_id) not in ALLOWED_SETTLEMENT_TYPE_IDS:
            violations.append(f"unsupported settlementTypeID={settlement_type_id}")
        if violations:
            policy_violations.setdefault(instrument_id, []).extend(violations)

        positions_by_instrument.setdefault(instrument_id, []).append(position)

    aggregate_values: Dict[int, float] = {}
    aggregate_pnl: Dict[int, float] = {}
    for item in aggregate.get("instrumentAggregates", []) or []:
        instrument_id = value_from(item, "instrumentId", "instrumentID", default=None)
        if instrument_id is None:
            continue
        instrument_id = int(instrument_id)
        aggregate_values[instrument_id] = safe_float(
            value_from(item, "liquidationValueAccountCurrency", "liquidationValueAcctCcy", default=0.0)
        )
        aggregate_pnl[instrument_id] = safe_float(
            value_from(item, "accountCurrencyReturn", "pnlAssetCurrency", default=0.0)
        )

    totals = aggregate.get("accountTotals", {}) or {}
    equity = safe_float(totals.get("accountTotalValue"), 0.0)
    available_cash = safe_float(totals.get("accountAvailableCash"), 0.0)
    if equity <= 0:
        equity = available_cash + sum(aggregate_values.values())

    cid = value_from(aggregate, "cid", "CID", default=None)
    if cid is None:
        # Positions also normally contain CID.
        cid = next(
            (
                value_from(p, "CID", "cid", default=None)
                for lots in positions_by_instrument.values()
                for p in lots
                if value_from(p, "CID", "cid", default=None) is not None
            ),
            0,
        )

    return PortfolioState(
        cid=int(cid or 0),
        account_currency=str(aggregate.get("accountCurrency", "UNKNOWN")),
        equity=equity,
        available_cash=available_cash,
        positions_by_instrument=positions_by_instrument,
        pending_open=pending_open,
        pending_close=pending_close,
        aggregate_values=aggregate_values,
        aggregate_pnl=aggregate_pnl,
        policy_violations=policy_violations,
        raw_aggregate=aggregate,
        raw_portfolio=detailed,
    )


def preferred_long_x1_config(eligibility: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    configs = eligibility.get("leverageConfigs", []) or []

    # Prefer REAL when available; otherwise allow CFD. LONG and x1 remain mandatory.
    for preferred_settlement in ALLOWED_SETTLEMENT_TYPES:
        for config in configs:
            settlement = normalize_symbol(config.get("settlementType", ""))
            direction = normalize_symbol(config.get("direction", ""))
            leverages = [safe_float(x, np.nan) for x in config.get("leverageValues", []) or []]
            if (
                settlement == preferred_settlement
                and direction == "LONG"
                and any(abs(x - REQUIRED_LEVERAGE) < 1e-9 for x in leverages if np.isfinite(x))
            ):
                return config
    return None


# =============================================================================
# TARGET SELECTION / ACTION PLANNING
# =============================================================================


@dataclass
class TargetAsset:
    asset: str
    instrument_id: int
    etoro_symbol: str
    momentum_return: float
    signal_week: pd.Timestamp
    min_position_amount: float


def resolve_universe_instruments(client: EtoroClient) -> Tuple[Dict[str, Dict[str, Any]], Dict[int, str]]:
    by_asset: Dict[str, Dict[str, Any]] = {}
    asset_by_id: Dict[int, str] = {}

    print_rule("RESOLVING ETORO INSTRUMENTS")
    for asset in UNIVERSE:
        try:
            resolved = client.resolve_instrument(asset)
        except Exception as error:
            print(f"{asset:<10} eToro resolution ERROR: {error}")
            continue
        if resolved is None:
            print(f"{asset:<10} NOT FOUND on eToro")
            continue
        instrument_id = int(resolved["instrument_id"])
        by_asset[asset] = resolved
        asset_by_id.setdefault(instrument_id, asset)
        print(f"{asset:<10} -> {resolved['symbol']:<14} id={instrument_id}")

    return by_asset, asset_by_id


def build_weekly_target(
    client: EtoroClient,
    signal: MomentumSignal,
    resolved_by_asset: Dict[str, Dict[str, Any]],
) -> Optional[TargetAsset]:
    """Return the single investable eToro target, or None for a valid CASH signal."""
    if signal.target_asset is None:
        return None

    asset = signal.target_asset
    resolved = resolved_by_asset.get(asset)
    if resolved is None:
        raise RuntimeError(f"Current strategy target {asset} could not be resolved to an eToro instrument.")

    instrument_id = int(resolved["instrument_id"])
    eligibility = client.get_eligibility([instrument_id]).get(instrument_id, {})
    trade_cfg = preferred_long_x1_config(eligibility)
    if trade_cfg is None:
        raise RuntimeError(
            f"Current strategy target {asset} has no REAL-or-CFD / LONG / x1 configuration on this account."
        )
    if not bool(eligibility.get("allowOpenPosition", False)):
        raise RuntimeError(f"eToro currently does not allow opening the strategy target {asset}.")

    min_amount = max(
        safe_float(eligibility.get("minPositionExposure"), 0.0),
        safe_float((trade_cfg or {}).get("minPositionAmount"), 0.0),
    )
    return TargetAsset(
        asset=asset,
        instrument_id=instrument_id,
        etoro_symbol=str(resolved["symbol"]),
        momentum_return=float(signal.returns_8w[asset]),
        signal_week=pd.Timestamp(signal.signal_week),
        min_position_amount=min_amount,
    )


def build_action_plan(
    portfolio: PortfolioState,
    asset_by_instrument_id: Dict[int, str],
    weekly_target: Optional[TargetAsset],
    strategy_active: bool,
    signal: MomentumSignal,
) -> pd.DataFrame:
    actions: List[Dict[str, Any]] = []
    full_close_ids: set[int] = set()
    cancelled_open_order_ids: set[int] = set()

    # 0) Automatic-account cleanup. This account is bot-owned, so anything
    # outside GLD / SLV / BTC is stale and should be removed.
    strategy_ids = set(asset_by_instrument_id)

    if CLEAN_NON_STRATEGY_POSITIONS:
        for instrument_id, orders in portfolio.pending_open.items():
            if instrument_id in strategy_ids:
                continue
            for order in orders:
                order_id = order.get("order_id")
                if order_id is None:
                    continue
                order_id = int(order_id)
                cancelled_open_order_ids.add(order_id)
                actions.append({
                    "Priority": 0, "Action": "CANCEL_OPEN", "Asset": f"ID:{instrument_id}",
                    "InstrumentID": instrument_id, "PositionID": None, "Units": None,
                    "Amount": None, "OrderID": order_id,
                    "Reason": "opening order is outside configured strategy universe; automatic cleanup",
                })

        for instrument_id in portfolio.current_ids - strategy_ids:
            if instrument_id in portfolio.pending_close_ids:
                actions.append({
                    "Priority": 10, "Action": "PENDING_CLOSE", "Asset": f"ID:{instrument_id}",
                    "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                    "Reason": "non-strategy holding already has a close pending; no duplicate",
                })
            else:
                actions.append({
                    "Priority": 1, "Action": "SELL_ALL_CLEANUP", "Asset": f"ID:{instrument_id}",
                    "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                    "Reason": "holding is outside configured strategy universe; automatic cleanup",
                })
                full_close_ids.add(instrument_id)

    if CLEAN_POLICY_VIOLATIONS:
        for instrument_id, reasons in portfolio.policy_violations.items():
            if instrument_id in full_close_ids:
                continue
            asset = asset_by_instrument_id.get(instrument_id, f"ID:{instrument_id}")
            if instrument_id in portfolio.pending_close_ids:
                actions.append({
                    "Priority": 10, "Action": "PENDING_CLOSE", "Asset": asset,
                    "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                    "Reason": "policy-violating holding already has a close pending; no duplicate",
                })
            else:
                actions.append({
                    "Priority": 1, "Action": "SELL_ALL_CLEANUP", "Asset": asset,
                    "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                    "Reason": "existing position violates REAL/LONG/x1 policy: " + "; ".join(sorted(set(reasons))),
                })
                full_close_ids.add(instrument_id)

    # If signal construction or eToro target validation failed, stop here. Cleanup
    # remains safe, but do not alter valid strategy holdings without a complete target.
    if not strategy_active:
        if not actions:
            return pd.DataFrame(columns=["Priority", "Action", "Asset", "InstrumentID", "PositionID", "Units", "Amount", "Reason"])
        plan = pd.DataFrame(actions)
        if "OrderID" not in plan.columns:
            plan["OrderID"] = np.nan
        return plan.sort_values(["Priority", "Asset"], na_position="last").reset_index(drop=True)

    target_ids = {weekly_target.instrument_id} if weekly_target is not None else set()

    # 1) Cancel every pending strategy open that is not the current weekly target.
    for instrument_id, orders in portfolio.pending_open.items():
        if instrument_id not in strategy_ids or instrument_id in target_ids:
            continue
        asset = asset_by_instrument_id.get(instrument_id, f"ID:{instrument_id}")
        for order in orders:
            order_id = order.get("order_id")
            if order_id is None:
                continue
            order_id = int(order_id)
            if order_id in cancelled_open_order_ids:
                continue
            cancelled_open_order_ids.add(order_id)
            actions.append({
                "Priority": 0, "Action": "CANCEL_OPEN", "Asset": asset,
                "InstrumentID": instrument_id, "PositionID": None, "Units": None,
                "Amount": None, "OrderID": order_id,
                "Reason": "pending opening order is not the current 8W momentum target",
            })

    # 2) Close all strategy holdings that are not the current target. When the
    # absolute-momentum test fails, target_ids is empty, so this moves fully to cash.
    for instrument_id in (portfolio.current_ids & strategy_ids) - target_ids:
        if instrument_id in full_close_ids:
            continue
        asset = asset_by_instrument_id.get(instrument_id, f"ID:{instrument_id}")
        if instrument_id in portfolio.pending_close_ids:
            actions.append({
                "Priority": 10, "Action": "PENDING_CLOSE", "Asset": asset,
                "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                "Reason": "weekly rotation exit already has a close pending; no duplicate",
            })
        else:
            actions.append({
                "Priority": 2, "Action": "SELL_ALL_REBALANCE", "Asset": asset,
                "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                "Reason": (
                    f"not current target for completed week {signal.signal_week.date()} "
                    f"(winner={signal.winner}, best 8W={signal.best_return:+.2%})"
                ),
            })
            full_close_ids.add(instrument_id)

    # 3) CASH is a complete valid target: exits above are all that is required.
    if weekly_target is None:
        actions.append({
            "Priority": 90, "Action": "HOLD_CASH", "Asset": "CASH",
            "InstrumentID": None, "PositionID": None, "Units": None, "Amount": None,
            "Reason": f"best 8W return is non-positive ({signal.winner} {signal.best_return:+.2%})",
        })
    else:
        instrument_id = weekly_target.instrument_id
        desired_exposure = max(0.0, round(portfolio.equity * TARGET_WEIGHT - CASH_RESERVE_USD, 2))

        # If a target position is being closed for policy cleanup, treat its current
        # exposure as zero so the replacement LONG/x1 order is sized after settlement.
        current_target_value = 0.0 if instrument_id in full_close_ids else max(
            0.0, portfolio.aggregate_values.get(instrument_id, 0.0)
        )
        topup_amount = max(0.0, round(desired_exposure - current_target_value, 2))

        if instrument_id in portfolio.pending_close_ids:
            actions.append({
                "Priority": 90, "Action": "PENDING_CLOSE", "Asset": weekly_target.asset,
                "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                "Reason": "current target has a close pending; wait for broker state to settle before re-entry",
            })
        elif instrument_id in portfolio.pending_open_ids:
            actions.append({
                "Priority": 90, "Action": "PENDING_OPEN", "Asset": weekly_target.asset,
                "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                "Reason": "current target already has an opening order pending; no duplicate",
            })
        elif topup_amount <= TOPUP_TOLERANCE_USD:
            actions.append({
                "Priority": 90, "Action": "HOLD_TARGET", "Asset": weekly_target.asset,
                "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                "Reason": "current target already occupies essentially all strategy equity",
            })
        elif topup_amount + 1e-9 < weekly_target.min_position_amount:
            actions.append({
                "Priority": 90, "Action": "HOLD_TARGET" if instrument_id in portfolio.current_ids else "SKIP_BUY",
                "Asset": weekly_target.asset,
                "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": topup_amount,
                "Reason": (
                    f"remaining target gap ${topup_amount:.2f} is below eToro minimum "
                    f"${weekly_target.min_position_amount:.2f}"
                ),
            })
        else:
            actions.append({
                "Priority": 5, "Action": "BUY_NEW", "Asset": weekly_target.asset,
                "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": topup_amount,
                "Reason": (
                    f"100% weekly target from {signal.signal_week.date()}; "
                    f"8W return={weekly_target.momentum_return:+.2%}"
                ),
            })

    if not actions:
        return pd.DataFrame(columns=["Priority", "Action", "Asset", "InstrumentID", "PositionID", "Units", "Amount", "Reason"])

    plan = pd.DataFrame(actions)
    if "OrderID" not in plan.columns:
        plan["OrderID"] = np.nan
    return plan.sort_values(["Priority", "Asset"], na_position="last").reset_index(drop=True)


# =============================================================================
# EXECUTION
# =============================================================================


def execute_plan(
    client: EtoroClient,
    portfolio: PortfolioState,
    plan: pd.DataFrame,
) -> None:
    if not EXECUTE_TRADES:
        print("\nDRY RUN: execution disabled; no eToro writes submitted.")
        return
    if plan.empty:
        print("\nNo eToro write actions required.")
        return

    writes = 0

    def before_write() -> None:
        nonlocal writes
        if writes >= MAX_WRITES_PER_RUN:
            raise RuntimeError(f"MAX_WRITES_PER_RUN={MAX_WRITES_PER_RUN} reached; remaining actions deferred.")

    def after_write() -> None:
        nonlocal writes
        writes += 1
        time.sleep(WRITE_DELAY_SECONDS)

    try:
        # A. Cancel stale non-target opening orders first.
        for _, row in plan[plan["Action"] == "CANCEL_OPEN"].iterrows():
            before_write()
            print(f"\nCANCEL STALE OPEN: {row['Asset']} order={int(row['OrderID'])}")
            response = client.cancel_order(int(row["OrderID"]))
            print(response)
            after_write()

        # B. Full exits (cleanup / weekly rotation, per Priority).
        # A successful close response can still create a PENDING broker order. Track
        # the affected instruments so replacement buys wait for actual settlement.
        full_rows = plan[plan["Action"].isin(["SELL_ALL_REBALANCE", "SELL_ALL_CLEANUP"])].sort_values("Priority")
        submitted_full_close_ids: set[int] = set()
        for _, row in full_rows.iterrows():
            instrument_id = int(row["InstrumentID"])
            lots = portfolio.positions_by_instrument.get(instrument_id, [])
            for position in lots:
                before_write()
                position_id = int(value_from(position, "positionID", "positionId"))
                print(f"\n{row['Action']}: {row['Asset']} position={position_id} FULL CLOSE")
                response = client.close_position(position_id, instrument_id, None)
                print(response)
                submitted_full_close_ids.add(instrument_id)
                after_write()

        # C. Settle full closes before replacement buys. A close-order response only
        # confirms that eToro accepted the instruction; the proceeds may still be tied
        # up in a pending close. If any close that matters to this rebalance is still
        # open/pending, wait briefly. If it does not settle, defer ALL new buys to the
        # next run instead of letting an earlier BUY_NEW consume the remaining cash.
        buy_rows = plan[plan["Action"] == "BUY_NEW"].copy()
        if not buy_rows.empty:
            close_dependency_ids = set(portfolio.pending_close_ids) | submitted_full_close_ids
            settled_state: Optional[PortfolioState] = None

            if close_dependency_ids:
                deadline = time.monotonic() + CLOSE_FILL_WAIT_SECONDS
                last_blocking_ids = set(close_dependency_ids)
                print(
                    f"\nWAIT FOR CLOSE SETTLEMENT: instruments {sorted(close_dependency_ids)}; "
                    f"up to {CLOSE_FILL_WAIT_SECONDS:.0f}s before replacement buys."
                )

                while True:
                    try:
                        candidate_state = read_portfolio_state(client)
                    except Exception as error:
                        print(f"Could not verify close settlement before buys: {error}")
                        settled_state = None
                        break

                    last_blocking_ids = {
                        instrument_id
                        for instrument_id in close_dependency_ids
                        if (
                            instrument_id in candidate_state.current_ids
                            or instrument_id in candidate_state.pending_close_ids
                        )
                    }

                    if not last_blocking_ids:
                        settled_state = candidate_state
                        # The position/pending-order endpoints can update just before
                        # accountAvailableCash. Take one fresh cash snapshot after the
                        # close is confirmed settled so replacement sizing uses the
                        # released proceeds rather than the immediately preceding value.
                        try:
                            settled_aggregate = client.get_aggregate()
                            settled_totals = settled_aggregate.get("accountTotals", {}) or {}
                            settled_state.available_cash = safe_float(
                                settled_totals.get("accountAvailableCash"),
                                settled_state.available_cash,
                            )
                        except Exception as error:
                            print(f"Cash refresh after close settlement failed: {error}")
                        print(
                            f"Close settlement confirmed. Available cash is "
                            f"${settled_state.available_cash:,.2f}."
                        )
                        break

                    if time.monotonic() >= deadline:
                        print(
                            f"DEFER NEW BUYS: close still pending/open for instrument IDs "
                            f"{sorted(last_blocking_ids)} after {CLOSE_FILL_WAIT_SECONDS:.0f}s. "
                            "The next run will retry after broker state settles."
                        )
                        settled_state = None
                        break

                    time.sleep(CLOSE_FILL_POLL_SECONDS)

                if settled_state is None:
                    buy_rows = buy_rows.iloc[0:0]

            if not buy_rows.empty:
                if settled_state is not None:
                    available_cash = settled_state.available_cash
                else:
                    try:
                        refreshed = client.get_aggregate()
                        totals = refreshed.get("accountTotals", {}) or {}
                        available_cash = safe_float(totals.get("accountAvailableCash"), portfolio.available_cash)
                    except Exception as error:
                        print(f"Could not refresh cash before buys; using initial snapshot: {error}")
                        available_cash = portfolio.available_cash

                # Current eligibility is checked again at execution time because market /
                # account availability can differ from the target-construction snapshot.
                buy_ids = [int(x) for x in buy_rows["InstrumentID"].tolist()]
                try:
                    eligibility_by_id = client.get_eligibility(buy_ids)
                except Exception as error:
                    print(f"BUY execution disabled this run: eligibility refresh failed: {error}")
                    eligibility_by_id = {}

                for _, row in buy_rows.iterrows():
                    instrument_id = int(row["InstrumentID"])
                    desired = round(float(row["Amount"]), 2)
                    eligibility = eligibility_by_id.get(instrument_id, {})
                    trade_cfg = preferred_long_x1_config(eligibility)

                    if not bool(eligibility.get("allowOpenPosition", False)):
                        print(f"\nSKIP BUY {row['Asset']}: eToro currently does not allow opening this position.")
                        continue
                    if trade_cfg is None:
                        print(f"\nSKIP BUY {row['Asset']}: no REAL-or-CFD / LONG / x1 configuration.")
                        continue

                    min_amount = max(
                        safe_float(eligibility.get("minPositionExposure"), 0.0),
                        safe_float((trade_cfg or {}).get("minPositionAmount"), 0.0),
                    )
                    usable_cash = max(0.0, available_cash - CASH_RESERVE_USD)

                    # Keep the normal equal-weight target whenever cash allows it. If
                    # fees/spreads or rounding leave the final slot slightly short, use
                    # the remaining usable cash instead of dropping that target.
                    spendable_cash = math.floor((usable_cash + 1e-9) * 100.0) / 100.0
                    buy_amount = min(desired, spendable_cash)

                    if buy_amount <= 0:
                        print(f"\nSKIP BUY {row['Asset']}: no usable cash remains after the ${CASH_RESERVE_USD:.2f} reserve.")
                        continue
                    if buy_amount + 1e-9 < min_amount:
                        print(
                            f"\nSKIP BUY {row['Asset']}: remaining usable cash ${buy_amount:.2f} "
                            f"is below eToro minimum ${min_amount:.2f}."
                        )
                        continue

                    settlement_type = normalize_symbol(trade_cfg.get("settlementType", ""))

                    if buy_amount + 1e-9 < desired:
                        print(
                            f"\nCASH-LIMITED BUY {row['Asset']}: planned ${desired:,.2f}, "
                            f"using remaining usable cash ${buy_amount:,.2f}."
                        )

                    before_write()
                    print(
                        f"\nBUY / TOP UP: {row['Asset']} ${buy_amount:,.2f} "
                        f"| {settlement_type} | LONG | x{REQUIRED_LEVERAGE:g}"
                    )
                    response = client.open_buy(instrument_id, buy_amount, settlement_type)
                    print(response)
                    available_cash -= buy_amount
                    after_write()

                    # Reconcile downward with the broker after each accepted buy.
                    try:
                        refreshed_after_buy = client.get_aggregate()
                        refreshed_totals = refreshed_after_buy.get("accountTotals", {}) or {}
                        broker_cash = safe_float(
                            refreshed_totals.get("accountAvailableCash"),
                            available_cash,
                        )
                        available_cash = min(available_cash, broker_cash)
                    except Exception as error:
                        print(f"Cash refresh after {row['Asset']} buy failed; using conservative local cash: {error}")

    except UnknownExecutionStateError as error:
        print_rule("EXECUTION HALTED: UNKNOWN BROKER WRITE RESULT")
        print(error)
        print(
            "No additional writes will be submitted in this run. The next run will "
            "re-read positions and pending orders from eToro before acting."
        )
        return
    except Exception as error:
        print_rule("EXECUTION ERROR")
        print(f"{type(error).__name__}: {error}")
        traceback.print_exc()
        print("Remaining actions are deferred to the next run.")
        return

    print_rule("EXECUTION COMPLETE")
    print(f"eToro writes submitted this run: {writes}")
    print("The bot does not assume that HTTP 200 means an order has filled; the next run re-reads broker state.")


# =============================================================================
# REPORTING
# =============================================================================


def print_signal(signal: MomentumSignal) -> None:
    print_rule(f"REL_ABS_GOLD_SILVER_BTC_{MOMENTUM_WEEKS}W SIGNAL")
    rows = []
    for asset in STRATEGY_ASSETS:
        rows.append({
            "Asset": asset,
            "StartWeek": signal.start_week.date(),
            "StartClose": signal.start_closes[asset],
            "SignalWeek": signal.signal_week.date(),
            "LatestClose": signal.latest_closes[asset],
            "8WReturn": signal.returns_8w[asset],
            "Winner": "YES" if asset == signal.winner else "",
        })
    view = pd.DataFrame(rows)
    view["StartClose"] = view["StartClose"].map(lambda x: f"{x:.6g}")
    view["LatestClose"] = view["LatestClose"].map(lambda x: f"{x:.6g}")
    view["8WReturn"] = view["8WReturn"].map(lambda x: f"{x:+.2%}")
    print(view.to_string(index=False))
    print()
    if signal.target_asset is None:
        print(f"TARGET: CASH | best return = {signal.winner} {signal.best_return:+.2%} <= 0")
    else:
        print(f"TARGET: {signal.target_asset} at 100% | best 8W return = {signal.best_return:+.2%}")


def print_target(target: Optional[TargetAsset], signal: MomentumSignal, strategy_active: bool) -> None:
    print_rule("ETORO WEEKLY TARGET")
    if not strategy_active:
        print("Strategy writes are disabled this run because the signal/target could not be validated safely.")
        return
    if target is None:
        print(f"CASH target from signal week {signal.signal_week.date()}.")
        return
    print(
        f"{target.asset} -> {target.etoro_symbol} | instrumentID={target.instrument_id} | "
        f"8W={target.momentum_return:+.2%} | min position=${target.min_position_amount:,.2f}"
    )


def print_action_plan(plan: pd.DataFrame) -> None:
    print_rule("ACTION PLAN")
    if plan.empty:
        print("No action required.")
        return

    view = plan.copy()
    if "Units" in view:
        view["Units"] = view["Units"].map(lambda x: f"{x:.10f}" if pd.notna(x) else "")
    if "Amount" in view:
        view["Amount"] = view["Amount"].map(lambda x: f"${x:,.2f}" if pd.notna(x) else "")
    columns = [c for c in ["Action", "Asset", "InstrumentID", "PositionID", "Units", "Amount", "Reason"] if c in view.columns]
    print(view[columns].to_string(index=False))


# =============================================================================
# MAIN
# =============================================================================


def validate_credentials() -> Tuple[str, str]:
    api_key = ETORO_API_KEY.strip() or os.getenv("ETORO_API_KEY", "").strip()
    user_key = ETORO_USER_KEY.strip() or os.getenv("ETORO_USER_KEY", "").strip()
    if not api_key or not user_key:
        raise RuntimeError(
            "Missing eToro DEMO credentials. Paste ETORO_API_KEY and ETORO_USER_KEY "
            "into the CONFIG section near the top of this file."
        )
    return api_key, user_key


def main() -> None:
    if DEPENDENCY_IMPORT_ERROR is not None:
        raise RuntimeError(
            "Missing/failed Python dependency. Install with: "
            "pip install requests pandas numpy yfinance. "
            f"Original import error: {DEPENDENCY_IMPORT_ERROR}"
        )

    now_local = datetime.now(LOCAL_TZ)

    print_rule("ETORO DEMO — REL_ABS_GOLD_SILVER_BTC_8W")
    print(f"Local time:                 {now_local:%Y-%m-%d %H:%M:%S %Z}")
    print(f"Strategy assets:            {', '.join(STRATEGY_ASSETS)}")
    print(f"Momentum horizon:           {MOMENTUM_WEEKS} completed Friday-ending weeks")
    print("Allocation rule:            100% strongest positive 8W asset; otherwise CASH")
    print(f"Execution enabled:          {EXECUTE_TRADES}")
    print(f"Cash reserve:               ${CASH_RESERVE_USD:.2f}")
    print("Separate SMA entry/stop:    None")

    api_key, user_key = validate_credentials()
    client = EtoroClient(api_key, user_key)

    market_data, resolved_yahoo, download_failures = download_universe()

    signal: Optional[MomentumSignal] = None
    signal_error: Optional[Exception] = None
    try:
        signal = build_8w_signal(market_data, now_local)
        print_signal(signal)
    except Exception as error:
        signal_error = error
        print_rule("WEEKLY SIGNAL ERROR")
        print(f"{type(error).__name__}: {error}")
        traceback.print_exc()
        print("Strategy rotation writes will be disabled; automatic account/policy cleanup can still run.")

    portfolio = read_portfolio_state(client)
    if portfolio.account_currency.upper() != "USD":
        raise RuntimeError(f"Demo account currency is {portfolio.account_currency}, not USD.")

    client.load_instrument_catalog()
    resolved_by_asset, asset_by_instrument_id = resolve_universe_instruments(client)

    if len(asset_by_instrument_id) != len({int(v["instrument_id"]) for v in resolved_by_asset.values()}):
        print("WARNING: multiple configured symbols map to the same eToro instrument; strategy writes will be disabled.")
        signal_error = RuntimeError("duplicate eToro instrument mapping in strategy universe")

    unknown_current = portfolio.current_ids - set(asset_by_instrument_id)
    if unknown_current:
        print_rule("AUTOMATIC PORTFOLIO CLEANUP")
        print("Non-strategy instrument IDs currently held:", sorted(unknown_current))
        print("They will be fully closed unless a close is already pending.")
    if portfolio.policy_violations:
        print_rule("EXISTING POSITION POLICY CLEANUP")
        for instrument_id, reasons in sorted(portfolio.policy_violations.items()):
            print(f"ID:{instrument_id} -> {', '.join(sorted(set(reasons)))}")
        print("These positions will be fully closed; new buys remain REAL / LONG / x1 only.")

    print_rule("ETORO ACCOUNT SNAPSHOT")
    print(f"CID:                         {portfolio.cid}")
    print(f"Equity:                      ${portfolio.equity:,.2f}")
    print(f"Available cash:              ${portfolio.available_cash:,.2f}")
    print(f"Direct instruments held:     {len(portfolio.current_ids)}")
    print(f"Pending opening instruments: {len(portfolio.pending_open_ids)}")
    print(f"Pending closing instruments: {len(portfolio.pending_close_ids)}")

    weekly_target: Optional[TargetAsset] = None
    strategy_active = signal is not None and signal_error is None
    if strategy_active and signal is not None:
        try:
            weekly_target = build_weekly_target(
                client=client,
                signal=signal,
                resolved_by_asset=resolved_by_asset,
            )
        except Exception as error:
            print_rule("ETORO TARGET ERROR")
            print(f"{type(error).__name__}: {error}")
            traceback.print_exc()
            strategy_active = False

    # A cash signal legitimately has weekly_target=None while strategy_active=True.
    if signal is not None:
        print_target(weekly_target, signal, strategy_active)

    # build_action_plan needs a signal object only for explanatory text. If signal
    # construction failed, make a harmless placeholder; strategy_active=False means
    # no strategy membership writes will be created from it.
    plan_signal = signal or MomentumSignal(
        signal_week=pd.Timestamp(now_local.date()),
        start_week=pd.Timestamp(now_local.date()),
        returns_8w={asset: np.nan for asset in STRATEGY_ASSETS},
        start_closes={asset: np.nan for asset in STRATEGY_ASSETS},
        latest_closes={asset: np.nan for asset in STRATEGY_ASSETS},
        winner="N/A",
        best_return=np.nan,
        target_asset=None,
    )

    plan = build_action_plan(
        portfolio=portfolio,
        asset_by_instrument_id=asset_by_instrument_id,
        weekly_target=weekly_target,
        strategy_active=strategy_active,
        signal=plan_signal,
    )
    print_action_plan(plan)

    if download_failures:
        print_rule("MARKET DATA WARNINGS")
        for asset, error in download_failures.items():
            print(f"{asset:<10} {error}")
        print("All three strategy assets are required for a valid weekly signal.")

    execute_plan(client, portfolio, plan)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print_rule("FATAL ERROR")
        print(f"{type(error).__name__}: {error}")
        traceback.print_exc()
    finally:
        if KEEP_WINDOW_OPEN:
            wait_for_keypress()