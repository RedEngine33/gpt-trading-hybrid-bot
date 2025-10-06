import os
import logging
import traceback
from flask import Flask, request, jsonify
import requests
from openai import OpenAI

# ---------- App & Logging ----------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------- Environment ----------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
BOT_TOKEN       = os.environ.get("BOT_TOKEN")
CHANNEL_ID      = os.environ.get("CHANNEL_ID")          # e.g., -1001234567890  (for sendMessage)
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID")     # optional: only accept messages from this user/chat (numeric id)
PARSE_MODE      = os.environ.get("PARSE_MODE", "Markdown")  # or "MarkdownV2"
TV_SECRET       = os.environ.get("TV_SECRET")           # secret string for TradingView webhook auth

# OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- Helpers ----------
def tg_escape_markdown_v2(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters."""
    specials = r'_\*\[\]\(\)~`>#+-=|{}.!'
    out = []
    for ch in text:
        if ch in specials:
            out.append('\\' + ch)
        else:
            out.append(ch)
    return ''.join(out)

def tg_send_message(chat_id: str, message: str):
    """Send a Telegram message to a specific chat_id."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    text = message if PARSE_MODE != "MarkdownV2" else tg_escape_markdown_v2(message)
    payload = {"chat_id": chat_id, "text": text, "parse_mode": PARSE_MODE}
    r = requests.post(url, json=payload, timeout=25)
    r.raise_for_status()
    return r.json()

def send_signal_to_channel(message: str):
    """Send a Telegram message to configured CHANNEL_ID."""
    return tg_send_message(CHANNEL_ID, message)

def generate_gpt_analysis(coin_name: str, extra_context: str = "") -> str:
    """Call OpenAI to generate concise analysis, optionally with extra context (from user/chart)."""
    base_prompt = (
        f"You are a professional crypto analyst. Provide a concise, actionable analysis for {coin_name} "
        "using EMA(9), EMA(20), RSI(14), and recent candle structure. "
        "Return: Direction (LONG/SHORT/WAIT), Entry range, SL zone, 2 TP levels, RR=1.5–2 where possible, "
        "and a one-line risk note. Keep it under 8 lines, no emojis."
    )
    if extra_context:
        base_prompt += f"\nAdditional context from user/charts:\n{extra_context}\n"

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a disciplined, probability-focused trader."},
            {"role": "user", "content": base_prompt}
        ],
        temperature=0.2
    )
    return (resp.choices[0].message.content or "").strip()

# ---------- Routes ----------
@app.route("/", methods=["GET"])
def root():
    return jsonify({"ok": True, "service": "gpt-trading-hybrid-bot"})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"})

@app.route("/diag", methods=["GET"])
def diag():
    return {
        "OPENAI_API_KEY_set": bool(OPENAI_API_KEY),
        "BOT_TOKEN_set": bool(BOT_TOKEN),
        "CHANNEL_ID_set": bool(CHANNEL_ID),
        "ALLOWED_CHAT_ID_set": bool(ALLOWED_CHAT_ID),
        "PARSE_MODE": PARSE_MODE,
        "TV_SECRET_set": bool(TV_SECRET),
        "model": "gpt-4o-mini"
    }

# ---- Manual JSON trigger: POST { "text": "BTCUSDT", "context": "optional extra notes" }
@app.route("/gpt-signal", methods=["POST"])
def gpt_signal():
    data = request.get_json(silent=True) or {}
    coin = (data.get("text") or data.get("coin") or "").upper().strip()
    extra = (data.get("context") or "").strip()

    if not coin:
        return jsonify({"status": "error", "message": "No coin provided (use text/coin)"}), 400

    try:
        analysis = generate_gpt_analysis(coin, extra)
        msg = f"📊 *GPT Analysis for {coin}:*\n\n{analysis}"
        res = send_signal_to_channel(msg)
        return jsonify({"status": "sent", "coin": coin, "telegram": res}), 200
    except requests.HTTPError as e:
        body = e.response.text if getattr(e, "response", None) is not None else ""
        app.logger.exception("Telegram HTTPError")
        return jsonify({"status": "telegram_error", "detail": str(e), "response": body}), 502
    except Exception as e:
        app.logger.exception("Unhandled error in /gpt-signal")
        return jsonify({"status": "error", "detail": str(e), "trace": traceback.format_exc()}), 500

# ---- TradingView webhook: POST with JSON { "secret": "...", "text": "BTCUSDT", "context": "optional" }
@app.route("/tv-alert", methods=["POST"])
def tv_alert():
    data = request.get_json(silent=True) or {}

    # simple secret validation
    if (data.get("secret") or "") != (TV_SECRET or ""):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    coin = (data.get("text") or data.get("symbol") or "").upper().strip()
    extra = (data.get("context") or "").strip()

    if not coin:
        return jsonify({"ok": False, "error": "no symbol"}), 400

    try:
        analysis = generate_gpt_analysis(coin, extra)
        msg = f"📊 *GPT Analysis for {coin}:*\n\n{analysis}"
        res = send_signal_to_channel(msg)
        return jsonify({"ok": True, "coin": coin, "telegram": res}), 200
    except Exception as e:
        app.logger.exception("Unhandled error in /tv-alert")
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500

# ---- Telegram Bot webhook
# setWebhook: https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=https://YOUR-RENDER-APP.onrender.com/tg-webhook
@app.route("/tg-webhook", methods=["POST"])
def tg_webhook():
    update = request.get_json(silent=True) or {}
    message = (update.get("message") or update.get("edited_message") or {})
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")

    # Optional: restrict who can talk to the bot
    if ALLOWED_CHAT_ID and chat_id and str(chat_id) != str(ALLOWED_CHAT_ID):
        # ignore messages not from allowed chat
        return {"ok": True}

    text = (message.get("text") or message.get("caption") or "").strip()
    # If message includes photos, we still rely on caption as context
    # (OCR can be added later if needed)

    if not chat_id:
        return {"ok": True}

    if not text:
        # Ask user to send symbol or note
        try:
            tg_send_message(chat_id, "لطفاً نماد یا توضیح کوتاه بفرست (مثلاً: BTCUSDT با توضیح).")
        except Exception:
            pass
        return {"ok": True}

    # Very simple parse: first token as symbol, rest as context
    parts = text.split()
    coin = parts[0][:20].upper()
    extra = " ".join(parts[1:]).strip()

    try:
        analysis = generate_gpt_analysis(coin, extra)
        msg = f"📊 *GPT Analysis for {coin}:*\n\n{analysis}"
        tg_send_message(chat_id, msg)
    except Exception as e:
        app.logger.exception("Unhandled error in /tg-webhook")
        try:
            tg_send_message(chat_id, f"❌ Error: {e}")
        except Exception:
            pass

    return {"ok": True}, 200

# ---- Quick pings
@app.route("/ping-tg", methods=["GET"])
def ping_tg():
    try:
        res = send_signal_to_channel("✅ Test: Telegram is connected.")
        return {"ok": True, "telegram": res}
    except Exception as e:
        return {"ok": False, "where": "telegram", "error": str(e)}, 500

@app.route("/ping-openai", methods=["GET"])
def ping_openai():
    try:
        test = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "ping"}],
            temperature=0
        )
        txt = test.choices[0].message.content
        return {"ok": True, "openai_sample": (txt or "")[:80]}
    except Exception as e:
        return {"ok": False, "where": "openai", "error": str(e)}, 500
