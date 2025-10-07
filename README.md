# GPT Trading Hybrid Bot — Option 2 PLUS

## What’s new (this package)
- Pretty, compact **signal formatting** for Telegram.
- **Advanced CSV journal** (`trade_journal.csv`) with upsert by `trade_id`.
- **Telegram commands** to maintain journal:
  - `/tp1 ID PRICE`, `/tp2 ID PRICE`, `/sl ID`, `/exit ID PNL`, `/cancel ID`, `/status ID`, `/fill ID PRICE`
- **Vision & text** in `/tg-webhook` (replies only to your chat; not forwarded to public channel).
- **TradingView → Channel** path stays strict: `/tv-alert` posts to `CHANNEL_ID` and logs to journal.
- Optional **GPT validation** of TV alert using Pine numbers (`ENABLE_GPT_VALIDATION=1`).

## Endpoints
- `GET /health`, `GET /diag`
- `GET /ping-tg`, `GET /ping-openai`
- `POST /tv-alert`    ← TradingView webhook (uses metrics JSON from Pine)
- `POST /gpt-signal`  ← manual JSON test; does NOT post to channel
- `POST /tg-webhook`  ← Telegram Bot webhook (text + photo vision + journal commands)

## Deploy on Render
1. Create a **Web Service** from this folder/repo.
2. Set **Environment Variables** (Dashboard → Environment):
   - `OPENAI_API_KEY`      = `sk-...`
   - `BOT_TOKEN`           = `12345:ABC...`
   - `CHANNEL_ID`          = `-100xxxxxxxxxx` (signals channel)
   - `ALLOWED_CHAT_ID`     = your Telegram user id (optional, to restrict /tg-webhook)
   - `PARSE_MODE`          = `Markdown`
   - `TV_SECRET`           = `abc123!@#` (must match Pine)
   - `FORWARD_TO_CHANNEL`  = `0`
   - `JOURNAL_CSV_PATH`    = `./trade_journal.csv`
   - `JOURNAL_CHANNEL_ID`  = `-100yyyyyyyyyy` (optional)
   - `ENABLE_GPT_VALIDATION` = `1` (optional)
3. **Start command**: Use `Procfile` or set:  
   ```
   gunicorn gpt_signal_api:app --bind 0.0.0.0:$PORT
   ```

## Telegram Setup
- Set webhook: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=https://YOUR-APP.onrender.com/tg-webhook`
- Test: send a text like `BTCUSDT` or a **chart image** (with optional caption).
- Journal commands work in the same chat.

## TradingView
- Use the Pine included below (or your own). The script sends **JSON** with `metrics` to `/tv-alert`.
- Alert: **Condition = Any alert() function call**.
- Webhook URL: `https://YOUR-APP.onrender.com/tv-alert`
- Example payload sent by Pine (for reference only):
```json
{
  "secret": "abc123!@#",
  "symbol": "BTCUSDT",
  "metrics": {
    "tf": "15m",
    "setup": "strong_long",
    "close": 67890.0,
    "ema9": 67880.0,
    "ema20": 67750.0,
    "rsi": 58.2,
    "atr": 140.0,
    "entry_min": 67850.0,
    "entry_max": 67920.0,
    "sl": 67680.0,
    "tp1": 68080.0,
    "tp2": 68220.0,
    "rr": 1.6,
    "mtf": "swing_15m_over_1h"
  }
}
```

## Pine (sample) — tv_alerts_option2.pine
- Attach to your chart and create a single alert with *Any alert() function call*.
- This sample keeps logic simple; feel free to replace with your own.

```
 //@version=5
 indicator("TV→Webhook JSON (Option2)", overlay=true)

 tf  = input.timeframe("15", "TF")
 ema9  = ta.ema(close, 9)
 ema20 = ta.ema(close, 20)
 rsi = ta.rsi(close, 14)
 atr = ta.atr(14)

 strong_long  = ema9 > ema20 and rsi > 55
 strong_short = ema9 < ema20 and rsi < 45
 setup = strong_long ? "strong_long" : strong_short ? "strong_short" : "neutral"

 entry_min = close
 entry_max = close
 sl  = strong_long ? close - atr : strong_short ? close + atr : na
 tp1 = strong_long ? close + atr*1.2 : strong_short ? close - atr*1.2 : na
 tp2 = strong_long ? close + atr*2.0 : strong_short ? close - atr*2.0 : na
 rr  = 1.6

 payload = '{"secret":"abc123!@#", "symbol":"%s", "metrics":{"tf":"%s","setup":"%s","close":%f,"ema9":%f,"ema20":%f,"rsi":%f,"atr":%f,"entry_min":%f,"entry_max":%f,"sl":%f,"tp1":%f,"tp2":%f,"rr":%f}}'
 alert_message = str.format(payload, syminfo.ticker, tf, setup, close, ema9, ema20, rsi, atr, entry_min, entry_max, sl, tp1, tp2, rr)

 alertcondition(true, title="Send JSON", message=alert_message)
```

## Notes
- Public channel receives **only** `/tv-alert` signals.
- Personal chat (`/tg-webhook`) is great for quick checks, image vision, and updating the journal with commands.
- CSV is simple and portable; you can open it in Excel or import to Google Sheets.
