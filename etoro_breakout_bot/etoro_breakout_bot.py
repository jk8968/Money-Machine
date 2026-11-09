"""
eToro DEMO 50/20 WEEKLY BREAKOUT — TOP 3 / 52W MOMENTUM / SMA30 EXIT
========================================================================

Same eToro DEMO API, cash and portfolio-state framework as the provided bot.
Only strategy-dependent signals, target construction, allocation and reporting
are changed to match 52W TOP3 NO_FILTER STOP_SMA30 in the rolling backtest.

Run once per week, preferably Monday (Europe/Ljubljana), after the preceding
Friday-ending weekly candle is complete. Signals are rebuilt from completed
market history; no state file/database is used.

Strategy:
  - Build Friday-ending weekly OHLC (Open=first, High=max, Low=min,
    Close=last daily bar).
  - INACTIVE -> ELIGIBLE when weekly Close is above the highest weekly High
    of the PREVIOUS 50 completed weeks. No entry SMA filter.
  - ELIGIBLE -> INACTIVE when weekly Close is below the lowest weekly Low of
    the PREVIOUS 20 weeks, OR both that weekly Open and Close are below
    that week's 30-week SMA of weekly closes. Require a NEW breakout to reenter.
  - Rank eligible assets by Close / Close 52 weeks prior - 1.
  - Hold up to Top 3 broker-tradable assets, equal target weights. If only
    one or two qualify, split the invested capital equally between them.
    No eligible targets -> hold cash.
  - Check weekly membership; rotate old names, keep unchanged holdings,
    top up underweights when targets change or material cash sits idle.
    Never continually trim positions merely because market values drift.
  - Long / x1; prefer REAL, permit CFD as in the original demo bot.

Existing API endpoints, credential handling, automatic-account cleanup,
pending-order guards, close-settlement waits, rate limits and execution
methods are preserved. Real eToro market fills, availability and costs
can differ from the rolling backtest. Partial sales of retained overweights
are not introduced; if weights drift past equal targets, exact matching is
not possible until those holdings rotate out.

This bot assumes the demo account is dedicated to the strategy and cleans
up non-strategy direct holdings exactly as in the original version.
Credentials: enter DEMO API keys or set environment variables.
Dependencies: pip install requests pandas numpy yfinance
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

TARGET_POSITIONS = 3
NEW_POSITION_WEIGHT = 1.0 / TARGET_POSITIONS   # split across three when available
BREAKOUT_HIGH_WEEKS = 50
EXIT_LOW_WEEKS = 20
MOMENTUM_WEEKS = 52
STOP_SMA_WEEKS = 30  # additional exit; both completed-week open and close below SMA

CASH_RESERVE_USD = 5.00
MAX_WRITES_PER_RUN = 18  # below eToro's shared 20 writes / 60 sec quota
WRITE_DELAY_SECONDS = 3.2
CLOSE_FILL_WAIT_SECONDS = 45.0
CLOSE_FILL_POLL_SECONDS = 2.0
REQUEST_TIMEOUT = 30
READ_RETRIES = 4
HISTORY_PAGE_SIZE = 200
MAX_HISTORY_PAGES = 10

DOWNLOAD_PERIOD = "max"  # historical state machine needs past breakout/exit signals
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


def most_recent_completed_friday(now_local: datetime) -> pd.Timestamp:
    """Latest finished Friday, never including today's unfinished daily bar."""
    # Monday..Thursday -> preceding Friday; Saturday/Sunday -> the Friday just
    # passed. On Friday itself its daily candle may be unfinished, so go back.
    weekday = now_local.weekday()
    days_ago = (weekday - 4) % 7
    if days_ago == 0:
        days_ago = 7
    return pd.Timestamp((now_local - timedelta(days=days_ago)).date())


def completed_weekly_bars(frame: pd.DataFrame, now_local: datetime) -> pd.DataFrame:
    """Friday-ending OHLC without the current partial week; no future data."""
    friday = most_recent_completed_friday(now_local)
    daily = frame.loc[frame.index <= friday]
    if daily.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    weekly = daily.resample("W-FRI").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last",
    }).dropna()
    return weekly.loc[weekly.index <= friday]


