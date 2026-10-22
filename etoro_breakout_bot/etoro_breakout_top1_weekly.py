"""
eToro DEMO — TOP-1 WEEKLY 50/20 BREAKOUT + SMA50 EXIT
=======================================================
Only portfolio-strategy selection was changed from the user's original eToro bot:
- Hold the highest-ranked ONE qualifying instrument with almost 100% allocation.
- Each completed Friday-ending candle determines the NEXT week's selection.
- Entry into persistent eligibility: weekly close > previous 50-week high.
- Rank eligible instruments by their 26-week close-to-close return.
- Exit if weekly close < previous 20-week low OR weekly close < SMA50.
- Switch to the top eligible instrument at the Monday bot run; cash if none.
- If the incumbent remains the winner, HOLD without topping up.
- A stopped position can re-enter only after a fresh weekly breakout.

Unchanged: watchlist/proxy mappings; Yahoo downloads; DEMO account endpoints,
API retries and write limits; settlement handling; cash reserve; REAL/CFD LONG x1
checks; order reconciliation; pending-order safeguards; no external database;
Monday execution schedule. The Monday eToro market order can fill at prices
DIFFERENT from the backtest's next-weekday close.

Safety: Original EXECUTE_TRADES=True is preserved. Set it to FALSE for dry-run
before connecting demo credentials; running it otherwise can place DEMO orders.
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

TARGET_POSITIONS = 1
NEW_POSITION_WEIGHT = 1.0 / TARGET_POSITIONS  # 100% top-one position
# For a concentrated portfolio, avoid rotation on major Yahoo outages.
MIN_UNIVERSE_DATA_COVERAGE = 0.85

BREAKOUT_ENTRY_WEEKS = 50
BREAKOUT_EXIT_WEEKS = 20
BREAKOUT_RANK_WEEKS = 26
STOP_SMA_WEEKS = 50

CASH_RESERVE_USD = 5.00
MAX_WRITES_PER_RUN = 18  # below eToro's shared 20 writes / 60 sec quota
WRITE_DELAY_SECONDS = 3.2
CLOSE_FILL_WAIT_SECONDS = 45.0
CLOSE_FILL_POLL_SECONDS = 2.0
REQUEST_TIMEOUT = 30
READ_RETRIES = 4
HISTORY_PAGE_SIZE = 200
MAX_HISTORY_PAGES = 10

DOWNLOAD_PERIOD = "6y"  # unchanged data window; sufficient for 50W breakout and 26W ranking
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
# EDIT ONLY THIS LIST LATER WHEN YOU PROVIDE THE FINAL UNIVERSE.
#
# This is the active universe from your second script, deduplicated, with the
# old index/metal symbols and unresolved eToro symbols replaced by tradeable proxies/primary listings:
#   SPX      -> SPY
#   IXIC     -> QQQ
#   NI225    -> XDJP.L
#   HSI      -> 2800.HK
#   SILVER   -> SLV
#   GC1!     -> GLD
#   XPDUSD   -> PALL
#   PLATINUM -> PPLT
#   XLES     -> XLE        (US energy-sector ETF)
#   4COP     -> COPA.L     (second copper exposure; COPX already present)
#   URNU     -> U3O8.DE    (uranium miners ETF)
#   SC0V     -> SXEPEX.DE  (Europe oil & gas ETF)
#   XEG      -> IXC        (global energy ETF)
#   TCEHY    -> 0700.HK    (Tencent primary listing)
#   LRLCY    -> OR.PA      (L'Oreal primary listing)
#   SIEGY    -> SIE.DE     (Siemens primary listing)
#   NTDOF    -> 7974.T     (Nintendo primary listing)
#   VOLVF    -> VOLV-B.ST  (Volvo B primary listing)
#   VWAGY    -> VOW.DE     (Volkswagen ordinary shares)
#   HINKF    -> HEIA.NV    (Heineken primary listing)
#   MKTAY    -> SWK        (Stanley Black & Decker; power-tools peer)
#   LNVGF    -> 0992.HK    (Lenovo primary listing)
# -----------------------------------------------------------------------------
RAW_UNIVERSE = [
    # Crypto
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOT", "LINK",

    # Broad index / physical-metal ETF proxies
    "SPY", "QQQ", "XDJP.L", "2800.HK",
    "SLV", "GLD", "PALL", "PPLT", "EWH", "FXI",

    # Existing thematic / resource funds, using eToro-listed substitutes where needed
    "XLE", "COPX", "PSLV", "URA", "COPA.L", "SIL", "U3O8.DE", "SXEPEX.DE", "IXC",

    # Stocks from the second script (duplicates BABA / CCJ removed)
    "PHG", "TM", "AAPL", "META", "NFLX", "KO", "NESM", "PEP", "BABA", "MCD",
    "ADBE", "SHOP", "NKE", "SPOT", "0700.HK", "OR.PA", "1810", "SIE.DE", "INTC",
    "IBKR", "DELL", "MDLZ", "7974.T", "ADSK", "VOLV-B.ST", "VOW.DE", "GRMN", "HEIA.NV",
    "NVDA", "MCHP", "HPQ", "EBAY", "PUM", "MGA", "ZBRA", "AMZN", "MSFT", "SMSN",
    "AMD", "ADS", "TSM", "MSI", "RIO", "CCJ", "GOOGL", "JNJ", "SAP", "CSCO",
    "DIS", "TXN", "SND", "BHP", "SWK", "0992.HK", "PAAS", "WPM", "AEM", "NEM", "B", "FCX", "ALB", "XOM", "FRES.L", "AG", "01211.HK",
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


def week_regime_start(now_local: datetime) -> datetime:
    """Current Monday 00:00 in Europe/Ljubljana; same snapshot all week."""
    return monday_of_week(now_local)


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

    required = ["Open", "High", "Low", "Close"]
    for column in required:
        if column not in d.columns:
            raise RuntimeError(f"{column} column missing")

    out = d[required].copy()
    for column in required:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out = out.replace([np.inf, -np.inf], np.nan).dropna(subset=required)
    out = out[(out[required] > 0).all(axis=1)]
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


def weekly_features_before(frame: pd.DataFrame, cutoff_local: datetime) -> Optional[Dict[str, Any]]:
    """
    Strictly completed Friday-ending candles only; no lookahead into partial
    weeks. State machine reconstructs the breakout's persistent eligibility.
    """
    cutoff_naive = pd.Timestamp(cutoff_local.astimezone(LOCAL_TZ).replace(tzinfo=None))
    d = frame.loc[frame.index < cutoff_naive].copy()
    if d.empty:
        return None

    weekly = d.resample("W-FRI").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last"
    }).dropna(subset=["Open", "High", "Low", "Close"])
    # The resampler might label a partial Fri-ending candle in the future;
    # eliminate it even if some Mon-Thu observations were downloaded.
    weekly = weekly.loc[weekly.index < cutoff_naive]
    if len(weekly) < max(BREAKOUT_ENTRY_WEEKS + 1, BREAKOUT_RANK_WEEKS + 1):
        return None

    weekly["Prior50High"] = weekly["High"].shift(1).rolling(
        BREAKOUT_ENTRY_WEEKS, min_periods=BREAKOUT_ENTRY_WEEKS
    ).max()
    weekly["Prior20Low"] = weekly["Low"].shift(1).rolling(
        BREAKOUT_EXIT_WEEKS, min_periods=BREAKOUT_EXIT_WEEKS
    ).min()
    weekly["SMA50"] = weekly["Close"].rolling(
        STOP_SMA_WEEKS, min_periods=STOP_SMA_WEEKS
    ).mean()
    weekly["Momentum26"] = weekly["Close"] / weekly["Close"].shift(BREAKOUT_RANK_WEEKS) - 1.0

    # Entry is the *event* close > previous 50-week high. After that, the
    # position stays eligible until the first exit; no fresh breakout needed.
    active = False
    prior_entry = False
    for _, candle in weekly.iterrows():
        close = candle["Close"]
        entry = bool(pd.notna(candle["Prior50High"]) and close > candle["Prior50High"])
        exit_20 = bool(pd.notna(candle["Prior20Low"]) and close < candle["Prior20Low"])
        exit_ma = bool(pd.notna(candle["SMA50"]) and close < candle["SMA50"])
        if active:
            if exit_20 or exit_ma:
                active = False
        elif entry:
            active = True
        prior_entry = entry

    row = weekly.iloc[-1]
    close = safe_float(row["Close"], np.nan)
    sma50 = safe_float(row["SMA50"], np.nan)
    low20 = safe_float(row["Prior20Low"], np.nan)
    exit_20 = bool(np.isfinite(close) and np.isfinite(low20) and close < low20)
    exit_ma = bool(np.isfinite(close) and np.isfinite(sma50) and close < sma50)
    return {
        "WeekEnd": weekly.index[-1],
        "Open": safe_float(row["Open"], np.nan),
        "High": safe_float(row["High"], np.nan),
        "Low": safe_float(row["Low"], np.nan),
        "Close": close,
        "SMA50": sma50,
        "Prior50High": safe_float(row["Prior50High"], np.nan),
        "Prior20Low": low20,
        "Momentum26": safe_float(row["Momentum26"], np.nan),
        "BreakoutEvent": prior_entry,
        "BreakoutLong": active,
        "Exit20": exit_20,
        "ExitMA50": exit_ma,
    }


def build_breakout_ranking(
    market_data: Dict[str, pd.DataFrame],
    week_start_local: datetime,
) -> pd.DataFrame:
    """Reconstruct a deterministic signal from the most recent FINISHED week."""
    rows: List[Dict[str, Any]] = []
    for asset, frame in market_data.items():
        features = weekly_features_before(frame, week_start_local)
        if not features:
            continue
        # Exact prior Friday, not a stale weekly candle. If Yahoo has no
        # activity this past week, it cannot be our new position.
        expected_last_friday = pd.Timestamp(week_start_local.replace(tzinfo=None)) - pd.Timedelta(days=3)
        if pd.Timestamp(features["WeekEnd"]) != expected_last_friday:
            continue
        momentum = features["Momentum26"]
        if not np.isfinite(momentum):
            continue
        rows.append({
            "Asset": asset,
            "Momentum26": momentum,
            "SignalWeek": features["WeekEnd"],
        })
    if not rows:
        raise RuntimeError("No assets have sufficient COMPLETED weekly data for 26W ranking.")
    ranking = pd.DataFrame(rows).sort_values(
        ["Momentum26", "Asset"], ascending=[False, True]
    ).reset_index(drop=True)
    ranking.insert(0, "Rank", np.arange(1, len(ranking) + 1))
    return ranking


def entry_trend_pass(features: Optional[Dict[str, Any]]) -> bool:
    """Persistent 50-week breakout LONG state, not a 50/200 SMA filter."""
    return bool(features is not None and features.get("BreakoutLong", False))


def stop_triggered(features: Optional[Dict[str, Any]]) -> bool:
    """Either report's 20W low exit OR additional weekly close < SMA50."""
    return bool(features is not None and (
        features.get("Exit20", False) or features.get("ExitMA50", False)
    ))



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
    rank: int
    asset: str
    instrument_id: int
    etoro_symbol: str
    momentum_26w: float
    week_end: pd.Timestamp
    weekly_close: float
    sma50: float
    prior_50w_high: float
    prior_20w_low: float
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
        # If aliases accidentally map two configured assets to one instrument,
        # keep the first and reject the duplicate during target selection.
        asset_by_id.setdefault(instrument_id, asset)
        print(f"{asset:<10} -> {resolved['symbol']:<14} id={instrument_id}")

    return by_asset, asset_by_id


