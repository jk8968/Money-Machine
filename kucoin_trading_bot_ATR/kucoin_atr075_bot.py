#!/usr/bin/env python3
"""
Minimal KuCoin Futures 4h ATR Expansion bot.

Strategy (current research candidate)
-------------------------------------
Universe: XBT/ETH/SOL/BNB/XRP USDT-margined perpetuals
Timeframe: 4h
ATR: Wilder ATR(20)
Long signal:
    close_t > close_(t-1) + 0.75 * ATR20_t
Short signal:
    close_t < close_(t-1) - 0.75 * ATR20_t

Entry: market, shortly after the 4h bar closes
Initial stop: 2.0 * ATR20
Take profit: 1.5R = 3.0 * ATR20
Risk: 2.5% of each equal 1/5 portfolio sleeve
Max leverage / notional cap: 2x per sleeve
Margin mode: isolated

Design goals
------------
- No database.
- No WebSocket.
- Run briefly every 4 hours, then exit.
- KuCoin is the persistent state.
- If a symbol already has an open position, do not open another.
- Entry + attached TP/SL are submitted in ONE KuCoin /api/v1/st-orders request.
- Deterministic clientOid makes rerunning the same 4h signal idempotent-ish.
- POST orders are NOT automatically retried after an ambiguous network failure.

IMPORTANT
---------
This code targets KuCoin's currently documented Classic Futures REST API
and assumes ONE-WAY position mode (positionSide="BOTH").

LIVE_TRADING defaults to NO. Test with dry-run first and verify the exact
contracts, account mode, fills, TP/SL behavior, and candle parity before
putting real funds behind it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests


# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://api-futures.kucoin.com"

SYMBOLS = [
    "XBTUSDTM",
    "ETHUSDTM",
    "SOLUSDTM",
    "BNBUSDTM",
    "XRPUSDTM",
]

CANDLE_SECONDS = 4 * 60 * 60
KLINE_GRANULARITY = 14400  # KuCoin Classic Futures: seconds
KLINE_BARS = 100

ATR_N = 20
TRIGGER_ATR = 0.75
STOP_ATR = 2.0
TP_R = 1.5                    # 1.5R => 3 ATR with a 2 ATR stop
RISK_PER_SLEEVE = 0.025       # 2.5% of each 1/5 sleeve
MAX_LEVERAGE = 2              # chosen live cap
MARGIN_MODE = "ISOLATED"
POSITION_SIDE = "BOTH"        # one-way mode
STOP_PRICE_TYPE = "TP"        # KuCoin trade/transaction price trigger

ENTRY_GRACE_SECONDS = 10 * 60
HTTP_TIMEOUT = 10
GET_RETRIES = 3

LIVE_TRADING = os.getenv("LIVE_TRADING", "NO").strip().upper() == "YES"
DRY_RUN_EQUITY_USDT = float(os.getenv("DRY_RUN_EQUITY_USDT", "10000"))

API_KEY = os.getenv("KUCOIN_API_KEY", "").strip()
API_SECRET = os.getenv("KUCOIN_API_SECRET", "").strip()
API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE", "").strip()
API_KEY_VERSION = os.getenv("KUCOIN_API_KEY_VERSION", "2").strip()


# ============================================================
# SMALL HELPERS
# ============================================================

def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), msg, flush=True)


def d(x: Any) -> Decimal:
    return Decimal(str(x))


def normalize_ms(ts: Any) -> int:
    """Accept seconds, milliseconds, microseconds, or nanoseconds."""
    x = int(ts)
    if x < 10_000_000_000:          # seconds
        return x * 1000
    if x < 10_000_000_000_000:      # milliseconds
        return x
    if x < 10_000_000_000_000_000:  # microseconds
        return x // 1000
    return x // 1_000_000            # nanoseconds


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_FLOOR)
    return units * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_CEILING)
    return units * step


def decimal_str(x: Decimal) -> str:
    """Plain decimal string, no scientific notation."""
    s = format(x, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# ============================================================
# KUCOIN REST CLIENT
# ============================================================

class KuCoinError(RuntimeError):
    pass


class KuCoinFutures:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.offset_ms = 0

    @property
    def has_private_credentials(self) -> bool:
        return bool(API_KEY and API_SECRET and API_PASSPHRASE)

    def _headers(self, method: str, endpoint: str, body_text: str) -> Dict[str, str]:
        if not self.has_private_credentials:
            raise KuCoinError("Private endpoint requires KUCOIN API credentials.")

        ts = str(int(time.time() * 1000) + self.offset_ms)
        prehash = ts + method.upper() + endpoint + body_text

        signature = base64.b64encode(
            hmac.new(
                API_SECRET.encode(),
                prehash.encode(),
                hashlib.sha256,
            ).digest()
        ).decode()

        passphrase = base64.b64encode(
            hmac.new(
                API_SECRET.encode(),
                API_PASSPHRASE.encode(),
                hashlib.sha256,
            ).digest()
        ).decode()

        return {
            "KC-API-KEY": API_KEY,
            "KC-API-SIGN": signature,
            "KC-API-TIMESTAMP": ts,
            "KC-API-PASSPHRASE": passphrase,
            "KC-API-KEY-VERSION": API_KEY_VERSION,
            "Content-Type": "application/json",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        private: bool = False,
        retry_gets: bool = True,
    ) -> Any:
        method = method.upper()

        query = ""
        if params:
            # Values used here are simple KuCoin symbols/currency/numbers,
            # so the transmitted query and signature string remain aligned.
            query = "?" + urlencode(params)

        endpoint = path + query
        url = BASE_URL + endpoint

        body_text = ""
        if body is not None:
            body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        headers = {"Content-Type": "application/json"}
        if private:
            headers = self._headers(method, endpoint, body_text)

        attempts = GET_RETRIES if (method == "GET" and retry_gets) else 1
        last_exc: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    data=body_text if body is not None else None,
                    timeout=HTTP_TIMEOUT,
                )
                response.raise_for_status()
                payload = response.json()

                if payload.get("code") != "200000":
                    raise KuCoinError(
                        f"KuCoin error {payload.get('code')}: {payload.get('msg') or payload}"
                    )
                return payload.get("data")

            except (requests.RequestException, ValueError, KuCoinError) as exc:
                last_exc = exc
                if attempt >= attempts:
                    break
                time.sleep(1.0 * attempt)

        raise KuCoinError(f"{method} {path} failed: {last_exc}")

    def sync_time(self) -> int:
        server_ms = int(self.request("GET", "/api/v1/timestamp"))
        self.offset_ms = server_ms - int(time.time() * 1000)
        return server_ms

    def service_open(self) -> bool:
        data = self.request("GET", "/api/v1/status")
        return str(data.get("status", "")).lower() == "open"

    def contract(self, symbol: str) -> Dict[str, Any]:
        return self.request("GET", f"/api/v1/contracts/{symbol}")

    def ticker(self, symbol: str) -> Dict[str, Any]:
        return self.request("GET", "/api/v1/ticker", params={"symbol": symbol})

    def klines(self, symbol: str, now_ms: int) -> List[List[Any]]:
        start_ms = now_ms - KLINE_BARS * CANDLE_SECONDS * 1000
        return self.request(
            "GET",
            "/api/v1/kline/query",
            params={
                "symbol": symbol,
                "granularity": KLINE_GRANULARITY,
                "from": start_ms,
                "to": now_ms,
            },
        )

    def account_equity(self) -> float:
        data = self.request(
            "GET",
            "/api/v1/account-overview",
            params={"currency": "USDT"},
            private=True,
        )
        return float(data["accountEquity"])

    def positions(self) -> List[Dict[str, Any]]:
        data = self.request(
            "GET",
            "/api/v1/positions",
            params={"currency": "USDT"},
            private=True,
        )
        return data or []

    def place_attached_market_order(self, body: Dict[str, Any]) -> Dict[str, Any]:
        # IMPORTANT:
        # Deliberately no automatic retry. If the TCP connection dies after
        # KuCoin accepted the order but before we saw the response, blindly
        # retrying a live order is dangerous. Re-run the script; the same
        # deterministic clientOid and position reconciliation are safer.
        return self.request(
            "POST",
            "/api/v1/st-orders",
            body=body,
            private=True,
            retry_gets=False,
        )


# ============================================================
# MARKET DATA / ATR
# ============================================================

@dataclass
class Candle:
    start_ms: int
    open: float
    high: float
    low: float
    close: float


def parse_completed_candles(raw: List[List[Any]], now_ms: int) -> List[Candle]:
    """
    Current KuCoin Classic Futures Kline order:
        [start, open, high, low, close, volume, turnover]

    We also validate each OHLC row so an API format change fails closed.
    """
    candles: List[Candle] = []

    for row in raw:
        if len(row) < 5:
            continue

        start_ms = normalize_ms(row[0])
        o = float(row[1])
        h = float(row[2])
        l = float(row[3])
        c = float(row[4])

        if not (
            math.isfinite(o)
            and math.isfinite(h)
            and math.isfinite(l)
            and math.isfinite(c)
        ):
            continue

        # Fail closed if response column order ever changes.
        if h < max(o, c) or l > min(o, c) or h < l:
            raise KuCoinError(
                f"Unexpected KuCoin OHLC layout: {row[:5]}"
            )

        close_ms = start_ms + CANDLE_SECONDS * 1000

        # Ignore any still-forming 4h candle.
        if close_ms <= now_ms:
            candles.append(Candle(start_ms, o, h, l, c))

    candles.sort(key=lambda x: x.start_ms)

    # Remove any duplicate timestamps.
    dedup: Dict[int, Candle] = {x.start_ms: x for x in candles}
    return [dedup[k] for k in sorted(dedup)]


def wilder_atr(candles: List[Candle], n: int = ATR_N) -> List[Optional[float]]:
    """
    Matches pandas ewm(alpha=1/n, adjust=False, min_periods=n) used in
    the research backtester.
    """
    if not candles:
        return []

    trs: List[float] = []

    for i, bar in enumerate(candles):
        if i == 0:
            tr = bar.high - bar.low
        else:
            pc = candles[i - 1].close
            tr = max(
                bar.high - bar.low,
                abs(bar.high - pc),
                abs(bar.low - pc),
            )
        trs.append(tr)

    alpha = 1.0 / n
    ewma: Optional[float] = None
    out: List[Optional[float]] = []

    for i, tr in enumerate(trs):
        if ewma is None:
            ewma = tr
        else:
            ewma = (1.0 - alpha) * ewma + alpha * tr

        out.append(ewma if i + 1 >= n else None)

    return out


@dataclass
class Signal:
    side: Optional[str]   # "buy", "sell", or None
    candle_start_ms: int
    candle_close_ms: int
    atr: float
    close: float
    prev_close: float


def compute_signal(candles: List[Candle]) -> Signal:
    if len(candles) < ATR_N + 2:
        raise KuCoinError(f"Need at least {ATR_N + 2} completed candles.")

    atrs = wilder_atr(candles, ATR_N)
    bar = candles[-1]
    prev = candles[-2]
    atr_now = atrs[-1]

    if atr_now is None or atr_now <= 0:
        raise KuCoinError("ATR unavailable.")

    threshold = TRIGGER_ATR * atr_now

    side: Optional[str] = None
    if bar.close > prev.close + threshold:
        side = "buy"
    elif bar.close < prev.close - threshold:
        side = "sell"

    return Signal(
        side=side,
        candle_start_ms=bar.start_ms,
        candle_close_ms=bar.start_ms + CANDLE_SECONDS * 1000,
        atr=atr_now,
        close=bar.close,
        prev_close=prev.close,
    )


# ============================================================
# POSITION / ORDER SIZING
# ============================================================

def current_position_qty(positions: List[Dict[str, Any]], symbol: str) -> float:
    qty = 0.0
    for p in positions:
        if p.get("symbol") != symbol:
            continue
        if not bool(p.get("isOpen", False)):
            continue
        q = float(p.get("currentQty", 0) or 0)
        qty += q
    return qty


def deterministic_client_oid(symbol: str, candle_start_ms: int) -> str:
    raw = f"atr075-v1|{symbol}|{candle_start_ms}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


@dataclass
class OrderPlan:
    symbol: str
    side: str
    contracts: int
    entry_ref: Decimal
    stop: Decimal
    take_profit: Decimal
    risk_cash: Decimal
    notional: Decimal
    client_oid: str


def make_order_plan(
    *,
    symbol: str,
    side: str,
    signal: Signal,
    equity_usdt: float,
    contract: Dict[str, Any],
    ticker: Dict[str, Any],
) -> OrderPlan:
    multiplier = d(contract["multiplier"])
    tick_size = d(contract["tickSize"])
    lot_size = d(contract.get("lotSize", 1))

    if multiplier <= 0 or tick_size <= 0 or lot_size <= 0:
        raise KuCoinError(f"{symbol}: invalid contract multiplier/tick/lot size.")

    if side == "buy":
        entry_ref = d(ticker.get("bestAskPrice") or ticker["price"])
        raw_stop = entry_ref - d(STOP_ATR * signal.atr)
        raw_tp = entry_ref + d(STOP_ATR * TP_R * signal.atr)

        # Round protection away from the entry.
        stop = floor_to_step(raw_stop, tick_size)
        tp = ceil_to_step(raw_tp, tick_size)
    else:
        entry_ref = d(ticker.get("bestBidPrice") or ticker["price"])
        raw_stop = entry_ref + d(STOP_ATR * signal.atr)
        raw_tp = entry_ref - d(STOP_ATR * TP_R * signal.atr)

        stop = ceil_to_step(raw_stop, tick_size)
        tp = floor_to_step(raw_tp, tick_size)

    if entry_ref <= 0 or stop <= 0 or tp <= 0:
        raise KuCoinError(f"{symbol}: non-positive entry/SL/TP.")

    stop_distance = abs(entry_ref - stop)

    sleeve_equity = d(equity_usdt) / d(len(SYMBOLS))
    risk_cash = sleeve_equity * d(RISK_PER_SLEEVE)

    # Linear USDT futures:
    # 1 contract controls multiplier base-asset units.
    risk_per_contract = multiplier * stop_distance
    if risk_per_contract <= 0:
        raise KuCoinError(f"{symbol}: invalid risk per contract.")

    contracts_by_risk = floor_to_step(
        risk_cash / risk_per_contract,
        lot_size,
    )

    max_notional = sleeve_equity * d(MAX_LEVERAGE)
    notional_per_contract = multiplier * entry_ref
    contracts_by_leverage = floor_to_step(
        max_notional / notional_per_contract,
        lot_size,
    )

    contracts_dec = min(contracts_by_risk, contracts_by_leverage)

    market_max = contract.get("marketMaxOrderQty")
    if market_max is not None:
        contracts_dec = min(contracts_dec, d(market_max))

    contracts_dec = floor_to_step(contracts_dec, lot_size)

    if contracts_dec < lot_size:
        raise KuCoinError(
            f"{symbol}: calculated size below one lot. "
            f"risk_cash={risk_cash}, risk/contract={risk_per_contract}"
        )

    # KuCoin futures size is contract count. Current listed USDTM contracts
    # use integer lots, so reject non-integer output instead of guessing.
    if contracts_dec != contracts_dec.to_integral_value():
        raise KuCoinError(
            f"{symbol}: non-integer contract count {contracts_dec}; "
            "review lotSize handling for this contract."
        )

    contracts = int(contracts_dec)
    notional = d(contracts) * multiplier * entry_ref

    return OrderPlan(
        symbol=symbol,
        side=side,
        contracts=contracts,
        entry_ref=entry_ref,
        stop=stop,
        take_profit=tp,
        risk_cash=risk_cash,
        notional=notional,
        client_oid=deterministic_client_oid(symbol, signal.candle_start_ms),
    )


def order_body(plan: OrderPlan) -> Dict[str, Any]:
    if plan.side == "buy":
        trigger_up = plan.take_profit
        trigger_down = plan.stop
    else:
        # For a short, upward trigger is the stop-loss and downward
        # trigger is the take-profit.
        trigger_up = plan.stop
        trigger_down = plan.take_profit

    return {
        "clientOid": plan.client_oid,
        "side": plan.side,
        "symbol": plan.symbol,
        "leverage": MAX_LEVERAGE,
        "type": "market",
        "reduceOnly": False,
        "marginMode": MARGIN_MODE,
        "positionSide": POSITION_SIDE,
        "size": plan.contracts,
        "triggerStopUpPrice": decimal_str(trigger_up),
        "triggerStopDownPrice": decimal_str(trigger_down),
        "stopPriceType": STOP_PRICE_TYPE,
        "remark": "atr075_4h_v1",
    }


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    kc = KuCoinFutures()

    try:
        now_ms = kc.sync_time()
    except Exception as exc:
        log(f"FATAL: cannot reach KuCoin server time: {exc}")
        return 2

    if not kc.service_open():
        log("KuCoin Futures service status is not open. No action.")
        return 0

    if LIVE_TRADING and not kc.has_private_credentials:
        log("FATAL: LIVE_TRADING=YES but API credentials are missing.")
        return 2

    # Private read access is useful even in dry-run.
    if kc.has_private_credentials:
        try:
            equity = kc.account_equity()
            positions = kc.positions()
        except Exception as exc:
            log(f"FATAL: cannot read KuCoin account/positions: {exc}")
            return 2
    else:
        equity = DRY_RUN_EQUITY_USDT
        positions = []
        log(
            "No API credentials: public-data dry-run only. "
            f"Assuming equity={equity:.2f} USDT and no open positions."
        )

    log(
        f"Mode={'LIVE' if LIVE_TRADING else 'DRY-RUN'} | "
        f"equity={equity:.2f} USDT | "
        f"strategy=ATR0.75 4h / stop 2ATR / TP 1.5R / "
        f"risk 2.5% per 1/5 sleeve / cap {MAX_LEVERAGE}x"
    )

    for symbol in SYMBOLS:
        log(f"{symbol}: checking")

        # KuCoin itself is our state store.
        qty = current_position_qty(positions, symbol)
        if abs(qty) > 0:
            log(f"{symbol}: existing position qty={qty}; skip new entry.")
            continue

        try:
            contract = kc.contract(symbol)

            if str(contract.get("marketStage", "NORMAL")).upper() != "NORMAL":
                log(f"{symbol}: marketStage={contract.get('marketStage')}; skip.")
                continue

            raw = kc.klines(symbol, now_ms)
            candles = parse_completed_candles(raw, now_ms)
            signal = compute_signal(candles)

            age_sec = max(0, (now_ms - signal.candle_close_ms) / 1000.0)

            log(
                f"{symbol}: last closed bar age={age_sec:.0f}s | "
                f"close={signal.close:.8g} prev={signal.prev_close:.8g} "
                f"ATR20={signal.atr:.8g} signal={signal.side or 'none'}"
            )

            if signal.side is None:
                continue

            # Don't chase a signal hours later after a power/Wi-Fi outage.
            if age_sec > ENTRY_GRACE_SECONDS:
                log(
                    f"{symbol}: signal is stale (> {ENTRY_GRACE_SECONDS}s); "
                    "do not enter."
                )
                continue

            ticker = kc.ticker(symbol)

            plan = make_order_plan(
                symbol=symbol,
                side=signal.side,
                signal=signal,
                equity_usdt=equity,
                contract=contract,
                ticker=ticker,
            )

            body = order_body(plan)

            log(
                f"{symbol}: PLAN {plan.side.upper()} {plan.contracts} contracts | "
                f"entry_ref={decimal_str(plan.entry_ref)} | "
                f"SL={decimal_str(plan.stop)} | "
                f"TP={decimal_str(plan.take_profit)} | "
                f"notional≈{plan.notional:.2f} USDT | "
                f"risk≈{plan.risk_cash:.2f} USDT | "
                f"clientOid={plan.client_oid}"
            )

            if not LIVE_TRADING:
                log(f"{symbol}: DRY-RUN payload={json.dumps(body, separators=(',', ':'))}")
                continue

            # One atomic KuCoin request contains entry + both protective triggers.
            result = kc.place_attached_market_order(body)
            log(
                f"{symbol}: ORDER ACCEPTED | "
                f"orderId={result.get('orderId')} clientOid={result.get('clientOid')}"
            )

        except Exception as exc:
            # Fail isolated per symbol. The script does not invent fallback orders.
            log(f"{symbol}: ERROR: {exc}")
            continue

    log("Done. Process exits; no WebSocket/background loop remains running.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