def weekly_features_before(frame: pd.DataFrame, now_local: datetime) -> Optional[Dict[str, Any]]:
    """Rebuild 50/20 plus SMA30-exit eligibility from finished weekly bars.

    A new breakout is required after any exit; SMA30 is never an entry filter.
    The open/close SMA30 stop matches the rolling backtest's completed-week rule.
    """
    weekly = completed_weekly_bars(frame, now_local)
    minimum_weeks = max(BREAKOUT_HIGH_WEEKS + 1, MOMENTUM_WEEKS + 1, STOP_SMA_WEEKS)
    if len(weekly) < minimum_weeks:
        return None
    # An old/delisted price series must not compete with current tradable names.
    if weekly.index[-1] < most_recent_completed_friday(now_local) - pd.Timedelta(days=14):
        return None

    closes = weekly["Close"]
    opens = weekly["Open"]
    prior_high = weekly["High"].shift(1).rolling(
        BREAKOUT_HIGH_WEEKS, min_periods=BREAKOUT_HIGH_WEEKS
    ).max()
    prior_low = weekly["Low"].shift(1).rolling(
        EXIT_LOW_WEEKS, min_periods=EXIT_LOW_WEEKS
    ).min()
    stop_ma = closes.rolling(STOP_SMA_WEEKS, min_periods=STOP_SMA_WEEKS).mean()
    momentum = closes / closes.shift(MOMENTUM_WEEKS) - 1.0

    enters = closes > prior_high  # NO_FILTER entry: breakout only
    low_exits = closes < prior_low
    sma_exits = (opens < stop_ma) & (closes < stop_ma)
    exits = low_exits | sma_exits

    eligible = False
    latest_breakout = False
    latest_exit = False
    for entry, exit_ in zip(enters.to_numpy(dtype=bool), exits.to_numpy(dtype=bool)):
        latest_breakout = False
        latest_exit = False
        if not eligible and entry:
            eligible = True
            latest_breakout = True
        elif eligible and exit_:
            eligible = False
            latest_exit = True

    def finite(series: pd.Series) -> float:
        value = safe_float(series.iloc[-1], np.nan)
        return float(value) if np.isfinite(value) else float('nan')

    return {
        "WeekEnd": weekly.index[-1],
        "Open": finite(opens),
        "High": finite(weekly["High"]),
        "Low": finite(weekly["Low"]),
        "Close": finite(closes),
        "Prior50High": finite(prior_high),
        "Prior20Low": finite(prior_low),
        "SMA30": finite(stop_ma),
        "Momentum52": finite(momentum),
        "Eligible": bool(eligible),
        "NewBreakout": bool(latest_breakout),
        "ExitThisWeek": bool(latest_exit),
        "Stop20LowNow": bool(low_exits.iloc[-1]),
        "StopSMA30Now": bool(sma_exits.iloc[-1]),
        "StopNow": bool(exits.iloc[-1]),
        "WeeklyBars": len(weekly),
    }

