# 8-Week Gold / Silver / Bitcoin Momentum Bot

A bot for managing a portfolio using **relative and absolute momentum** across Gold, Silver, and Bitcoin.

The bot runs once per week and compares the **8-week return** of:

- **Gold** (GLD)
- **Silver** (SLV)
- **Bitcoin** (BTC)

Each week, the bot selects the asset with the highest 8-week return.

The portfolio is allocated approximately **100% to the strongest asset**, while keeping the configured small cash reserve.

## Strategy

The strategy is:

**REL_ABS_GOLD_SILVER_BTC_8W**

For every completed week, the bot:

1. Calculates the 8-week return of **Gold (GLD)**.
2. Calculates the 8-week return of **Silver (SLV)**.
3. Calculates the 8-week return of **Bitcoin (BTC)**.
4. Ranks the three assets by their 8-week return.
5. Selects the asset with the highest return.
6. Holds that asset only if its 8-week return is **positive**.
7. If all three assets have returns of **0% or less**, the portfolio moves to **cash**.

This combines:

- **Relative momentum** — choose the strongest asset.
- **Absolute momentum** — only invest when the strongest asset has positive momentum.

## Weekly Rebalancing

The bot is intended to run **once per week**.

Signals are calculated using completed weekly data.

The strategy uses **Friday-ending weekly closes** and compares the latest completed weekly close with the close from 8 weeks earlier.

The signal from the completed week is executed during the **next executable market session**.

Normally:

**Friday close → calculate signal → trade on the next market session**

If the current winner changes, the bot:

1. Closes the previous strategy holding.
2. Waits for the eToro close to settle when necessary.
3. Uses the available capital to buy the new winner.

If the current winner remains the same, the bot keeps the existing position and avoids unnecessary trading.

## Cash Rule

The strategy does not always have to be invested.

If the highest 8-week return among Gold, Silver, and Bitcoin is not positive, the target allocation is:

**100% cash**

The bot closes existing strategy positions and waits for a future weekly signal to become positive.

## Portfolio Allocation

Unlike the previous 3/6/12 momentum strategy, this strategy does not hold multiple assets at the same time.

The target portfolio is:

- **1 asset at approximately 100% of strategy capital**, or
- **100% cash**

A small configurable cash reserve is retained for execution purposes.

## Strategy Universe

The strategy uses only three assets:

- **GLD** — Gold ETF
- **SLV** — Silver ETF
- **BTC** — Bitcoin

The universe is intentionally limited because these are the assets used by the tested:

**REL_ABS_GOLD_SILVER_BTC_8W**

strategy.

## Exit Rule

There is no separate moving-average stop-loss.

A position is exited when the weekly momentum signal changes.

The bot exits when:

- another asset becomes the strongest positive 8-week momentum asset, or
- the best 8-week return falls to **0% or below**.

Therefore, the same momentum model controls both **entries and exits**.

## No Moving Average Filters

This strategy does **not** use:

- 50-week moving-average entry filters,
- 200-week moving-average entry filters,
- 50-week moving-average stop-losses.

Those rules belonged to the previous 3/6/12 momentum strategy and are not part of **REL_ABS_GOLD_SILVER_BTC_8W**.

## eToro Execution

The bot retains the existing eToro execution framework.

It includes:

- **eToro DEMO account execution**
- **LONG positions only**
- **x1 leverage only**
- **REAL or CFD settlement where supported**
- Pending opening-order detection
- Pending closing-order detection
- Duplicate-order protection
- Close-settlement handling before replacement purchases
- Automatic cleanup of non-strategy positions
- Automatic cleanup of unsupported leveraged or short positions
- Configurable cash reserve
- eToro minimum-position checks
- API write-rate protection
- Protection against retrying trades when execution state is unknown

## No External Database

The bot does not require an external database or local state file.

Each run reconstructs the required state from:

- current eToro positions,
- current pending opening orders,
- current pending closing orders,
- current account equity and available cash,
- current market data,
- the latest completed weekly momentum signal.

This makes the bot safe to run repeatedly without relying on a separate portfolio-state database.

## Market Data

Market data is downloaded using **Yahoo Finance / yfinance**.

The strategy uses:

- `GLD` for Gold,
- `SLV` for Silver,
- `BTC-USD` for Bitcoin market data.

The corresponding eToro instruments are resolved through the eToro instrument catalogue.

## Dependencies

Install the required Python packages with:

```bash
pip install requests pandas numpy yfinance
