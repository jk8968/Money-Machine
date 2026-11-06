# Single Asset Beakout Bot

A bot for managing a portfolio using weekly breakout and momentum strategy.

The bot runs once per week and identifies assets whose weekly closing price breaks above the high of the previous 50 weeks, provided the closing price is also above the 100-week moving average. Eligible assets are then ranked by their 26-week returns, and the bot selects the highest-ranked asset, allocating 100% of equity to it. If no assets qualify, the portfolio remains in cash.

Exit rule: A position is closed when the completed weekly candle closes below the low of the previous 20 weeks, or when another eligible asset takes the top position in the momentum ranking.

The script currently tracks 91 assets, including: 
- **Crypto** (BTC, ETH, SOL, BNB, XRP, DOT, LINK),
- **ETFs** (SPY, QQQ, XDJP.L, 2800.HK, GLD, SLV, PALL, PPLT, XLE, COPX, PSLV, URA, SIL, IXC)
- **Stocks:** (PHG, TM, AAPL, META, NFLX, KO, NESM, PEP, BABA, MCD, ADBE, SHOP, NKE, SPOT, 0700.HK, OR.PA, 1810, SIE.DE, INTC, IBKR, DELL, MDLZ, 7974.T, ADSK, VOLV-B.ST, VOW.DE, GRMN, HEIA.NV, NVDA, MCHP, HPQ, EBAY, PUM, MGA, ZBRA, AMZN, MSFT, SMSN, AMD, ADS, TSM, MSI, RIO, CCJ, GOOGL, JNJ, SAP, CSCO, DIS, TXN, SND, BHP, SWK, 0992.HK, PAAS, WPM, AEM, NEM, B, FCX, ALB, XOM, FRES.L, AG, 01211.HK)

The full list can be expanded manually and is configured in **RAW_UNIVERSE** variable.

## Setup
Download the files from the repositroy and put them in a permanent **folder**, where they will be able to reside without moving. 

Add your eToro **API credentials** under **ETORO_API_KEY = "your_key"** and **ETORO_USER_KEY = "your_user_key"**. 
You can find those keys under [eToro web](https://www.etoro.com/settings/trade).

Run **install.bat** file, this will install python if its not yet installed and install the libraries that are needed for the script to run

To make the bot run automatically every week search for **Task Scheduler** in windows. Create new task using **Create Task**.  

<p align="center">
<img width="1267" height="928" alt="image" src="https://github.com/user-attachments/assets/f0ad4f00-3c15-4eb9-b42a-b82f503f96e9" />
</p>

When there under General press **Run with highest privileges**, otherwise administrator privileges might prevent the script from running.

<p align="center">
<img width="636" height="486" alt="image" src="https://github.com/user-attachments/assets/45938ea7-f653-4354-b2c1-7c94ff970334" />
</p>

Under **Triggers**, press New and select the schedule. Let it run **Weekly** on **Monday** ideally, for European time choose something around **16:00**. The US Markets open around 15.30 under Ljubljana time.

<p align="center">
<img width="590" height="518" alt="image" src="https://github.com/user-attachments/assets/628fbd23-8018-43db-ad08-1c8cb12a0b3c" />
</p>

Under **Actions**, choose New, select Start a program, and press **Browse**. Then choose the **run_etoro_bot.bat** file you download, this is the executable file. You need to **modify** the run_etoro_bot.bat file, to **point to the folder** in which you saved the file!

<p align="center">
<img width="459" height="504" alt="image" src="https://github.com/user-attachments/assets/0aa3ad2b-567d-4505-bc08-d14b4d2999a8" />
</p>

<p align="center">
<img width="908" height="150" alt="image" src="https://github.com/user-attachments/assets/2bcc70d5-bf50-47e5-9b53-781c1f338bda" />
</p>

Under **Conditions** choose **Start only if the following network connection is avalible: Any connection**. This will run the script only when PC is connected to the internet. And under **Settings** choose **Run task as soon as possible after the schedule is missed**.





