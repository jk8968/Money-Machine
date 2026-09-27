from __future__ import annotations

"""
Weekly 50-MA cross portfolio bot
================================

Execution venue:
    Binance USDⓈ-M Futures (USDT-settled perpetuals)

Signal source:
    TradingView weekly candles via tvdatafeed

Strategy:
    - Long only.
    - No intentional leverage: Binance leverage is forced to 1x for strategy symbols.
    - Maximum 5 simultaneous strategy positions.
    - Each NEW entry targets 20% of REALIZED futures-wallet balance.
    - Existing positions are never topped up or rebalanced.
    - SELLs are processed and confirmed before any BUY.
    - BUY: previous completed weekly candle OPEN was below its 50W SMA, and the
      latest completed candle OPENS AND CLOSES above its 50W SMA.
    - SELL: previous completed weekly candle OPEN was above its 50W SMA, and the
      latest completed candle OPENS AND CLOSES below its 50W SMA.
    - A mixed latest candle (open/close on opposite sides of SMA50) gives no signal.
    - Re-running the bot in the same week is safe: positions/orders on Binance are
      treated as the source of truth, and client order IDs are deterministic.

Important:
    EXECUTE_TRADES defaults to False. Review diagnostics first, then set True.

Dependencies:
    pip install pandas requests tvdatafeed

Environment variables:
    BINANCE_API_KEY
    BINANCE_API_SECRET
    TV_USERNAME          (optional)
    TV_PASSWORD          (optional)
"""

import hashlib
import hmac
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from tvDatafeed import Interval, TvDatafeed


# =============================================================================
# CONFIG
# =============================================================================

LOCAL_TZ = ZoneInfo("Europe/Ljubljana")

BINANCE_BASE_URL = "https://fapi.binance.com"
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()

TV_USERNAME = os.getenv("TV_USERNAME") or None
TV_PASSWORD = os.getenv("TV_PASSWORD") or None

# SAFETY SWITCH. Run with False first and inspect the action plan.
EXECUTE_TRADES = False

SMA_WEEKS = 50
TV_BARS = 140
MAX_POSITIONS = 5
ENTRY_WEIGHT = Decimal("0.20")
USDT_CASH_RESERVE = Decimal("0.05")

REQUEST_TIMEOUT = 20
RECV_WINDOW = 10_000
FILL_TIMEOUT_SECONDS = 30
FILL_POLL_SECONDS = 1.0

# TradingView weekly bars for many futures begin Sunday evening, while crypto/OANDA
# weekly bars typically stamp Monday. Any last bar starting Sunday/Monday of the
# current local week is therefore considered the still-forming current candle.
CURRENT_WEEK_START_GRACE_DAYS = 1


@dataclass(frozen=True)
class AssetConfig:
    binance_symbol: str
    tv_symbol: str
    tv_exchange: str
    fut_contract: Optional[int] = None


# Fixed order is only a deterministic tie-break if more BUY crosses occur than
# available slots. No momentum/volatility ranking is used.
#
# Earlier "XAUUSDT + XAUUSDT" is interpreted as Silver + Gold:
# XAGUSDT = Silver, XAUUSDT = Gold.
ASSETS: List[AssetConfig] = [
    AssetConfig("COPPERUSDT", "HG", "COMEX", 1),
    AssetConfig("NATGASUSDT", "NG", "NYMEX", 1),
    AssetConfig("CLUSDT", "CL", "NYMEX", 1),
    AssetConfig("XAGUSDT", "XAGUSD", "OANDA", None),
    AssetConfig("XAUUSDT", "XAUUSD", "OANDA", None),
    AssetConfig("XRPUSDT", "XRPUSDT", "BINANCE", None),
    AssetConfig("SOLUSDT", "SOLUSDT", "BINANCE", None),
]


# =============================================================================
# HELPERS
# =============================================================================

