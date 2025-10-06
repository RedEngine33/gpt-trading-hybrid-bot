
# GPT Trading Hybrid Bot — Option 2 (Strict TV → GPT → Channel)

## What changes
- **ONLY TradingView alerts** post to the public channel.
- `/tg-webhook` replies **only to you** (no forwarding).
- `/tv-alert` consumes **metrics JSON** from Pine and constrains GPT to those numbers.
- Automatic **journal CSV** + optional **journal channel**.

## Endpoints
- `GET /health`, `GET /diag`
- `GET /ping-tg`, `GET /ping-openai`
- `POST /tv-alert`  ← TradingView webhook (uses metrics)
- `POST /gpt-signal` ← manual test; does NOT post to channel

## Deploy on Render
1. Create a Web Service from this repo/zip.
2. Environment variables (sample):
   - `OPENAI_API_KEY` = `sk-...`
   - `BOT_TOKEN`      = `12345:ABC...`
   - `CHANNEL_ID`     = `-100xxxxxxxxxx`
   - `ALLOWED_CHAT_ID`= your telegram user id (optional)
   - `PARSE_MODE`     = `Markdown`
   - `TV_SECRET`      = e.g. `abc123!@#` (MUST match Pine)
   - `FORWARD_TO_CHANNEL` = `0`  (default)
   - `JOURNAL_CSV_PATH`   = `./trade_journal.csv`
   - `JOURNAL_CHANNEL_ID` = `-100yyyyyyyyyy` (optional)
3. Start command: use **Procfile** or set:
   ```
   gunicorn gpt_signal_api:app --bind 0.0.0.0:$PORT
   ```

## TradingView
- Use the Pine in `tv_alerts_option2.pine`.
- Create alert with **Condition = Any alert() function call**.
- Webhook URL: `https://YOUR-APP.onrender.com/tv-alert`
- No message body needed (the script sends JSON via `alert()`).
