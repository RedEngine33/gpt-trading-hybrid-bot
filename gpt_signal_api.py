
import os
import logging
import traceback
import csv
from datetime import datetime
from flask import Flask, request, jsonify
import requests
from openai import OpenAI

# ---------- App & Logging ----------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------- Environment ----------
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY")
BOT_TOKEN       = os.environ.get("BOT_TOKEN")
CHANNEL_ID      = os.environ.get("CHANNEL_ID")             # e.g., -1001234567890 (main signals channel)
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID")        # optional allowlist for /tg-webhook
PARSE_MODE      = os.environ.get("PARSE_MODE", "Markdown") # or "MarkdownV2"
TV_SECRET       = os.environ.get("TV_SECRET")              # secret for TradingView webhooks
FORWARD_SWITCH  = os.environ.get("FORWARD_TO_CHANNEL", "0") in ("1", "true", "True")  # default off in option-2

# Journal
JOURNAL_CSV_PATH = os.environ.get("JOURNAL_CSV_PATH", "./trade_journal.csv")
JOURNAL_CHANNEL  = os.environ.get("JOURNAL_CHANNEL_ID")  # optional channel id for journal summaries

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
    if not CHANNEL_ID:
        raise RuntimeError("CHANNEL_ID is not set")
    return tg_send_message(CHANNEL_ID, message)

def journal_append(source, coin, analysis_text, extra=""):
    """Append each signal to CSV + optionally push a compact note to JOURNAL_CHANNEL."""
    header = ["ts_utc","source","coin","extra","analysis"]
    row = [datetime.utcnow().isoformat(timespec="seconds")+"Z", source, coin, extra, (analysis_text or "").replace("\n"," \\n ")]
    new_file = not os.path.exists(JOURNAL_CSV_PATH)
    with open(JOURNAL_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file: w.writerow(header)
        w.writerow(row)
    if JOURNAL_CHANNEL:
        try:
            preview = (analysis_text[:350] + "…") if analysis_text and len(analysis_text) > 350 else (analysis_text or "")
            tg_send_message(JOURNAL_CHANNEL, f"🗒 Journal | {source} | {coin}\n{preview}")
        except Exception:
            app.logger.exception("Failed sending to journal channel")

# ---------- Routes ----------
@app.route("/", methods=["GET"])
def root():
    return jsonify({"ok": True, "service": "gpt-trading-hybrid-bot-option2"})

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
        "JOURNAL_CSV_PATH": JOURNAL_CSV_PATH,
        "JOURNAL_CHANNEL_set": bool(JOURNAL_CHANNEL),
        "model": "gpt-4o-mini"
    }

# ---- Manual JSON trigger: POST { "text": "BTCUSDT", "context": "optional extra notes" }
@app.route("/gpt-signal", methods=["POST"])
def gpt_signal():
    # This endpoint replies to the requester; does NOT forward to channel in option-2
    data = request.get_json(silent=True) or {}
    coin = (data.get("text") or data.get("coin") or "").upper().strip()
    extra = (data.get("context") or "").strip()

    if not coin:
        return jsonify({"status": "error", "message": "No coin provided (use text/coin)"}), 400

    try:
        base_prompt = (
            f"You are a disciplined analyst. Provide a compact plan for {coin}.\n"
            "Return exactly 6 lines:\n"
            "1) Direction (LONG/SHORT/WAIT)\n"
            "2) Entry range\n"
            "3) SL zone\n"
            "4) TP1/TP2\n"
            "5) RR≈1.5–2\n"
            "6) One-line risk note.\n"
            "No emojis. Max 8 lines total."
        )
        if extra:
            base_prompt += f"\nUser context: {extra}"

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a probability-focused trader."},
                {"role": "user", "content": base_prompt}
            ],
            temperature=0.2
        )
        analysis = (resp.choices[0].message.content or "").strip()
        msg = f"📊 *GPT Analysis for {coin}:*\n\n{analysis}"
        # reply only to requester; do not forward to channel in option-2
        return jsonify({"status": "ok", "coin": coin, "analysis": analysis}), 200
    except Exception as e:
        app.logger.exception("Unhandled error in /gpt-signal")
        return jsonify({"status": "error", "detail": str(e), "trace": traceback.format_exc()}), 500

