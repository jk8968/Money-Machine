# Money Machine
A collection of ools used for constructing and monitoring my portfolio.

`app` folder contains Android and iOS app for helping with my portfolio decisions. It helps identify possible buying opportunities. It ranks assets on the watchlist based on momentum and checks whether an asset is below its **200-week moving average** for a possible below value buy. 

The `etoro_momentum_bot` automatically manages a momentum portfolio on eToro. It runs once and week and ranks assets using **3/6/12-month momentum**, automatically buys and rebalances the portfolio and applies the strategy's stop-loss rules.

The `kucoin_trading_bot_ATR` is an automated crypto trading bot using an **ATR-based strategy on the 4-hour timeframe**. It  automatically handles entries, position sizing, stop losses and take profits.

The `valuation_template` contains an Excel valuation model for estimating a realistic value of a company using **Discounted Cash Flow (DCF)** model.

These projects are primarily built for research, portfolio management and automation.