def build_weekly_targets(
    client: EtoroClient,
    ranking: pd.DataFrame,
    market_data: Dict[str, pd.DataFrame],
    week_start_local: datetime,
    resolved_by_asset: Dict[str, Dict[str, Any]],
) -> Tuple[List[TargetAsset], pd.DataFrame]:
    # Weekly Top-1 target from the most recent COMPLETED Friday candle.

    trend_rows: List[Dict[str, Any]] = []
    trend_passing_ids: List[int] = []
    row_cache: List[Tuple[pd.Series, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]] = []

    for _, row in ranking.iterrows():
        asset = str(row["Asset"])
        features = weekly_features_before(market_data[asset], week_start_local) if asset in market_data else None
        resolved = resolved_by_asset.get(asset)
        trend_ok = entry_trend_pass(features)
        instrument_id = int(resolved["instrument_id"]) if resolved else None
        if trend_ok and instrument_id is not None:
            trend_passing_ids.append(instrument_id)
        row_cache.append((row, features, resolved))

    eligibility_by_id = client.get_eligibility(trend_passing_ids) if trend_passing_ids else {}

    selected: List[TargetAsset] = []
    selected_ids: set[int] = set()

    for row, features, resolved in row_cache:
        asset = str(row["Asset"])
        reason = ""
        trend_ok = entry_trend_pass(features)
        if not trend_ok:
            reason = "FAIL persistent 50W breakout eligibility"
        elif resolved is None:
            reason = "PASS trend, but no eToro instrument"
        else:
            instrument_id = int(resolved["instrument_id"])
            eligibility = eligibility_by_id.get(instrument_id, {})
            trade_cfg = preferred_long_x1_config(eligibility)
            if instrument_id in selected_ids:
                reason = "duplicate eToro instrument mapping"
            elif trade_cfg is None:
                reason = "no REAL-or-CFD / LONG / x1 configuration on this account"
            else:
                min_amount = max(
                    safe_float(eligibility.get("minPositionExposure"), 0.0),
                    safe_float((trade_cfg or {}).get("minPositionAmount"), 0.0),
                )
                selected.append(TargetAsset(
                    rank=int(row["Rank"]),
                    asset=asset,
                    instrument_id=instrument_id,
                    etoro_symbol=str(resolved["symbol"]),
                    momentum_26w=float(row["Momentum26"]),
                    week_end=pd.Timestamp(features["WeekEnd"]),
                    weekly_close=float(features["Close"]),
                    sma50=float(features["SMA50"]),
                    prior_50w_high=float(features["Prior50High"]),
                    prior_20w_low=float(features["Prior20Low"]),
                    min_position_amount=min_amount,
                ))
                selected_ids.add(instrument_id)
                reason = f"SELECTED #{len(selected)}"

        trend_rows.append({
            "Rank": int(row["Rank"]),
            "Asset": asset,
            "Momentum26_%": float(row["Momentum26"]) * 100,
            "WeekEnd": features.get("WeekEnd") if features else pd.NaT,
            "Close": features.get("Close") if features else np.nan,
            "SMA50": features.get("SMA50") if features else np.nan,
            "Prior50High": features.get("Prior50High") if features else np.nan,
            "Prior20Low": features.get("Prior20Low") if features else np.nan,
            "BreakoutLong": features.get("BreakoutLong") if features else False,
            "Decision": reason,
        })

        if len(selected) >= TARGET_POSITIONS:
            break

    diagnostics = pd.DataFrame(trend_rows)
    return selected, diagnostics


