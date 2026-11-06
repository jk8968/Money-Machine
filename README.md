# Money Machine
A collection of bots used for constructing and monitoring my portfolio.

The `etoro_breakout_bot` automatically manages a momentum portfolio on eToro. It runs once per week, identifies assets breaking above their **50-week high** and selects the single strongest asset based on **26-week momentum**. It exits when the weekly close falls below its **20-week** low or another asset becomes the top-ranked choice.

The `etoro_gold-btc-silver_bot` automatically manages a gold, silver and Bitcoin momentum portfolio on eToro. It runs once per week and compares the **8-week returns** of **GLD, SLV and BTC**, allocating 100% of equity to the strongest asset if its return is positive. If all returns are non-positive, it moves to cash. Positions are automatically rotated as rankings change.

The `etoro_momentum_bot` automatically manages a momentum portfolio on eToro. It runs once per week, ranks assets monthly using **20% × 3-month, 40% × 6-month and 40% × 12-month returns**, and selects the **top 7** assets, each with allocation of 14.29% of equity. New positions must pass the **SMA50 and SMA200 entry filters**, while existing positions are closed when the weekly candle opens and closes below **SMA50**.

The `valuation_template` contains an Excel valuation model for estimating a realistic value of a company using **Discounted Cash Flow (DCF)** model.

These projects are primarily built for fun, portfolio management and automation.
