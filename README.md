# Kalshi AI Trading Bot

Automated prediction market trading bot that combines AI probability estimation,
news analysis, and technical analysis to identify mispriced markets on Kalshi.

## Quick Setup

### 1. Activate the virtual environment
```bash
cd /Users/sk8er02/Kelshi/kalshi_bot
source venv/bin/activate
```

### 2. Get your Kalshi API credentials
1. Log in at [kalshi.com](https://kalshi.com)
2. Go to **Profile → API Keys**
3. Create a new API key — download the **private key PEM file**
4. Copy it to `keys/private_key.pem`
5. Note your **API Key ID**

### 3. Get your OpenRouter API key
1. Sign up at [openrouter.ai](https://openrouter.ai)
2. Go to **Settings → API Keys** → create a key
3. Free tier includes DeepSeek R1 and Llama 3.3 (200 req/day each)

### 4. Configure your .env file
Edit `.env` and fill in your credentials:
```bash
KALSHI_API_KEY_ID=your_key_id_here
KALSHI_PRIVATE_KEY_PATH=/Users/sk8er02/Kelshi/kalshi_bot/keys/private_key.pem
OPENROUTER_API_KEY=sk-or-v1-your-key-here
```

### 5. Run in DRY RUN mode first (no real orders)
```bash
DRY_RUN=true python main.py
```

Watch the logs for 30+ minutes to see it discover markets and generate signals.

### 6. Go live (when you're confident)
Edit `.env` and set:
```
DRY_RUN=false
```
Then run:
```bash
python main.py
```

---

## Risk Controls

| Limit | Default | Configure in |
|-------|---------|--------------|
| Max per trade | $5 | `MAX_TRADE_COST_CENTS=500` in `.env` |
| Daily spend cap | $50 | `MAX_DAILY_SPEND_CENTS=5000` in `.env` |
| Kill switch | -$20 loss | `DAILY_LOSS_KILL_SWITCH_CENTS=2000` in `.env` |
| Max positions | 5 simultaneous | `config.py` |
| Profit target | +15% | `config.py` |
| Stop loss | -20% | `config.py` |

## How It Works

Every 5 minutes:
1. Fetches all open Kalshi markets and scores them by opportunity
2. Fetches relevant news for top 10 markets via RSS (Google News, Reuters, AP)
3. Asks AI (DeepSeek via OpenRouter) to estimate probability for top 5 markets
4. Compares AI estimate to market price — if edge > 8 cents, checks TA
5. Places a **limit order** (not market order) if all checks pass

Every 15 minutes:
- Reviews all open positions
- Closes at +15% profit target or -20% stop loss
- Closes positions 60 minutes before market resolution

## File Structure

```
kalshi_bot/
├── main.py              # Entry point + scheduler
├── config.py            # All configuration
├── .env                 # Your API keys (never commit this)
├── keys/
│   └── private_key.pem  # Kalshi RSA key (never commit this)
├── kalshi/              # Kalshi API wrapper
├── analysis/            # News, TA, and AI analysis
├── signals/             # Signal generation engine
├── risk/                # Risk manager + kill switch
├── utils/               # Logger + SQLite state
└── data/
    └── trades.db        # Trade history database
```

## Logs

- Console: Human-readable, INFO level
- File: `logs/bot.log` — JSON format, rotates at 10MB, keeps 7 files

## Important Notes

- Kalshi charges ~7% fee on winning trade profits — factored into the 8-cent minimum edge
- Start with `DRY_RUN=true` for at least 24 hours before going live
- The bot defaults to conservative sizing ($1-5/trade)
- Keep `private_key.pem` secure: `chmod 600 keys/private_key.pem`