def stop_blocked_instruments_since(
    history: List[Dict[str, Any]],
    week_start_local: datetime,
    market_data: Dict[str, pd.DataFrame],
    asset_by_instrument_id: Dict[int, str],
) -> set[int]:
    """
    Reconstruct whether a stopped position has already exited this week.

    eToro history tells us that a position was closed, but not reliably *why*.
    Blocking every close causes false BLOCK_REENTRY events after a manual close,
    cleanup, or other non-stop exit.  Instead, for each historical close we
    reconstruct the latest completed weekly candle that was available at that
    close time.  The instrument is blocked only when that candle satisfied the
    strategy's full-exit rule: Close < prior 20-week LOW OR Close < SMA50.

    This preserves no-database idempotency for real stop exits while allowing
    non-stop/manual closes of a current weekly target to be restored.
    """
    week_start_utc = week_start_local.astimezone(timezone.utc)
    blocked: set[int] = set()

    for trade in history:
        instrument_id_raw = value_from(trade, "instrumentId", "instrumentID", default=None)
        if instrument_id_raw is None:
            continue
        instrument_id = int(instrument_id_raw)
        asset = asset_by_instrument_id.get(instrument_id)
        if not asset or asset not in market_data:
            continue

        closed_at_utc = parse_utc_datetime(
            value_from(trade, "closeTimestamp", "closeDateTime", default=None)
        )
        if closed_at_utc is None or closed_at_utc < week_start_utc:
            continue

        closed_local = closed_at_utc.astimezone(LOCAL_TZ)
        completed_week_cutoff = monday_of_week(closed_local)
        features = weekly_features_before(market_data[asset], completed_week_cutoff)
        if stop_triggered(features):
            blocked.add(instrument_id)

    return blocked


