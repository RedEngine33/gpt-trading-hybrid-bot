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
CHANNEL_ID      = os.environ.get("CHANNEL_ID")          # e.g., -1001234567890
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID")     # optional allowlist for /tg-webhook
PARSE_MODE      = os.environ.get("PARSE_MODE", "Markdown")  # or "MarkdownV2"
TV_SECRET       = os.environ.get("TV_SECRET")           # secret for TradingView webhooks
FORWARD_SWITCH  = os.environ.get("FORWARD_TO_CHANNEL", "0") in ("1", "true", "True")

# OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- Helpers ----------
def tg_escape_markdown_v2(text: str) -> str:
    specials = r'_\*\[\]\(\)~`>#+-=|{}.!'
    out = []
    for ch in text:
        out.append("\\" + ch if ch in specials else ch)
    return "".join(out)

def tg_send_message(chat_id: str, message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    text = message if PARSE_MODE != "MarkdownV2" else tg_escape_markdown_v2(message)
    payload = {"chat_id": chat_id, "text": text, "parse_mode": PARSE_MODE}
    r = requests.post(url, json=payload, timeout=25)
    r.raise_for_status()
    return r.json()

def send_signal_to_channel(message: str):
    return tg_send_message(CHANNEL_ID, message)

def generate_gpt_analysis(coin_name: str, extra_context: str = "") -> str:
    base_prompt = (
        f"You are a professional crypto analyst. Provide a concise, actionable analysis for {coin_name} "
        "using EMA(9), EMA(20), RSI(14), and recent candle structure. "
        "Return strictly this structure: "
        "1) Direction (LONG/SHORT/WAIT), 2) Entry range, 3) SL zone, 4) TP1/TP2, 5) RR≈1.5–2, "
        "6) One-line risk note. No emojis. Max 8 lines."
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
        "FORWARD_TO_CHANNEL": FORWARD_SWITCH,
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

# ---- TradingView webhook: POST JSON { "secret": "...", "text": "BTCUSDT", "context": "optional" }
@app.route("/tv-alert", methods=["POST"])
def tv_alert():
    data = request.get_json(silent=True) or {}
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

# ---- Telegram Bot webhook (supports text + photo vision)
@app.route("/tg-webhook", methods=["POST"])
def tg_webhook():
    """
    Supports:
      - Text only: "BTCUSDT 15m pullback"
      - Photo + caption: chart image + "ETHUSDT 5m"
      - Photo without caption: vision-only analysis
    Sends result:
      - Always reply to the same chat
      - Optionally also forward to CHANNEL_ID if FORWARD_TO_CHANNEL=1
    """
    update = request.get_json(silent=True) or {}
    message = (update.get("message") or update.get("edited_message") or {})
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")

    if not chat_id:
        return {"ok": True}

    # Optional allowlist
    if ALLOWED_CHAT_ID and str(chat_id) != str(ALLOWED_CHAT_ID):
        return {"ok": True}

    text = (message.get("text") or message.get("caption") or "").strip()
    photos = message.get("photo") or []  # Telegram sends array of sizes

    # helper to send to chat and optional channel
    def send_out(msg: str):
        try:
            tg_send_message(chat_id, msg)
        except Exception:
            app.logger.exception("Failed sending reply to user chat")
        if FORWARD_SWITCH:
            try:
                send_signal_to_channel(msg)
            except Exception:
                app.logger.exception("Failed forwarding to channel")

    try:
        # PHOTO → Vision
        if photos:
            largest = sorted(photos, key=lambda p: p.get("file_size", 0))[-1]
            file_id = largest.get("file_id")

            # getFile
            r = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                params={"file_id": file_id},
                timeout=20
            )
            r.raise_for_status()
            file_path = (r.json().get("result") or {}).get("file_path")
            if not file_path:
                send_out("❌ نشد فایل عکس رو بگیرم. لطفاً دوباره بفرست یا کپشن بذار.")
                return {"ok": True}

            file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            coin_guess = text.split()[0][:20].upper() if text else ""

            vision_prompt = (
                "You are a professional crypto analyst. Analyze this chart image. "
                "Identify trend structure (HH/HL or LH/LL), EMAs alignment (assume EMA9/EMA20 if visible), "
                "RSI behavior if visible, key supports/resistances, and propose a trade idea if probability is decent. "
                "Return compact playbook: Direction (LONG/SHORT/WAIT), Entry zone, SL zone, TP1/TP2, RR≈1.5–2, one risk note. "
                "No emojis. Max 8 lines."
            )
            if text:
                vision_prompt += f"\nUser context: {text}\n"

            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0.2,
                messages=[
                    {"role": "system", "content": "You are a disciplined, probability-focused trader."},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": vision_prompt},
                            {"type": "image_url", "image_url": {"url": file_url}}
                        ]
                    }
                ]
            )
            analysis = (resp.choices[0].message.content or "").strip()
            header = f"🖼️ Vision Analysis{f' for *{coin_guess}*' if coin_guess else ''}:"
            msg = f"{header}\n\n{analysis}"
            send_out(msg)
            return {"ok": True}, 200

        # TEXT-ONLY
        if not text:
            tg_send_message(chat_id, "لطفاً نماد یا توضیح کوتاه بفرست، یا عکس چارت رو با کپشن ارسال کن.")
            return {"ok": True}

        parts = text.split()
        coin = parts[0][:20].upper()
        extra = " ".join(parts[1:]).strip()
        analysis = generate_gpt_analysis(coin, extra)
        msg = f"📊 *GPT Analysis for {coin}:*\n\n{analysis}"
        send_out(msg)
        return {"ok": True}, 200

    except Exception as e:
        app.logger.exception("tg-webhook error")
        try:
            tg_send_message(chat_id, f"❌ Error: {e}")
        except Exception:
            pass
        return {"ok": False}, 200

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