# ---- TradingView webhook: POST JSON { "secret": "...", "text": "BTCUSDT", "metrics": {...} }
@app.route("/tv-alert", methods=["POST"])
def tv_alert():
    data = request.get_json(silent=True) or {}
    if (data.get("secret") or "") != (TV_SECRET or ""):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    coin = (data.get("text") or data.get("symbol") or "").upper().strip()
    metrics = data.get("metrics") or {}
    if not coin:
        return jsonify({"ok": False, "error": "no symbol"}), 400

    try:
        # Strict prompt: use ONLY Pine metrics; direction tied to setup
        prompt = (
            f"Use ONLY these numbers and rules.\n"
            f"COIN={coin} TF={metrics.get('tf')} CLOSE={metrics.get('close')} "
            f"EMA9={metrics.get('ema9')} EMA20={metrics.get('ema20')} "
            f"RSI={metrics.get('rsi')} ATR={metrics.get('atr')} "
            f"SETUP={metrics.get('setup')}.\n\n"
            "Direction MUST follow SETUP: LONG if strong_long, SHORT if strong_short, otherwise WAIT.\n"
            "Return exactly 6 lines:\n"
            "1) Direction\n2) Entry range\n3) SL zone\n4) TP1/TP2\n5) RR≈1.5–2\n6) One-line risk note.\n"
            "No emojis. Max 8 lines total."
        )

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.1,
            messages=[{"role": "user", "content": prompt}]
        )
        analysis = (resp.choices[0].message.content or "").strip()
        msg = f"📊 *GPT Analysis for {coin}:*\n\n{analysis}"
        # Send to main channel (signals) and journal
        res = send_signal_to_channel(msg)
        journal_append("tv-alert", coin, analysis, extra=str(metrics))
        return jsonify({"ok": True, "coin": coin, "telegram": res}), 200
    except Exception as e:
        app.logger.exception("Unhandled error in /tv-alert")
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()}), 500

# ---- Telegram Bot webhook (supports text + photo vision)  — replies to user only
@app.route("/tg-webhook", methods=["POST"])
def tg_webhook():
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

    def reply(msg: str):
        try:
            tg_send_message(chat_id, msg)
        except Exception:
            app.logger.exception("Failed sending reply to user chat")

    try:
        # PHOTO → simple vision summary (reply only)
        if photos:
            largest = sorted(photos, key=lambda p: p.get("file_size", 0))[-1]
            file_id = largest.get("file_id")
            r = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                params={"file_id": file_id},
                timeout=20
            )
            r.raise_for_status()
            file_path = (r.json().get("result") or {}).get("file_path")
            if not file_path:
                reply("❌ نشد فایل عکس رو بگیرم. لطفاً دوباره بفرست یا کپشن بذار.")
                return {"ok": True}

            file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            coin_guess = text.split()[0][:20].upper() if text else ""

            vision_prompt = (
                "Analyze this trading chart image briefly. "
                "Provide a compact playbook: Direction (LONG/SHORT/WAIT), Entry zone, SL zone, TP1/TP2, RR≈1.5–2, one risk line. "
                "No emojis. Max 8 lines."
            )
            if text:
                vision_prompt += f"\nUser context: {text}\n"

            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0.2,
                messages=[
                    {"role": "system", "content": "You are a disciplined, probability-focused trader."},
                    {"role": "user", "content": [
                        {"type": "text", "text": vision_prompt},
                        {"type": "image_url", "image_url": {"url": file_url}}
                    ]}
                ]
            )
            analysis = (resp.choices[0].message.content or "").strip()
            header = f"🖼️ Vision Analysis{f' for *{coin_guess}*' if coin_guess else ''}:"
            reply(f"{header}\n\n{analysis}")
            journal_append("tg-webhook-vision", coin_guess or "N/A", analysis, extra=text)
            return {"ok": True}, 200

        # TEXT-ONLY → reply only
        if not text:
            reply("لطفاً نماد یا توضیح کوتاه بفرست، یا عکس چارت رو با کپشن ارسال کن.")
            return {"ok": True}

        parts = text.split()
        coin = parts[0][:20].upper()
        extra = " ".join(parts[1:]).strip()

        # simple analysis
        base_prompt = (
            f"Provide a compact plan for {coin}.\n"
            "Return exactly 6 lines:\n"
            "1) Direction (LONG/SHORT/WAIT)\n"
            "2) Entry range\n"
            "3) SL zone\n"
            "4) TP1/TP2\n"
            "5) RR≈1.5–2\n"
            "6) One-line risk note.\n"
            "No emojis. Max 8 lines."
        )
        if extra:
            base_prompt += f"\nUser context: {extra}"
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            messages=[{"role":"user","content": base_prompt}]
        )
        analysis = (resp.choices[0].message.content or "").strip()
        reply(f"📊 *GPT Analysis for {coin}:*\n\n{analysis}")
        journal_append("tg-webhook-text", coin, analysis, extra)
        return {"ok": True}, 200

    except Exception as e:
        app.logger.exception("tg-webhook error")
        try:
            reply(f"❌ Error: {e}")
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