def build_action_plan(
    client: EtoroClient,
    portfolio: PortfolioState,
    market_data: Dict[str, pd.DataFrame],
    asset_by_instrument_id: Dict[int, str],
    resolved_by_asset: Dict[str, Dict[str, Any]],
    weekly_targets: Optional[List[TargetAsset]],
    weekly_active: bool,
    week_start_local: datetime,
    now_local: datetime,
) -> pd.DataFrame:
    actions: List[Dict[str, Any]] = []
    full_close_ids: set[int] = set()
    cancelled_open_order_ids: set[int] = set()

    # 0) Automatic-account cleanup. This account is bot-owned, so anything
    # outside the configured strategy universe is stale and should be removed.
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

    current_monday = monday_of_week(now_local)
    weekly_features: Dict[str, Optional[Dict[str, Any]]] = {
        asset: weekly_features_before(frame, current_monday)
        for asset, frame in market_data.items()
    }

    # 1) Weekly 20-week LOW / SMA50 exits take precedence over other actions.
    for instrument_id, lots in portfolio.positions_by_instrument.items():
        if instrument_id in full_close_ids:
            continue
        asset = asset_by_instrument_id.get(instrument_id)
        if not asset:
            continue
        features = weekly_features.get(asset)
        if (features and pd.Timestamp(features['WeekEnd']) ==
                pd.Timestamp(current_monday.replace(tzinfo=None)) - pd.Timedelta(days=3)
                and stop_triggered(features)):
            if instrument_id in portfolio.pending_close_ids:
                actions.append({
                    "Priority": 10, "Action": "PENDING_CLOSE", "Asset": asset,
                    "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                    "Reason": "20W low or SMA50 exit triggered; close already pending, no duplicate",
                })
            else:
                actions.append({
                    "Priority": 1, "Action": "SELL_ALL_STOP", "Asset": asset,
                    "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                    "Reason": (
                        f"completed week {features['WeekEnd'].date()}: "
                        f"Close {features['Close']:.4g}; prev20WLow={features['Prior20Low']:.4g}; "
                        f"SMA50={features['SMA50']:.4g}; "
                        f"20W_exit={features['Exit20']} MA50_exit={features['ExitMA50']}"
                    ),
                })
                full_close_ids.add(instrument_id)

    # 2) WEEKLY Top-1 rotation; target is deterministic/frozen until next Monday.
    target_ids = {t.instrument_id for t in weekly_targets or []}
    target_by_id = {t.instrument_id: t for t in weekly_targets or []}

    if weekly_active and weekly_targets is not None:
        try:
            history = client.get_history_since(week_start_local)
            stop_blocked_this_week = stop_blocked_instruments_since(
                history=history,
                week_start_local=week_start_local,
                market_data=market_data,
                asset_by_instrument_id=asset_by_instrument_id,
            )
        except Exception as error:
            # Without history we cannot safely reconstruct whether a missing
            # current target was genuinely stopped out earlier in this week.
            print(f"WARNING: weekly trade history unavailable; NEW weekly buys are disabled this run: {error}")
            history = []
            stop_blocked_this_week = set()
            allow_weekly_buys = False
        else:
            allow_weekly_buys = True

        # Cancel stale pending opens that are no longer in the frozen target.
        for instrument_id, orders in portfolio.pending_open.items():
            if instrument_id in target_ids:
                continue
            asset = asset_by_instrument_id.get(instrument_id, f"ID:{instrument_id}")
            for order in orders:
                order_id = order.get("order_id")
                if order_id is not None:
                    order_id = int(order_id)
                    if order_id in cancelled_open_order_ids:
                        continue
                    cancelled_open_order_ids.add(order_id)
                    actions.append({
                        "Priority": 0, "Action": "CANCEL_OPEN", "Asset": asset,
                        "InstrumentID": instrument_id, "PositionID": None, "Units": None,
                        "Amount": None, "OrderID": order_id,
                        "Reason": "pending opening order is not in this week's frozen target",
                    })

        # Sell holdings that are not in this week's Top-1 target.
        for instrument_id in portfolio.current_ids - target_ids:
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
                    "Reason": "not in this week's Top-1 target",
                })
                full_close_ids.add(instrument_id)

        # Aim for one winner at ~100% equity. The original seven-slot bot had
        # "never top up" behavior, but that would leave just ~1/7 invested if
        # the new Top-1 winner was an existing position. A single additional
        # BUY_NEW lot can fill the deficit AFTER the other holdings settle.
        desired_amount = round(portfolio.equity * NEW_POSITION_WEIGHT, 2)
        for target in weekly_targets:
            instrument_id = target.instrument_id
            if instrument_id in portfolio.pending_open_ids:
                actions.append({
                    "Priority": 90, "Action": "PENDING_OPEN", "Asset": target.asset,
                    "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                    "Reason": "opening order already pending; no duplicate",
                })
                continue
            if instrument_id in portfolio.current_ids:
                # Only top up with a trustworthy broker exposure estimate.
                holdings_value = getattr(portfolio, 'aggregate_values', {})
                if instrument_id not in holdings_value:
                    actions.append({
                        "Priority": 90, "Action": "HOLD_TARGET", "Asset": target.asset,
                        "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                        "Reason": "existing winner; broker exposure unavailable, top-up deferred for safety",
                    })
                    continue
                existing_value = max(0.0, safe_float(holdings_value[instrument_id]))
                additional_amount = round(max(0.0, desired_amount - existing_value), 2)
                # Prevent repeated trivial orders to chase price fluctuations.
                minimum_top_up = max(float(target.min_position_amount),
                                     0.01 * max(portfolio.equity, 0.0))
                if additional_amount + 1e-9 < minimum_top_up:
                    actions.append({
                        "Priority": 90, "Action": "HOLD_TARGET", "Asset": target.asset,
                        "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                        "Reason": "incumbent remains winner; already near full exposure",
                    })
                    continue
                actions.append({
                    "Priority": 5, "Action": "BUY_NEW", "Asset": target.asset,
                    "InstrumentID": instrument_id, "PositionID": None, "Units": None,
                    "Amount": additional_amount,
                    "Reason": "Top-1 allocation correction for existing winner; "
                              f"current ${existing_value:,.2f}, additional target ${additional_amount:,.2f}",
                })
                continue
            if instrument_id in portfolio.pending_close_ids:
                actions.append({
                    "Priority": 90, "Action": "PENDING_CLOSE", "Asset": target.asset,
                    "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                    "Reason": "close pending for target; wait for broker state to settle",
                })
                continue
            if not allow_weekly_buys:
                actions.append({
                    "Priority": 90, "Action": "SKIP_BUY", "Asset": target.asset,
                    "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": desired_amount,
                    "Reason": "trade history unavailable; duplicate-safe re-entry check could not run",
                })
                continue
            if instrument_id in stop_blocked_this_week:
                actions.append({
                    "Priority": 90, "Action": "BLOCK_REENTRY", "Asset": target.asset,
                    "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": desired_amount,
                    "Reason": "historical close matches 20W-low or SMA50 exit; do not re-enter until next week",
                })
                continue
            if desired_amount + 1e-9 < target.min_position_amount:
                actions.append({
                    "Priority": 90, "Action": "SKIP_BUY", "Asset": target.asset,
                    "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": desired_amount,
                    "Reason": f"{NEW_POSITION_WEIGHT:.2%} slot ${desired_amount:.2f} < eToro minimum ${target.min_position_amount:.2f}",
                })
                continue
            actions.append({
                "Priority": 5, "Action": "BUY_NEW", "Asset": target.asset,
                "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": desired_amount,
                "Reason": f"new weekly Top-1 target rank #{target.rank}; initial slot = {NEW_POSITION_WEIGHT:.2%} of equity",
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

        # B. Full exits (stop before rebalance, per Priority).
        # A successful close response can still create a PENDING broker order. Track
        # the affected instruments so replacement buys wait for actual settlement.
        full_rows = plan[plan["Action"].isin(["SELL_ALL_STOP", "SELL_ALL_REBALANCE", "SELL_ALL_CLEANUP"])].sort_values("Priority")
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
                # account availability can differ from the weekly target-construction snapshot.
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
                        f"\nBUY NEW: {row['Asset']} ${buy_amount:,.2f} "
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


def print_breakout_table(
    ranking: pd.DataFrame,
    market_data: Dict[str, pd.DataFrame],
    week_start_local: datetime,
    rows: int = 25,
) -> None:
    print_rule(f"TOP {min(rows, len(ranking))} — COMPLETED 26-WEEK MOMENTUM / 50-20 BREAKOUT")
    view = ranking.head(rows).copy()
    status = []
    for asset in view["Asset"].astype(str):
        features = weekly_features_before(market_data[asset], week_start_local)
        status.append("YES" if entry_trend_pass(features) else "NO")
    view["BreakoutLong"] = status
    view["Momentum26"] = view["Momentum26"].map(lambda x: f"{x:+.2%}")
    print(view[["Rank", "Asset", "Momentum26", "SignalWeek", "BreakoutLong"]].to_string(index=False))


def print_target_diagnostics(diagnostics: pd.DataFrame, targets: List[TargetAsset]) -> None:
    print_rule("LATEST FINISHED WEEK: 26W RANK / BREAKOUT ELIGIBILITY")
    if diagnostics.empty:
        print("No diagnostics available.")
    else:
        view = diagnostics.copy()
        view["Momentum26_%"] = view["Momentum26_%"].map(lambda x: f"{x:+.2f}%")
        for column in ["Close", "SMA50", "Prior50High", "Prior20Low"]:
            view[column] = view[column].map(lambda x: f"{x:.4g}" if pd.notna(x) else "")
        print(view.to_string(index=False))

    print_rule(f"WEEKLY TOP-{TARGET_POSITIONS} TARGET")
    if not targets:
        print("No eligible target: CASH (if market-data and broker checks pass).")
        return
    target_df = pd.DataFrame([
        {
            "Rank": t.rank,
            "Asset": t.asset,
            "eToro": t.etoro_symbol,
            "InstrumentID": t.instrument_id,
            "Momentum26": f"{t.momentum_26w:+.2%}",
            "WeekEnd": t.week_end.date(),
            "Close": f"{t.weekly_close:.4g}",
            "SMA50": f"{t.sma50:.4g}",
            "Prior50High": f"{t.prior_50w_high:.4g}",
            "Prior20Low": f"{t.prior_20w_low:.4g}",
        }
        for t in targets
    ])
    print(target_df.to_string(index=False))


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
    week_start_local = week_regime_start(now_local)
    weekly_active = True

    print_rule("ETORO DEMO — WEEKLY 50/20 BREAKOUT + SMA50 STOP BOT")
    print(f"Local time:                 {now_local:%Y-%m-%d %H:%M:%S %Z}")
    print(f"Universe size (deduped):    {len(UNIVERSE)}")
    print(f"Weekly signal cutoff:       {week_start_local:%Y-%m-%d %H:%M %Z}")
    print("Weekly Top-1 rotation:      True (same target across reruns in the week)")
    print(f"Execution enabled:          {EXECUTE_TRADES}")
    print(f"Target holdings:            {TARGET_POSITIONS} x {NEW_POSITION_WEIGHT:.2%} on NEW entry")
    print(f"Entry:                      weekly Close > prior {BREAKOUT_ENTRY_WEEKS}W high (persistent)")
    print(f"Ranking:                    {BREAKOUT_RANK_WEEKS}W close-to-close return")
    print(f"Weekly exit:                Close < prior {BREAKOUT_EXIT_WEEKS}W low OR SMA{STOP_SMA_WEEKS}W")
    print("NOTE: Top-1 weekly rules; eToro DEMO market-order fills and its available assets may differ from the backtest.")

    api_key, user_key = validate_credentials()
    client = EtoroClient(api_key, user_key)

    # Data failures are isolated asset-by-asset; the bot still handles positions
    # for which valid data exists.
    market_data, resolved_yahoo, download_failures = download_universe()
    ranking = build_breakout_ranking(market_data, week_start_local)
    print_breakout_table(ranking, market_data, week_start_local)
    # An incomplete cross-asset ranking can select the wrong concentrated winner.
    # Stops and non-strategy cleanup remain active even when rotation is deferred.
    fresh_count = sum(
        bool(f and pd.Timestamp(f['WeekEnd']) == (
            pd.Timestamp(week_start_local.replace(tzinfo=None)) - pd.Timedelta(days=3)))
        for frame in market_data.values()
        for f in [weekly_features_before(frame, week_start_local)]
    )
    data_coverage = fresh_count / max(1, len(UNIVERSE))
    selection_data_safe = data_coverage >= MIN_UNIVERSE_DATA_COVERAGE
    print(f"Weekly signal coverage:     {fresh_count}/{len(UNIVERSE)} ({data_coverage:.1%}); "
          f"required {MIN_UNIVERSE_DATA_COVERAGE:.0%}")
    if not selection_data_safe:
        print("WARNING: too many unavailable/stale instruments; Top-1 rotation disabled for this run.")

    portfolio = read_portfolio_state(client)
    if portfolio.account_currency.upper() != "USD":
        raise RuntimeError(f"Demo account currency is {portfolio.account_currency}, not USD.")

    client.load_instrument_catalog()
    resolved_by_asset, asset_by_instrument_id = resolve_universe_instruments(client)

    # Detect accidentally duplicated eToro IDs in the configured universe.
    if len(asset_by_instrument_id) != len({int(v["instrument_id"]) for v in resolved_by_asset.values()}):
        print("WARNING: multiple configured symbols map to the same eToro instrument; duplicate target IDs are rejected.")

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

    expected_friday = pd.Timestamp(week_start_local.replace(tzinfo=None)) - pd.Timedelta(days=3)
    held_unpriced_assets = []
    for inst in portfolio.current_ids:
        asset = asset_by_instrument_id.get(inst)
        if asset is None:  # original cleanup rules handle unknown broker IDs
            continue
        if asset not in market_data:
            held_unpriced_assets.append(asset)
            continue
        features = weekly_features_before(market_data[asset], week_start_local)
        if not features or pd.Timestamp(features['WeekEnd']) != expected_friday:
            held_unpriced_assets.append(asset)
    if held_unpriced_assets:
        selection_data_safe = False
        print("WARNING: cannot evaluate currently held assets "
              f"{held_unpriced_assets}; weekly target rotation suspended.")

    print_rule("ETORO ACCOUNT SNAPSHOT")
    print(f"CID:                         {portfolio.cid}")
    print(f"Equity:                      ${portfolio.equity:,.2f}")
    print(f"Available cash:              ${portfolio.available_cash:,.2f}")
    print(f"Direct instruments held:     {len(portfolio.current_ids)}")
    print(f"Pending opening instruments: {len(portfolio.pending_open_ids)}")
    print(f"Pending closing instruments: {len(portfolio.pending_close_ids)}")

    weekly_targets: Optional[List[TargetAsset]] = None
    diagnostics = pd.DataFrame()

    if weekly_active and selection_data_safe:
        try:
            weekly_targets, diagnostics = build_weekly_targets(
                client=client,
                ranking=ranking,
                market_data=market_data,
                week_start_local=week_start_local,
                resolved_by_asset=resolved_by_asset,
            )
            print_target_diagnostics(diagnostics, weekly_targets)
            if len(weekly_targets) == 0 and not diagnostics.empty and bool(diagnostics['BreakoutLong'].any()):
                # There WERE valid breakout signals, but their assets are not
                # executable (symbol, permission, or minimum position issue).
                # Do not misinterpret an execution failure as a CASH signal.
                weekly_targets = None
                print("Weekly rotation deferred: breakout candidates exist but no broker-tradable target could be selected.")
            elif len(weekly_targets) > TARGET_POSITIONS:
                raise RuntimeError("More than one selected target: invalid Top-1 state")
        except Exception as error:
            print_rule("WEEKLY TARGET ERROR")
            print(f"{type(error).__name__}: {error}")
            traceback.print_exc()
            weekly_targets = None
            print("Weekly membership changes disabled; weekly risk management will still be planned.")
    plan = build_action_plan(
        client=client,
        portfolio=portfolio,
        market_data=market_data,
        asset_by_instrument_id=asset_by_instrument_id,
        resolved_by_asset=resolved_by_asset,
        weekly_targets=weekly_targets,
        weekly_active=(weekly_active and weekly_targets is not None),
        week_start_local=week_start_local,
        now_local=now_local,
    )
    print_action_plan(plan)

    if download_failures:
        print_rule("MARKET DATA WARNINGS")
        for asset, error in download_failures.items():
            print(f"{asset:<10} {error}")
        print("Assets without sufficient data cannot enter the ranking; a held asset without weekly data will not be auto-stopped.")

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