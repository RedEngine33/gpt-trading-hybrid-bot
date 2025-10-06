# GPT Trading Hybrid Bot — Enriched (TV + Coinglass + Glassnode + News)

## New ENV (add to Render → Environment)
- COINGLASS_API_KEY — from Coinglass (Dashboard → API)
- GLASSNODE_API_KEY — from Glassnode (Account → API)
- CRYPTOPANIC_API_TOKEN — optional, for news
- NEWS_FEEDS — optional, comma-separated RSS URLs (fallback)
- (existing) OPENAI_API_KEY, BOT_TOKEN, CHANNEL_ID, TV_SECRET, PARSE_MODE, FORWARD_TO_CHANNEL

## Endpoints
- POST /tv-alert — now auto-enriches with Coinglass/Glassnode/News before GPT
- POST /gpt-signal — also enriched
- POST /tg-webhook — photo+caption vision + enriched text
- GET /diag, /ping-openai, /ping-tg

## Quick test
curl -X POST "https://YOUR-APP.onrender.com/tv-alert" -H "Content-Type: application/json" -d '{"secret":"abc123","text":"BTCUSDT","context":"TF=15m; test"}'

If keys are missing, it will still send an analysis (with null external fields).
