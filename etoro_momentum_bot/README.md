# 3/6/12 Momentum Bot

A bot for managing a portfolio using momentum.

The bot runs once per week and ranks assets using: 20% × 3-month returns, 40% × 6-month returns and 40% × 12-month returns. It then selects the top 7 assets, each position is roughly 14.29% of account equity.

Exit rule
A position is closed when weekly candle opens and closes below 50 weekly MA.

The script currently tracks 91 assets, including: Crypto (BTC, ETH, SOL, BNB, XRP, DOT, LINK), ETFs (SPYm QQQ, XDJP.L, 2800.HK, GLD, SLV, PALL, PPLT, XLE, COPX, PSLV, URA, SIL, IXC) and Stocks (AAPL, MSFT, NVDA, META, AMZN, GOOGL, AMD, TSM, BABA, TM, RIO, BHP, XOM, CCJ and others). 
The full list can be expanded manually and is configured in RAW_UNIVERSE variable.

# Setup
Donwload the files from the repositroy and put them in a permanent folder, where they will be able to reside without moving. 

Add your eToro API credentials under ETORO_API_KEY = "your_key" and ETORO_USER_KEY = "your_user_key". You can find those keys under etoro -> settings -> .

To make the bot run automatically every week search for Task Scheduler in windows. When there create new task using Create Task.  

<img width="1267" height="928" alt="image" src="https://github.com/user-attachments/assets/f0ad4f00-3c15-4eb9-b42a-b82f503f96e9" />
When there under General press Run with highest privileges, otherwise administrator privileges might prevent the script from running.
<img width="636" height="486" alt="image" src="https://github.com/user-attachments/assets/45938ea7-f653-4354-b2c1-7c94ff970334" />
Under Triggers, press New and select the schedule. Let it run Weekly on Monday ideally, for European time choose something around 16:00. The US Markets open around 15.30 under Ljubljana time.
<img width="590" height="518" alt="image" src="https://github.com/user-attachments/assets/628fbd23-8018-43db-ad08-1c8cb12a0b3c" />
Under Actions, choose New, select Start a program, and press Browse. Then choose the .bat file you download, this is the executable file. You need to modify the run_etoro_bot.bat file, to point to the folder in which you saved the file!
<img width="459" height="504" alt="image" src="https://github.com/user-attachments/assets/0aa3ad2b-567d-4505-bc08-d14b4d2999a8" />
<img width="908" height="150" alt="image" src="https://github.com/user-attachments/assets/2bcc70d5-bf50-47e5-9b53-781c1f338bda" />
Under Conditions choose Start only if the following network connection is avalible: Any connection. This will run the script only when PC is connected to the internet. And under Settings chose: Run task as soon as possible after the schedule is missed.




