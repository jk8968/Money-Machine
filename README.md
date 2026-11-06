# Money Machine
A collection of bots used for constructing and monitoring my portfolio.

The `etoro_breakout_bot` automatically manages a momentum portfolio on eToro. It runs once per week, identifies assets breaking above their **50-week high** and selects the single strongest asset based on **26-week momentum**. It exits when the weekly close falls below its **20-week** low or another asset becomes the top-ranked choice.

The `etoro_gold-btc-silver_bot` automatically manages a gold, silver and Bitcoin momentum portfolio on eToro. It runs once per week and compares the **8-week returns** of GLD, SLV and BTC, allocating 100% of equity to the strongest asset if its return is positive. If all returns are non-positive, it moves to cash. Positions are automatically rotated as rankings change.

The `etoro_momentum_bot` automatically manages a momentum portfolio on eToro. It runs once and week and ranks assets using **3/6/12-month momentum**, automatically buys and rebalances the portfolio and applies the strategy's stop-loss rules.

The `valuation_template` contains an Excel valuation model for estimating a realistic value of a company using **Discounted Cash Flow (DCF)** model.

These projects are primarily built for fun, portfolio management and automation.
