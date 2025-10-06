
# GPT Trading Hybrid Bot (Flask, OpenAI, Telegram)

Automation paths:
- **TradingView → Server → GPT → Telegram**
- **Telegram (you) → Server → GPT → Telegram**

## Endpoints
- `GET /` → service info
- `GET /health` → health check
- `GET /diag` → env diagnostic flags
- `POST /gpt-signal` → `{ "text": "BTCUSDT", "context": "optional notes" }`
- `POST /tv-alert` → from TradingView; JSON must include `{ "secret": "...", "text": "BTCUSDT" }`
- `POST /tg-webhook` → Telegram bot webhook
- `GET /ping-tg` → sends a test message to CHANNEL_ID
- `GET /ping-openai` → tests OpenAI connectivity

## Deploy (Render)
1. Push repo to GitHub.
2. Create **Web Service** on Render.
3. Set **Environment** variables:
   - `OPENAI_API_KEY` (must start with `sk-`)
   - `BOT_TOKEN`
   - `CHANNEL_ID` (like `-1001234567890`)
   - `ALLOWED_CHAT_ID` (optional, numeric)
   - `PARSE_MODE` (Markdown or MarkdownV2)
   - `TV_SECRET` (secret for TradingView)
4. Ensure **Start Command** is empty (so Procfile is used) **OR** set it to:
   ```
   gunicorn gpt_signal_api:app --bind 0.0.0.0:$PORT
   ```
5. Deploy.

## Telegram Webhook
Set your bot webhook to the service URL:
```
https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=https://YOUR-RENDER-APP.onrender.com/tg-webhook
```

## TradingView Alert
Webhook URL:
```
https://YOUR-RENDER-APP.onrender.com/tv-alert
```
Message body (JSON):
```json
{ "secret": "YOUR_TV_SECRET", "text": "{{ticker}}", "context": "optional note" }
```

## Test (curl)
```bash
curl -X POST "https://YOUR-RENDER-APP.onrender.com/gpt-signal"   -H "Content-Type: application/json"   -d '{"text":"BTCUSDT"}'
```

If you get `status":"sent"`, check your Telegram channel for the message.

## Notes
- If Telegram parsing fails, switch to `PARSE_MODE=MarkdownV2`.
- For rate limits or outages, add retries around Telegram requests.
- `ALLOWED_CHAT_ID` can restrict who the bot accepts messages from.
