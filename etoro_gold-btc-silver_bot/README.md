# Gold/Silver/Bitcoin Rotation Bot

A bot for managing a portfolio using relative and absolute momentum across gold, silver and Bitcoin.

The bot runs once per week and compares the **8-week returns** of Gold, Silver and Bitcoin. 
It selects the asset with the highest return and allocates 100% of account equity to it, provided its 8-week return is **positive**. If all three returns are zero or negative, portfolio moves to cash.

Exit rule: A position is closed when another asset achieves the highest positive 8-week return, when all three assets have non-positive 8-week returns, or when both the weekly candle of the highest-ranked asset fall below its 40-week moving average.

The script tracks exactly three assets: **Gold (GLD), Silver (SLV), Bitcoin (BTC)**

The asset universe is configured in the **RAW_UNIVERSE** variable.

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