def build_weekly_ranking(
    market_data: Dict[str, pd.DataFrame], now_local: datetime,
) -> Tuple[pd.DataFrame, Dict[str, Dict[str, Any]]]:
    """Rank eligible assets by 52W momentum with unchanged stable tie handling."""
    rows = []
    features_by_asset: Dict[str, Dict[str, Any]] = {}
    for asset, frame in market_data.items():
        features = weekly_features_before(frame, now_local)
        if features is None:
            continue
        features_by_asset[asset] = features
        r52 = features["Momentum52"]
        if not np.isfinite(r52):
            continue
        rows.append({
            "Asset": asset, "Momentum52": r52,
            "Eligible": features["Eligible"],
            "WeekEnd": features["WeekEnd"],
            "Open": features["Open"],
            "Close": features["Close"],
            "Prior50High": features["Prior50High"],
            "Prior20Low": features["Prior20Low"],
            "SMA30": features["SMA30"],
            "NewBreakout": features["NewBreakout"],
            "ExitThisWeek": features["ExitThisWeek"],
        })
    if not rows:
        raise RuntimeError("No assets have enough completed weekly OHLC for the 50/20 breakout.")
    ranking = pd.DataFrame(rows).sort_values(
        ["Eligible", "Momentum52", "Asset"], ascending=[False, False, True]
    ).reset_index(drop=True)
    ranking.insert(0, "Rank", np.arange(1, len(ranking) + 1))
    return ranking, features_by_asset

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
    momentum_score: float
    week_end: pd.Timestamp
    weekly_close: float
    prior50_high: float
    prior20_low: float
    sma30: float
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
    resolved_by_asset: Dict[str, Dict[str, Any]],
) -> Tuple[List[TargetAsset], pd.DataFrame]:
    """Select up to Top 3 statefully eligible, eToro-tradable names by 52W return.

    Skip unsupported instruments, as the old bot did. Zero eligible names is a
    valid CASH target; 1 or 2 available targets use 1/N equal target weights.
    """
    eligible_rows = ranking.loc[ranking["Eligible"]].copy()
    potential_ids = [
        int(resolved_by_asset[a]["instrument_id"])
        for a in eligible_rows["Asset"] if a in resolved_by_asset
    ]
    eligibility_by_id = client.get_eligibility(potential_ids) if potential_ids else {}
    selected: List[TargetAsset] = []
    selected_ids = set()
    decisions = {}

    for _, row in eligible_rows.iterrows():
        asset = str(row["Asset"])
        resolved = resolved_by_asset.get(asset)
        if not resolved:
            decisions[asset] = "No resolved eToro instrument"
            continue
        instrument_id = int(resolved["instrument_id"])
        if instrument_id in selected_ids:
            decisions[asset] = "Duplicate eToro instrument"
            continue
        eligibility = eligibility_by_id.get(instrument_id, {})
        trade_cfg = preferred_long_x1_config(eligibility)
        if trade_cfg is None:
            decisions[asset] = "No REAL/CFD LONG x1 eligibility"
            continue
        min_amount = max(
            safe_float(eligibility.get("minPositionExposure"), 0.0),
            safe_float(trade_cfg.get("minPositionAmount"), 0.0),
        )
        selected.append(TargetAsset(
            rank=int(row["Rank"]), asset=asset,
            instrument_id=instrument_id, etoro_symbol=str(resolved["symbol"]),
            momentum_score=float(row["Momentum52"]),
            week_end=pd.Timestamp(row["WeekEnd"]),
            weekly_close=float(row["Close"]),
            prior50_high=float(row["Prior50High"]),
            prior20_low=float(row["Prior20Low"]),
            sma30=float(row["SMA30"]), min_position_amount=min_amount,
        ))
        selected_ids.add(instrument_id)
        decisions[asset] = f"SELECTED TOP {TARGET_POSITIONS}"
        if len(selected) >= TARGET_POSITIONS:
            break

    diagnostics = ranking.copy()
    diagnostics["Decision"] = diagnostics.apply(
        lambda r: decisions.get(r["Asset"], "Not selected / not in eligible 50/20 breakout state"), axis=1
    )
    return selected, diagnostics

