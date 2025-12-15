# TradeBot

An automated cryptocurrency trading bot for Bitkub exchange featuring multiple trading strategies and backtesting capabilities.

## Overview

TradeBot is a Python-based trading automation system designed for the Bitkub cryptocurrency exchange. It provides:

- **Multiple Trading Strategies**: Grid trading, MACD, EMA, RSI, Z-score, and hybrid strategies
- **Backtesting Framework**: Historical data analysis to test strategy performance
- **Trend Detection**: Real-time technical analysis with multiple timeframes
- **Position Tracking**: Cost tracking and P&L management
- **Colored Logging**: Enhanced terminal output with color-coded information

## Features

### Trading Strategies

- **Grid Trading** (`Strategy/Grid_trade.py`): Classical grid bot with hysteresis, support for any BASE/QUOTE pair
- **MACD Strategy** (`Strategy/MACD_trade.py`): MACD-based entry/exit signals
- **MACD26ADX20** (`Strategy/MACD26ADX20_trade.py`): Combined MACD and ADX indicators
- **EMA 50/200** (`Strategy/EMA50_200.py`): Exponential moving average crossover strategy
- **RSI Strategy** (`Strategy/Rsi_trade.py`): Relative strength index trading signals
- **Z-Score Strategy** (`Strategy/Z_trade.py`): Statistical mean reversion strategy

### Analysis & Backtesting

- **Trend Detection** (`Trend_detection.py`): Multi-timeframe technical analysis with support for:
  - 1m, 5m, 15m, 30m, 1h, 4h, 1d timeframes
  - Multiple currency pairs (XRP_THB, BTC_THB, ETH_THB, USDT_THB, SOL_THB, ADA_THB, BNB_THB)
  - VWAP and normalized trade data
  
- **Grid Backtesting** (`Backtesting/Grid_backtest.py`): Historical simulation of grid trading strategy
- **MACD Backtesting** (`Backtesting/MACD26_backtest.py`): Historical simulation of MACD strategy

## Project Structure

```
TradeBot/
├── Strategy/                 # Live trading strategies
│   ├── Grid_trade.py        # Grid trading bot
│   ├── MACD_trade.py        # MACD strategy
│   ├── MACD26ADX20_trade.py # MACD + ADX hybrid
│   ├── EMA50_200.py         # EMA crossover
│   ├── Rsi_trade.py         # RSI strategy
│   ├── Z_trade.py           # Z-score strategy
│   ├── Cost.json            # Position tracking data
│   └── Cost_USDT.json       # USDT position tracking
│
├── Backtesting/             # Strategy backtesting modules
│   ├── Grid_backtest.py     # Grid strategy historical test
│   └── MACD26_backtest.py   # MACD strategy historical test
│
├── Trend_detection.py       # Technical analysis & trend detection
├── Cost.json                # Master cost tracking file
└── config/
    └── color.json           # Terminal color configuration

```

## Requirements

- Python 3.7+
- Dependencies:
  - `requests` - HTTP requests for API communication
  - `pandas` - Data manipulation and analysis
  - `pandas-ta` - Technical analysis indicators
  - `numpy` - Numerical computations
  - `python-dotenv` - Environment variable management
  - `psutil` - System monitoring
  - `tabulate` - Pretty table formatting

## Installation

1. Clone the repository:
   ```bash
   git clone <repository-url>
   cd TradeBot
   ```

2. Create a virtual environment (optional but recommended):
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Configure environment variables:
   ```bash
   cp .env.example .env
   # Edit .env with your Bitkub API credentials
   export BITKUB_API_KEY="your_api_key"
   export BITKUB_API_SECRET="your_api_secret"
   ```

## Configuration

### Environment Variables

Create a `.env` file in the project root:

```
BITKUB_API_KEY=your_api_key_here
BITKUB_API_SECRET=your_api_secret_here
```

### Strategy Configuration

Each strategy can be customized through parameters:

- **ORDER_NOTIONAL_THB**: Order size in THB
- **SYMBOL**: Trading pair (e.g., "XRP_THB", "BTC_THB")
- **REFRESH_SEC**: Update interval in seconds
- **DRY_RUN**: Set to `True` for testing, `False` for live trading
- **SLIPPAGE_BPS**: Slippage tolerance in basis points
- **FEE_RATE**: Exchange fee rate (default 0.25%)

### Color Configuration

Terminal colors are configured in `config/color.json`:

```json
{
    "UP": "\u001b[92m",      // Green
    "DOWN": "\u001b[91m",    // Red
    "SIDEWAYS": "\u001b[93m",// Yellow
    "UNKNOWN": "\u001b[2m",  // Dim
    "RESET": "\u001b[0m"     // Reset
}
```

## Usage

### Grid Trading Bot

```bash
python Strategy/Grid_trade.py
```

The grid bot will:
- Buy when price drops below grid lines
- Sell when price rises above grid lines
- Track positions and P&L in Cost.json
- Apply hysteresis to reduce false signals at grid boundaries

### MACD Trading Strategy

```bash
python Strategy/MACD_trade.py
```

### Trend Detection Analysis

```bash
python Trend_detection.py
```

Shows multi-timeframe technical analysis:
- MACD signals
- ATR (Average True Range)
- Trend direction (UP/DOWN/SIDEWAYS)
- Support/Resistance levels

### Backtesting

Run historical simulations to evaluate strategy performance:

```bash
python Backtesting/Grid_backtest.py
python Backtesting/MACD26_backtest.py
```

## Technical Indicators Used

- **MACD** (Moving Average Convergence Divergence): Trend momentum
- **EMA** (Exponential Moving Average): Trend following
- **RSI** (Relative Strength Index): Momentum and overbought/oversold levels
- **ADX** (Average Directional Index): Trend strength
- **ATR** (Average True Range): Volatility measurement
- **VWAP** (Volume Weighted Average Price): Fair value price
- **Z-Score**: Statistical deviation from mean for mean reversion

## Key Features

### Position Tracking
- Tracks average cost per position
- Calculates realized and unrealized P&L
- Persists state to JSON files (Cost.json)
- Supports multiple trading pairs

### Robust API Integration
- Automatic retry with exponential backoff
- Request timeout handling
- Server time synchronization
- Detailed HTTP debugging options

### Hysteresis & Filtering
- Minimum movement requirement from last trade price
- Reduces whipsaws and false signals
- Configurable via `MIN_MOVE_PCT_FROM_LAST_TRADE`

### Multi-Timeframe Analysis
- Analyze trends across different timeframes
- Support for 1m to daily data
- Multiple currency pairs analysis in parallel

## Risk Management

⚠️ **Important**: 

- Always test strategies in **DRY_RUN mode** before live trading
- Use backtesting to validate strategies before deploying
- Understand the risks of algorithmic trading
- Monitor positions and adjust parameters based on market conditions
- Keep API credentials secure and never commit `.env` to version control

## Backtesting Results

The backtesting modules use historical OHLCV data from Bitkub's TradingView API:

```
https://api.bitkub.com/tradingview/history
```

## Disclaimer

**This is automated trading software. Use at your own risk.** 

- Past performance does not guarantee future results
- Cryptocurrency markets are highly volatile
- Thoroughly test all strategies before live trading
- Never risk more than you can afford to lose
- This software is provided without warranties