class BinanceAPIError(RuntimeError):
    def __init__(self, status_code: int, code: Optional[int], message: str):
        super().__init__(f"Binance HTTP {status_code} code={code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


def D(value: Any) -> Decimal:
    return Decimal(str(value))


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def print_rule(title: str) -> None:
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def current_monday_date(dt: datetime):
    return (dt - timedelta(days=dt.weekday())).date()


# =============================================================================
# TRADINGVIEW SIGNAL ENGINE
# =============================================================================

@dataclass
class SignalSnapshot:
    binance_symbol: str
    tv_name: str
    week_start: pd.Timestamp
    open_price: float
    close_price: float
    sma50: float
    previous_open: float
    previous_sma50: float
    latest_state: str
    signal: str  # BUY, SELL, NONE


def make_tv() -> TvDatafeed:
    if TV_USERNAME and TV_PASSWORD:
        return TvDatafeed(TV_USERNAME, TV_PASSWORD)
    return TvDatafeed()


def get_weekly_history(tv: TvDatafeed, cfg: AssetConfig) -> pd.DataFrame:
    df = tv.get_hist(
        symbol=cfg.tv_symbol,
        exchange=cfg.tv_exchange,
        interval=Interval.in_weekly,
        n_bars=TV_BARS,
        fut_contract=cfg.fut_contract,
        extended_session=False,
    )
    if df is None or df.empty:
        raise RuntimeError(f"No TradingView data for {cfg.tv_exchange}:{cfg.tv_symbol}")

    frame = df.copy().sort_index()
    for col in ("open", "close"):
        if col not in frame.columns:
            raise RuntimeError(f"{cfg.binance_symbol}: TradingView column {col!r} missing")
        frame[col] = pd.to_numeric(frame[col], errors="coerce")

    frame = frame.dropna(subset=["open", "close"])
    if len(frame) < SMA_WEEKS + 2:
        raise RuntimeError(
            f"{cfg.binance_symbol}: only {len(frame)} weekly bars; need at least {SMA_WEEKS + 2}"
        )
    return frame


def drop_current_incomplete_week(frame: pd.DataFrame, dt_local: datetime) -> pd.DataFrame:
    """
    Use TradingView's 1W candles exactly as returned. We do NOT rebuild candles.

    tvdatafeed timestamps are bar starts. A current futures week may begin Sunday
    evening and a crypto/OANDA week may stamp Monday, so a last bar dated current
    Monday minus one day or later is treated as the still-forming week and removed.
    """
    if frame.empty:
        return frame

    monday = current_monday_date(dt_local)
    threshold = monday - timedelta(days=CURRENT_WEEK_START_GRACE_DAYS)
    last_date = pd.Timestamp(frame.index[-1]).date()

    if last_date >= threshold:
        frame = frame.iloc[:-1].copy()

    if len(frame) < SMA_WEEKS + 2:
        raise RuntimeError("Too little completed weekly history after dropping current bar.")
    return frame


def classify_body(open_price: float, close_price: float, sma: float) -> str:
    if open_price > sma and close_price > sma:
        return "ABOVE"
    if open_price < sma and close_price < sma:
        return "BELOW"
    return "MIXED"


def build_signal(tv: TvDatafeed, cfg: AssetConfig, dt_local: datetime) -> SignalSnapshot:
    frame = get_weekly_history(tv, cfg)
    frame = drop_current_incomplete_week(frame, dt_local)
    frame["sma50"] = frame["close"].rolling(SMA_WEEKS).mean()

    states: List[Optional[str]] = []
    for _, row in frame.iterrows():
        if pd.isna(row["sma50"]):
            states.append(None)
        else:
            states.append(
                classify_body(float(row["open"]), float(row["close"]), float(row["sma50"]))
            )
    frame["state"] = states

    latest = frame.iloc[-1]
    previous = frame.iloc[-2]

    if pd.isna(latest["sma50"]) or pd.isna(previous["sma50"]):
        raise RuntimeError(f"{cfg.binance_symbol}: SMA50 unavailable on signal candles")

    latest_state = str(latest["state"])
    previous_open = float(previous["open"])
    previous_sma50 = float(previous["sma50"])

    # True cross rule:
    # BUY  -> previous candle OPEN below its SMA50, then latest candle
    #         OPENS AND CLOSES above its SMA50.
    # SELL -> previous candle OPEN above its SMA50, then latest candle
    #         OPENS AND CLOSES below its SMA50.
    #
    # The previous candle's CLOSE is deliberately not part of the cross test.
    signal = "NONE"
    if previous_open < previous_sma50 and latest_state == "ABOVE":
        signal = "BUY"
    elif previous_open > previous_sma50 and latest_state == "BELOW":
        signal = "SELL"

    tv_name = f"{cfg.tv_exchange}:{cfg.tv_symbol}"
    if cfg.fut_contract:
        tv_name += f"{cfg.fut_contract}!"

    return SignalSnapshot(
        binance_symbol=cfg.binance_symbol,
        tv_name=tv_name,
        week_start=pd.Timestamp(frame.index[-1]),
        open_price=float(latest["open"]),
        close_price=float(latest["close"]),
        sma50=float(latest["sma50"]),
        previous_open=previous_open,
        previous_sma50=previous_sma50,
        latest_state=latest_state,
        signal=signal,
    )


# =============================================================================
# BINANCE USD-M REST CLIENT
# =============================================================================

class BinanceUM:
    def __init__(self, api_key: str, api_secret: str):
        if not api_key or not api_secret:
            raise RuntimeError(
                "Missing BINANCE_API_KEY / BINANCE_API_SECRET environment variables."
            )
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})
        self.secret = api_secret.encode()
        self._time_offset_ms = 0

    def _timestamp_ms(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    def sync_time(self) -> None:
        response = self.session.get(
            BINANCE_BASE_URL + "/fapi/v1/time",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        server_ms = int(response.json()["serverTime"])
        local_ms = int(time.time() * 1000)
        self._time_offset_ms = server_ms - local_ms

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
    ) -> Any:
        payload: Dict[str, Any] = dict(params or {})

        if signed:
            payload["timestamp"] = self._timestamp_ms()
            payload["recvWindow"] = RECV_WINDOW
            query = urlencode(payload, doseq=True)
            payload["signature"] = hmac.new(
                self.secret,
                query.encode(),
                hashlib.sha256,
            ).hexdigest()

        response = self.session.request(
            method,
            BINANCE_BASE_URL + path,
            params=payload,
            timeout=REQUEST_TIMEOUT,
        )

        if not response.ok:
            code = None
            message = response.text
            try:
                body = response.json()
                code = body.get("code")
                message = body.get("msg", message)
            except Exception:
                pass
            raise BinanceAPIError(response.status_code, code, message)

        if not response.text:
            return {}
        return response.json()

    def exchange_info(self) -> Dict[str, Any]:
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def ticker_price(self, symbol: str) -> Decimal:
        data = self._request("GET", "/fapi/v2/ticker/price", {"symbol": symbol})
        return D(data["price"])

    def account(self) -> Dict[str, Any]:
        return self._request("GET", "/fapi/v3/account", signed=True)

    def positions(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/fapi/v3/positionRisk", signed=True)

    def open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/openOrders", params, signed=True)

    def query_order_by_client_id(self, symbol: str, client_id: str) -> Optional[Dict[str, Any]]:
        try:
            return self._request(
                "GET",
                "/fapi/v1/order",
                {"symbol": symbol, "origClientOrderId": client_id},
                signed=True,
            )
        except BinanceAPIError as exc:
            if exc.code == -2013:  # Order does not exist.
                return None
            raise

    def position_mode_is_hedge(self) -> bool:
        data = self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        return bool(data.get("dualSidePosition", False))

    def set_leverage_1x(self, symbol: str) -> None:
        self._request(
            "POST",
            "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": 1},
            signed=True,
        )

    def cancel_order(self, symbol: str, order_id: int) -> Dict[str, Any]:
        return self._request(
            "DELETE",
            "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id},
            signed=True,
        )

    def market_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        client_id: str,
        reduce_only: bool,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": decimal_text(quantity),
            "newClientOrderId": client_id,
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        return self._request("POST", "/fapi/v1/order", params, signed=True)


# =============================================================================
# ACCOUNT / SYMBOL RULES
# =============================================================================

@dataclass
class SymbolRules:
    symbol: str
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal


@dataclass
class AccountSnapshot:
    wallet_balance: Decimal      # realized wallet balance; excludes unrealized PnL
    available_balance: Decimal   # currently available for new orders
    positions: Dict[str, Decimal]
    open_orders: Dict[str, List[Dict[str, Any]]]


def parse_symbol_rules(exchange_info: Dict[str, Any]) -> Dict[str, SymbolRules]:
    wanted = {asset.binance_symbol for asset in ASSETS}
    result: Dict[str, SymbolRules] = {}

    for item in exchange_info.get("symbols", []):
        symbol = str(item.get("symbol", "")).upper()
        if symbol not in wanted:
            continue

        filters = {f.get("filterType"): f for f in item.get("filters", [])}
        lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE") or {}
        step_size = D(lot.get("stepSize", "0"))
        min_qty = D(lot.get("minQty", "0"))

        min_notional = Decimal("0")
        min_filter = filters.get("MIN_NOTIONAL")
        if min_filter:
            min_notional = D(min_filter.get("notional", min_filter.get("minNotional", "0")))
        notional_filter = filters.get("NOTIONAL")
        if notional_filter:
            min_notional = max(min_notional, D(notional_filter.get("minNotional", "0")))

        result[symbol] = SymbolRules(
            symbol=symbol,
            step_size=step_size,
            min_qty=min_qty,
            min_notional=min_notional,
        )

    missing = wanted - set(result)
    if missing:
        raise RuntimeError(
            "Configured symbols missing from Binance USD-M exchangeInfo: "
            + ", ".join(sorted(missing))
        )
    return result


def read_account_snapshot(client: BinanceUM) -> AccountSnapshot:
    account = client.account()
    positions_raw = client.positions()
    orders_raw = client.open_orders()

    wallet_balance = D(account.get("totalWalletBalance", "0"))
    available_balance = D(account.get("availableBalance", "0"))

    wanted = {asset.binance_symbol for asset in ASSETS}
    positions: Dict[str, Decimal] = {}
    for row in positions_raw:
        symbol = str(row.get("symbol", "")).upper()
        if symbol not in wanted:
            continue
        amount = D(row.get("positionAmt", "0"))
        if amount != 0:
            positions[symbol] = amount

    open_orders: Dict[str, List[Dict[str, Any]]] = {}
    for order in orders_raw:
        symbol = str(order.get("symbol", "")).upper()
        if symbol in wanted:
            open_orders.setdefault(symbol, []).append(order)

    return AccountSnapshot(
        wallet_balance=wallet_balance,
        available_balance=available_balance,
        positions=positions,
        open_orders=open_orders,
    )


def strategy_long_symbols(snapshot: AccountSnapshot) -> List[str]:
    longs: List[str] = []
    for symbol, qty in snapshot.positions.items():
        if qty < 0:
            raise RuntimeError(
                f"{symbol} is SHORT ({qty}). This long-only bot will not manage a short automatically."
            )
        if qty > 0:
            longs.append(symbol)
    return longs


# =============================================================================
# ORDER SAFETY / IDEMPOTENCY
# =============================================================================

def deterministic_client_id(symbol: str, week_start: pd.Timestamp, side_letter: str) -> str:
    stamp = pd.Timestamp(week_start).strftime("%Y%m%d")
    return f"w50_{symbol}_{stamp}_{side_letter}"


def cancel_conflicting_orders(
    client: BinanceUM,
    snapshot: AccountSnapshot,
    symbol: str,
    signal: str,
) -> None:
    """
    SELL signal -> cancel outstanding BUY orders first.
    BUY signal  -> cancel outstanding SELL orders first.
    """
    conflict_side = "BUY" if signal == "SELL" else "SELL"
    for order in snapshot.open_orders.get(symbol, []):
        if str(order.get("side", "")).upper() != conflict_side:
            continue
        order_id = int(order["orderId"])
        print(f"  cancel conflicting {conflict_side} order {order_id} on {symbol}")
        if EXECUTE_TRADES:
            client.cancel_order(symbol, order_id)


def wait_until_position(client: BinanceUM, symbol: str, should_be_long: bool) -> bool:
    deadline = time.time() + FILL_TIMEOUT_SECONDS
    while time.time() < deadline:
        snapshot = read_account_snapshot(client)
        qty = snapshot.positions.get(symbol, Decimal("0"))
        if should_be_long and qty > 0:
            return True
        if not should_be_long and qty == 0:
            return True
        time.sleep(FILL_POLL_SECONDS)
    return False


def submit_market_order_idempotent(
    client: BinanceUM,
    symbol: str,
    side: str,
    quantity: Decimal,
    client_id: str,
    reduce_only: bool,
) -> Optional[Dict[str, Any]]:
    """
    Deterministic clientOrderId prevents accidental duplicate orders on reruns.
    It also lets us recover after a network error without blindly retrying a write.
    """
    existing = client.query_order_by_client_id(symbol, client_id)
    if existing is not None:
        print(
            f"  existing order clientOrderId={client_id} status={existing.get('status')}; "
            "no duplicate submitted"
        )
        return existing

    if not EXECUTE_TRADES:
        print(
            f"  DRY RUN: {side} {decimal_text(quantity)} {symbol} "
            f"reduceOnly={reduce_only} clientOrderId={client_id}"
        )
        return None

    try:
        return client.market_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            client_id=client_id,
            reduce_only=reduce_only,
        )
    except requests.RequestException as exc:
        print(f"  network error during order write: {exc}")
        print("  checking Binance by deterministic clientOrderId before any retry...")
        time.sleep(2)
        recovered = client.query_order_by_client_id(symbol, client_id)
        if recovered is not None:
            print(f"  recovered order status={recovered.get('status')}")
            return recovered
        raise RuntimeError(
            f"Unknown execution state for {symbol}; aborting to avoid a duplicate order."
        ) from exc