def build_action_plan(
    client: EtoroClient,
    portfolio: PortfolioState,
    market_data: Dict[str, pd.DataFrame],
    asset_by_instrument_id: Dict[int, str],
    resolved_by_asset: Dict[str, Dict[str, Any]],
    weekly_targets: Optional[List[TargetAsset]],
    selection_active: bool,
    now_local: datetime,
) -> pd.DataFrame:
    actions: List[Dict[str, Any]] = []
    full_close_ids: set[int] = set()
    cancelled_open_order_ids: set[int] = set()
    strategy_ids = set(asset_by_instrument_id)

    # 0) Automatic-account cleanup / policy handling: original bot framework.
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

    # 1) Completed-week 20-low OR weekly OPEN+CLOSE below SMA30 stop.
    # Verify stops even if target selection fails. A fresh 50W breakout is
    # required before a stopped-out asset becomes eligible again.
    weekly_features: Dict[str, Optional[Dict[str, Any]]] = {
        asset: weekly_features_before(frame, now_local)
        for asset, frame in market_data.items()
    }
    for instrument_id in portfolio.current_ids:
        if instrument_id in full_close_ids:
            continue
        asset = asset_by_instrument_id.get(instrument_id)
        if not asset:
            continue
        features = weekly_features.get(asset)
        if features is None or not bool(features["StopNow"]):
            continue
        reasons = []
        if features["Stop20LowNow"]:
            reasons.append(
                f"Close {features['Close']:.4g} < previous 20W Low {features['Prior20Low']:.4g}"
            )
        if features["StopSMA30Now"]:
            reasons.append(
                f"weekly Open {features['Open']:.4g} and Close {features['Close']:.4g} "
                f"both < SMA30 {features['SMA30']:.4g}"
            )
        if instrument_id in portfolio.pending_close_ids:
            actions.append({
                "Priority": 10, "Action": "PENDING_CLOSE", "Asset": asset,
                "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                "Reason": "weekly breakout/SMA30 stop exit already pending; no duplicate close",
            })
        else:
            actions.append({
                "Priority": 1, "Action": "SELL_ALL_STOP", "Asset": asset,
                "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                "Reason": f"completed week {features['WeekEnd'].date()}: " + " OR ".join(reasons),
            })
            full_close_ids.add(instrument_id)

    # 2) Weekly Top 3 / cash reconciliation based on last completed Friday.
    target_ids = {t.instrument_id for t in weekly_targets or []}
    if selection_active and weekly_targets is not None:
        for instrument_id, orders in portfolio.pending_open.items():
            if instrument_id in target_ids:
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
                    "Reason": f"pending opening order is not this week's Top {TARGET_POSITIONS} target",
                })

        for instrument_id in portfolio.current_ids - target_ids:
            if instrument_id in full_close_ids:
                continue
            asset = asset_by_instrument_id.get(instrument_id, f"ID:{instrument_id}")
            if instrument_id in portfolio.pending_close_ids:
                actions.append({
                    "Priority": 10, "Action": "PENDING_CLOSE", "Asset": asset,
                    "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                    "Reason": "weekly exit/rotation close already pending",
                })
            else:
                actions.append({
                    "Priority": 2, "Action": "SELL_ALL_REBALANCE", "Asset": asset,
                    "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                    "Reason": "not in this week's highest-52W-momentum eligible Top 3",
                })
                full_close_ids.add(instrument_id)

        if weekly_targets:
            # Match backtest's 1 / available N gross target, even for 1 or 2
            # eligible assets. Do not continually trade unchanged targets simply
            # because holdings drift. Refresh when membership changes or idle
            # cash exceeds 1% (same cash-management intent as original bot).
            n_targets = len(weekly_targets)
            target_dollars = portfolio.equity / n_targets
            membership_changed = portfolio.current_ids != target_ids
            cash_fraction = portfolio.available_cash / portfolio.equity if portfolio.equity > 0 else 0.0
            should_allocate = membership_changed or cash_fraction > 0.01

            # Estimate cash after scheduled exits, including already-pending
            # closes. A migration from the old 100%-in-one-stock strategy must
            # free capital to buy the other two targets. If existing *retained*
            # winners are overweight enough to block required Top 3 entries,
            # recycle them with the EXISTING full-close-and-buy workflow.
            # Unchanged member sets never trigger this sizing rebalance.
            released_ids = full_close_ids | (portfolio.pending_close_ids - target_ids)
            expected_releases = sum(
                max(0.0, safe_float(portfolio.aggregate_values.get(i), 0.0))
                for i in released_ids
            )
            budget = max(0.0, portfolio.available_cash + expected_releases - CASH_RESERVE_USD)
            recycle_ids = set()
            if should_allocate and membership_changed:
                shortfalls = 0.0
                overweight_targets = []
                for target in weekly_targets:
                    instrument_id = target.instrument_id
                    if instrument_id in portfolio.pending_open_ids or instrument_id in portfolio.pending_close_ids:
                        continue
                    if instrument_id in full_close_ids:
                        continue
                    held = instrument_id in portfolio.current_ids
                    current_value = (safe_float(portfolio.aggregate_values.get(instrument_id), np.nan)
                                     if held else 0.0)
                    if not np.isfinite(current_value) or current_value < 0:
                        continue
                    shortfalls += max(0.0, target_dollars - current_value)
                    if held and current_value > target_dollars + 0.01:
                        overweight_targets.append((current_value - target_dollars, target, current_value))

                for excess, target, current_value in sorted(overweight_targets, key=lambda x: -x[0]):
                    if shortfalls <= budget + 0.01:
                        break
                    instrument_id = target.instrument_id
                    # Closing then reopening a retained overweight position is
                    # needed only when rotation cannot otherwise fund equal
                    # targets; no new API action is introduced.
                    recycle_ids.add(instrument_id)
                    full_close_ids.add(instrument_id)
                    actions.append({
                        "Priority": 2, "Action": "SELL_ALL_REBALANCE", "Asset": target.asset,
                        "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                        "Reason": (
                            f"release ${excess:,.2f} of excess target exposure via full-close/reopen "
                            f"to fund 1/{n_targets} equal weights after membership change"
                        ),
                    })
                    budget += current_value
                    shortfalls += target_dollars  # repurchase the recycled target

            # Divide available proceeds across requested underweight purchases
            # rather than letting the first name exhaust the cash before the
            # remaining Top 3 candidates are processed.
            valid_buys = []
            desired_by_id = {}
            for target in weekly_targets:
                instrument_id = target.instrument_id
                held = instrument_id in portfolio.current_ids and instrument_id not in recycle_ids
                current_value = safe_float(portfolio.aggregate_values.get(instrument_id), np.nan) if held else 0.0
                if instrument_id in portfolio.pending_open_ids:
                    actions.append({
                        "Priority": 90, "Action": "PENDING_OPEN", "Asset": target.asset,
                        "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                        "Reason": "target opening order already pending",
                    })
                    continue
                if instrument_id in portfolio.pending_close_ids or (
                    instrument_id in full_close_ids and instrument_id not in recycle_ids
                ):
                    actions.append({
                        "Priority": 90, "Action": "PENDING_CLOSE", "Asset": target.asset,
                        "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                        "Reason": "target has pending/required close; wait for broker settlement",
                    })
                    continue
                if not should_allocate:
                    actions.append({
                        "Priority": 90, "Action": "HOLD_TARGET", "Asset": target.asset,
                        "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                        "Reason": "Top 3 unchanged; preserve drifted weights",
                    })
                    continue
                if held and (not np.isfinite(current_value) or current_value <= 0):
                    actions.append({
                        "Priority": 90, "Action": "SKIP_BUY", "Asset": target.asset,
                        "InstrumentID": instrument_id, "PositionID": None, "Units": None, "Amount": None,
                        "Reason": "cannot verify value of existing target; no blind top-up",
                    })
                    continue
                desired_by_id[instrument_id] = round(max(0.0, target_dollars - current_value), 2)
                valid_buys.append(target)

            total_desired = sum(desired_by_id.values())
            scale = min(1.0, budget / total_desired) if total_desired > 0 else 0.0
            for target in valid_buys:
                desired = round(desired_by_id[target.instrument_id] * scale, 2)
                if desired <= 0:
                    actions.append({
                        "Priority": 90, "Action": "HOLD_TARGET", "Asset": target.asset,
                        "InstrumentID": target.instrument_id, "PositionID": None, "Units": None, "Amount": None,
                        "Reason": "target held / no available cash for additional purchase",
                    })
                elif desired + 1e-9 < target.min_position_amount:
                    actions.append({
                        "Priority": 90, "Action": "SKIP_BUY", "Asset": target.asset,
                        "InstrumentID": target.instrument_id, "PositionID": None, "Units": None, "Amount": desired,
                        "Reason": f"planned top-up below broker minimum ${target.min_position_amount:.2f}",
                    })
                else:
                    actions.append({
                        "Priority": 5, "Action": "BUY_NEW", "Asset": target.asset,
                        "InstrumentID": target.instrument_id, "PositionID": None, "Units": None,
                        "Amount": desired,
                        "Reason": (
                            f"weekly Top {TARGET_POSITIONS} / 52W momentum {target.momentum_score:+.2%}; "
                            f"equal target = 1/{n_targets} of equity; "
                            + ("reopen retained overweight target after settlement" if target.instrument_id in recycle_ids
                               else "top up retained target" if target.instrument_id in portfolio.current_ids
                               else "new breakout winner")
                        ),
                    })
        # No weekly targets -> close unwanted positions above, leave proceeds as cash.

    if not actions:
        return pd.DataFrame(columns=[
            "Priority", "Action", "Asset", "InstrumentID", "PositionID", "Units", "Amount", "Reason", "OrderID"
        ])
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


