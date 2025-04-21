# Stablecoin Grid Trading Bot README

## Overview

This Python script implements a **grid trading strategy** for stablecoin pairs (e.g., FDUSD/USDC) on the **Binance Testnet**. It simulates trading by placing buy and sell orders at predefined price levels (a "grid") to profit from small price fluctuations in a stablecoin pair. The script fetches order book snapshots, manages a virtual portfolio, logs trades, and reports performance metrics like profit and ROI in a risk-free test environment.

![image](https://github.com/user-attachments/assets/4e0f131f-4559-4fce-9ed7-e7e669e5ec13)


## Installation

1. Clone or download the script to your local machine.

2. Create a `requirements.txt` file with the following content:

   ```text
   ccxt
   pandas
   numpy
   python-dotenv
   ```

3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Create an `.env` file in the script's working directory with your Binance Testnet API keys:

   ```text
   api_key=your_testnet_api_key
   api_secret=your_testnet_api_secret
   ```

   Replace `your_testnet_api_key` and `your_testnet_api_secret` with your actual Binance Testnet credentials. If using the provided script's keys, ensure they are valid.

5. Create a `config.txt` file with the input parameters (one per line, in order):

   ```text
   FDUSDUSDC
   10000
   1
   0.9980
   0.9990
   0.0001
   250.0
   10000.0
   0
   11
   10
   ```

6. Stop

   ```bash
   pkill -f testnet_spot.py
   ```

   

## Execution Flow

With the example inputs above, the script executes as follows:

1. **Setup Exchange**:

   - Initializes a Binance Testnet exchange instance with predefined API keys.
   - Verifies `FDUSDUSDC` is available.

2. **Reset Balance**:

   - Converts all assets (FDUSD, USDC, BTC) to \~10,000 USDC and minimal FDUSD.
   - Verifies USDC balance ≥ 100.

3. **Fetch OHLCV Data**:

   - Retrieves 1-hour OHLCV data for `FDUSDUSDC` (for context, not directly used in trading).

4. **Initialize Trader**:

   - Creates a `StablecoinGridTrader` with the input parameters (grid: 0.9980–0.9990, step: 0.0001, etc.).
   - Updates initial balance (\~10,000 USDC, 0 FDUSD).

5. **Process Snapshots**:

   - Fetches 10,000 order book snapshots (one every 1 second).
   - For each snapshot:
     - Fetches order book (100 bids/asks).
     - Calculates current price (average of best bid/ask).
     - Places up to 11 buy orders at grid levels (e.g., 0.9980, 0.9981, …) if funds allow.
     - Executes buy orders if the best ask matches a pending buy, then places a sell order at the next level (e.g., 0.9981).
     - Executes sell orders if the best bid matches a pending sell, logging profit.
     - Logs snapshot details (best bid, ask, price).
     - Every 10 snapshots, logs a recap (portfolio, profit, ROI, trades).

6. **Finalize**:

   - Sells any remaining FDUSD at the final price.
   - Logs final results (total trades, profit, fees, portfolio).

7. **Error/Interrupt Handling**:

   - Logs API errors and retries or stops after too many failures.
   - Exits gracefully on Ctrl+C, cleaning up resources.