# =============================================================================
# QUANTITY / CASH SIZING
# =============================================================================

def buy_quantity_for_notional(
    client: BinanceUM,
    rules: SymbolRules,
    target_notional: Decimal,
) -> Tuple[Decimal, Decimal]:
    price = client.ticker_price(rules.symbol)
    if price <= 0:
        raise RuntimeError(f"{rules.symbol}: invalid Binance price {price}")

    qty = floor_to_step(target_notional / price, rules.step_size)
    actual_notional = qty * price

    if qty <= 0 or qty < rules.min_qty:
        raise RuntimeError(
            f"target {target_notional} USDT -> qty={qty}, below minQty={rules.min_qty}"
        )
    if rules.min_notional > 0 and actual_notional < rules.min_notional:
        raise RuntimeError(
            f"notional {actual_notional:.4f} below Binance minimum {rules.min_notional}"
        )
    return qty, actual_notional


# =============================================================================
# MAIN
# =============================================================================

def run() -> None:
    run_time = now_local()

    print_rule("WEEKLY 50-MA CROSS PORTFOLIO BOT")
    print(f"Run time:                 {run_time.isoformat()}")
    print(f"Execution enabled:        {EXECUTE_TRADES}")
    print(f"Max positions:            {MAX_POSITIONS}")
    print(f"New-entry target:         {ENTRY_WEIGHT:.0%} of REALIZED wallet balance")
    print("BUY cross:                previous confirmed BELOW -> latest completed ABOVE")
    print("SELL cross:               previous confirmed ABOVE -> latest completed BELOW")
    print("MIXED candle:             no reset / no signal")

    # -------------------------------------------------------------------------
    # 1) TradingView signals
    # -------------------------------------------------------------------------
    tv = make_tv()
    signals: Dict[str, SignalSnapshot] = {}
    failures: Dict[str, str] = {}

    print_rule("TRADINGVIEW WEEKLY SIGNALS")
    for cfg in ASSETS:
        try:
            signal = build_signal(tv, cfg, run_time)
            signals[cfg.binance_symbol] = signal
            print(
                f"{cfg.binance_symbol:<12} {signal.tv_name:<20} "
                f"week={signal.week_start.date()} "
                f"O={signal.open_price:.6g} C={signal.close_price:.6g} "
                f"SMA50={signal.sma50:.6g} "
                f"prevO={signal.previous_open:.6g} prevSMA={signal.previous_sma50:.6g} "
                f"now={signal.latest_state:<5} SIGNAL={signal.signal}"
            )
        except Exception as exc:
            failures[cfg.binance_symbol] = str(exc)
            print(f"{cfg.binance_symbol:<12} DATA ERROR: {exc}")

    if not signals:
        raise RuntimeError("No valid TradingView signals; refusing to trade.")

    if failures:
        print("\nAssets skipped this run because signal data failed:")
        for symbol, error in failures.items():
            print(f"  {symbol}: {error}")

    # -------------------------------------------------------------------------
    # 2) Binance state and trading rules
    # -------------------------------------------------------------------------
    client = BinanceUM(BINANCE_API_KEY, BINANCE_API_SECRET)
    client.sync_time()

    if client.position_mode_is_hedge():
        raise RuntimeError(
            "Binance Futures is in Hedge Mode. Switch this dedicated account to One-way Mode."
        )

    rules = parse_symbol_rules(client.exchange_info())

    if EXECUTE_TRADES:
        for cfg in ASSETS:
            client.set_leverage_1x(cfg.binance_symbol)

    before = read_account_snapshot(client)
    longs_before = strategy_long_symbols(before)

    print_rule("BINANCE ACCOUNT BEFORE ACTIONS")
    print(f"Realized wallet balance:  {before.wallet_balance} USDT")
    print(f"Available balance:        {before.available_balance} USDT")
    print(f"Strategy positions:       {len(longs_before)} / {MAX_POSITIONS}")
    for symbol in longs_before:
        print(f"  {symbol}: qty={before.positions[symbol]}")

    if len(longs_before) > MAX_POSITIONS:
        raise RuntimeError(
            f"Already holding {len(longs_before)} strategy assets, above limit {MAX_POSITIONS}."
        )

    # -------------------------------------------------------------------------
    # 3) SELL PHASE — always before buys
    # -------------------------------------------------------------------------
    sell_symbols = [
        cfg.binance_symbol
        for cfg in ASSETS
        if cfg.binance_symbol in signals and signals[cfg.binance_symbol].signal == "SELL"
    ]

    print_rule("SELL PHASE")
    if not sell_symbols:
        print("No SELL crosses.")

    for symbol in sell_symbols:
        signal = signals[symbol]
        snapshot = read_account_snapshot(client)

        # A pending BUY plus a SELL cross must not survive.
        cancel_conflicting_orders(client, snapshot, symbol, "SELL")
        if EXECUTE_TRADES:
            snapshot = read_account_snapshot(client)  # catches any partial BUY fill

        qty = snapshot.positions.get(symbol, Decimal("0"))
        if qty < 0:
            raise RuntimeError(f"{symbol} is short ({qty}); refusing long-only sell logic.")
        if qty == 0:
            print(f"{symbol}: SELL cross but already flat. No action.")
            continue

        close_qty = floor_to_step(abs(qty), rules[symbol].step_size)
        if close_qty <= 0:
            raise RuntimeError(f"{symbol}: cannot quantize close quantity from {qty}")

        client_id = deterministic_client_id(symbol, signal.week_start, "S")
        print(f"{symbol}: SELL cross -> close qty={close_qty}")
        submit_market_order_idempotent(
            client=client,
            symbol=symbol,
            side="SELL",
            quantity=close_qty,
            client_id=client_id,
            reduce_only=True,
        )

        if EXECUTE_TRADES:
            if not wait_until_position(client, symbol, should_be_long=False):
                raise RuntimeError(
                    f"{symbol}: sell did not become flat within {FILL_TIMEOUT_SECONDS}s. "
                    "Aborting before buys."
                )
            print(f"  {symbol}: confirmed FLAT")

    # Every sell must be finished before the buy budget is calculated.
    after_sells = read_account_snapshot(client)
    longs_after_sells = strategy_long_symbols(after_sells)

    print_rule("ACCOUNT AFTER SELL PHASE")
    print(f"Realized wallet balance:  {after_sells.wallet_balance} USDT")
    print(f"Available balance:        {after_sells.available_balance} USDT")
    print(f"Positions remaining:      {len(longs_after_sells)} / {MAX_POSITIONS}")

    # -------------------------------------------------------------------------
    # 4) BUY PHASE
    # -------------------------------------------------------------------------
    # Crucial sizing rule:
    #   target = 20% of totalWalletBalance (realized base, no unrealized PnL)
    #   actual order <= availableBalance after completed sells
    target_entry_notional = after_sells.wallet_balance * ENTRY_WEIGHT

    buy_symbols = [
        cfg.binance_symbol
        for cfg in ASSETS
        if cfg.binance_symbol in signals and signals[cfg.binance_symbol].signal == "BUY"
    ]

    print_rule("BUY PHASE")
    print(f"20% realized-base target: {target_entry_notional} USDT")
    print(f"Initial free slots:       {max(0, MAX_POSITIONS - len(longs_after_sells))}")
    if not buy_symbols:
        print("No BUY crosses.")

    for symbol in buy_symbols:
        signal = signals[symbol]
        snapshot = read_account_snapshot(client)

        # If an old/pending SELL exists when a fresh BUY cross occurs, cancel it.
        cancel_conflicting_orders(client, snapshot, symbol, "BUY")
        if EXECUTE_TRADES:
            snapshot = read_account_snapshot(client)

        qty_held = snapshot.positions.get(symbol, Decimal("0"))
        if qty_held < 0:
            raise RuntimeError(f"{symbol} is short ({qty_held}); refusing BUY logic.")
        if qty_held > 0:
            print(f"{symbol}: BUY cross but already LONG qty={qty_held}; no top-up/re-entry.")
            continue

        pending_buys = [
            order
            for order in snapshot.open_orders.get(symbol, [])
            if str(order.get("side", "")).upper() == "BUY"
        ]
        if pending_buys:
            print(f"{symbol}: BUY cross but BUY order is already pending; no duplicate.")
            continue

        current_longs = strategy_long_symbols(snapshot)
        if len(current_longs) >= MAX_POSITIONS:
            print(f"{symbol}: BUY cross MISSED — portfolio already has {MAX_POSITIONS} positions.")
            continue

        # availableBalance is the hard spendable constraint. totalWalletBalance is
        # the realized sizing base, so unrealized PnL never enlarges a new entry.
        spendable = max(Decimal("0"), snapshot.available_balance - USDT_CASH_RESERVE)
        requested_notional = min(target_entry_notional, spendable)

        if requested_notional <= 0:
            print(f"{symbol}: BUY cross but no available USDT after reserve.")
            continue

        try:
            buy_qty, approx_notional = buy_quantity_for_notional(
                client,
                rules[symbol],
                requested_notional,
            )
        except Exception as exc:
            print(f"{symbol}: BUY skipped: {exc}")
            continue

        client_id = deterministic_client_id(symbol, signal.week_start, "B")
        print(
            f"{symbol}: BUY cross -> qty={buy_qty}, approx={approx_notional:.4f} USDT "
            f"(available={snapshot.available_balance})"
        )
        submit_market_order_idempotent(
            client=client,
            symbol=symbol,
            side="BUY",
            quantity=buy_qty,
            client_id=client_id,
            reduce_only=False,
        )

        if EXECUTE_TRADES:
            if not wait_until_position(client, symbol, should_be_long=True):
                raise RuntimeError(
                    f"{symbol}: buy did not create a long within {FILL_TIMEOUT_SECONDS}s. "
                    "Aborting remaining buys."
                )
            print(f"  {symbol}: confirmed LONG")

    # -------------------------------------------------------------------------
    # 5) FINAL REPORT
    # -------------------------------------------------------------------------
    final = read_account_snapshot(client)
    final_longs = strategy_long_symbols(final)

    print_rule("FINAL BINANCE STATE")
    print(f"Realized wallet balance:  {final.wallet_balance} USDT")
    print(f"Available balance:        {final.available_balance} USDT")
    print(f"Strategy positions:       {len(final_longs)} / {MAX_POSITIONS}")
    for symbol in final_longs:
        print(f"  {symbol}: qty={final.positions[symbol]}")

    print("\nRun complete.")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except Exception as exc:
        print_rule("FATAL ERROR — NO FURTHER ACTIONS")
        print(f"{type(exc).__name__}: {exc}")
        raise