def print_momentum_table(ranking: pd.DataFrame, rows: int = 25) -> None:
    print_rule(f"TOP {min(rows, len(ranking))} — ELIGIBLE 52-WEEK MOMENTUM")
    cols = ["Rank", "Asset", "Momentum52", "Eligible", "WeekEnd", "Close", "Prior50High", "Prior20Low", "SMA30"]
    view = ranking.head(rows)[cols].copy()
    view["Momentum52"] = view["Momentum52"].map(lambda x: f"{x:+.2%}")
    print(view.to_string(index=False))


def print_target_diagnostics(diagnostics: pd.DataFrame, targets: List[TargetAsset]) -> None:
    print_rule("WEEKLY 50/20 BREAKOUT + SMA30 EXIT / 52W RANK")
    if not diagnostics.empty:
        view = diagnostics.head(35).copy()
        view["Momentum52"] = view["Momentum52"].map(lambda x: f"{x:+.2%}")
        columns = ["Rank", "Asset", "Momentum52", "Eligible", "NewBreakout", "ExitThisWeek", "WeekEnd", "Decision"]
        print(view[columns].to_string(index=False))
    print_rule(f"LATEST COMPLETED WEEKLY TOP {TARGET_POSITIONS} TARGETS")
    if not targets:
        print("No eligible, broker-tradable asset: strategy target is 100% CASH.")
    else:
        for i, t in enumerate(targets, 1):
            print(f"{i}/{len(targets)}: {t.asset} / eToro {t.etoro_symbol} / ID {t.instrument_id}")
            print(f"  Week end: {t.week_end.date()} | 52W return: {t.momentum_score:+.2%}")
            print(f"  Close: {t.weekly_close:.5g} | SMA30 exit MA: {t.sma30:.5g}")
            print(f"  Prev 50W high: {t.prior50_high:.5g} | Prev 20W low: {t.prior20_low:.5g}")
        print(f"Target allocation: {1.0 / len(targets):.2%} each of equity (subject to fees/settlement).")


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
    last_friday = most_recent_completed_friday(now_local)
    print_rule("ETORO DEMO — 50/20 WEEKLY BREAKOUT / TOP 3 / 52W / SMA30 EXIT")
    print(f"Local time:                 {now_local:%Y-%m-%d %H:%M:%S %Z}")
    print(f"Universe size (deduped):    {len(UNIVERSE)}")
    print(f"Frozen weekly signal:       {last_friday.date()} (Friday close)")
    print(f"Execution enabled:          {EXECUTE_TRADES}")
    print(f"Target holdings:            {TARGET_POSITIONS} x {NEW_POSITION_WEIGHT:.0%} of equity")
    print("Entry:                     Close > previous 50W high (NO SMA entry filter)")
    print("Exit:                      Close < prior 20W low OR weekly Open AND Close < SMA30")
    print("Ranking:                   highest 52W return among eligible assets")
    print("Selection cycle:           EVERY completed week; Top 3 or fewer, cash if none")

    api_key, user_key = validate_credentials()
    client = EtoroClient(api_key, user_key)

    market_data, resolved_yahoo, download_failures = download_universe()
    ranking, features_by_asset = build_weekly_ranking(market_data, now_local)
    print_momentum_table(ranking)

    portfolio = read_portfolio_state(client)
    if portfolio.account_currency.upper() != "USD":
        raise RuntimeError(f"Demo account currency is {portfolio.account_currency}, not USD.")

    client.load_instrument_catalog()
    resolved_by_asset, asset_by_instrument_id = resolve_universe_instruments(client)

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

    print_rule("ETORO ACCOUNT SNAPSHOT")
    print(f"CID:                         {portfolio.cid}")
    print(f"Equity:                      ${portfolio.equity:,.2f}")
    print(f"Available cash:              ${portfolio.available_cash:,.2f}")
    print(f"Direct instruments held:     {len(portfolio.current_ids)}")
    print(f"Pending opening instruments: {len(portfolio.pending_open_ids)}")
    print(f"Pending closing instruments: {len(portfolio.pending_close_ids)}")

    weekly_targets: Optional[List[TargetAsset]] = None
    diagnostics = pd.DataFrame()
    try:
        weekly_targets, diagnostics = build_weekly_targets(
            client=client, ranking=ranking, resolved_by_asset=resolved_by_asset
        )
        print_target_diagnostics(diagnostics, weekly_targets)
    except Exception as error:
        print_rule("WEEKLY TARGET ERROR")
        print(f"{type(error).__name__}: {error}")
        traceback.print_exc()
        weekly_targets = None
        print("Weekly target changes disabled; 20-week-low and SMA30 exits can still be planned.")

    plan = build_action_plan(
        client=client, portfolio=portfolio,
        market_data=market_data, asset_by_instrument_id=asset_by_instrument_id,
        resolved_by_asset=resolved_by_asset, weekly_targets=weekly_targets,
        selection_active=(weekly_targets is not None), now_local=now_local,
    )
    print_action_plan(plan)

    if download_failures:
        print_rule("MARKET DATA WARNINGS")
        for asset, error in download_failures.items():
            print(f"{asset:<10} {error}")
        print("WARNING: incomplete universe can change Top 3 selection from the backtest.")
        print("Assets without data cannot enter the ranking or be evaluated for stops.")

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
