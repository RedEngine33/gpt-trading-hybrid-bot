
import os
import logging
import traceback
import csv
import time
from datetime import datetime
from uuid import uuid4
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
FORWARD_SWITCH  = os.environ.get("FORWARD_TO_CHANNEL", "0") in ("1", "true", "True")

# Journal
JOURNAL_CSV_PATH = os.environ.get("JOURNAL_CSV_PATH", "./trade_journal.csv")
JOURNAL_CHANNEL  = os.environ.get("JOURNAL_CHANNEL_ID")  # optional channel id for journal summaries

# OpenAI client (lazy init if missing key)
client = None
if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- Helpers ----------
def tg_escape_markdown_v2(text: str) -> str:
    specials = r'_\*\[\]\(\)~`>#+-=|{}.!'
    out = []
    for ch in text:
        out.append("\\" + ch if ch in specials else ch)
    return "".join(out)

def tg_api(method: str) -> str:
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

def tg_send_message(chat_id: str, message: str):
    url = tg_api("sendMessage")
    text = message if PARSE_MODE != "MarkdownV2" else tg_escape_markdown_v2(message)
    payload = {"chat_id": chat_id, "text": text, "parse_mode": PARSE_MODE, "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=25)
    r.raise_for_status()
    return r.json()

def send_signal_to_channel(message: str):
    if not CHANNEL_ID:
        raise RuntimeError("CHANNEL_ID is not set")
    return tg_send_message(CHANNEL_ID, message)

# -------- Journal utilities --------
JOURNAL_FIELDS = [
    "timestamp","trade_id","source","symbol","tf","setup",
    "entry_min","entry_max","sl","tp1","tp2","rr",
    "decision","status","pnl","note"
]

def journal_ensure():
    new_file = not os.path.exists(JOURNAL_CSV_PATH)
    if new_file:
        with open(JOURNAL_CSV_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
            w.writeheader()

def journal_append_row(row: dict):
    journal_ensure()
    with open(JOURNAL_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
        w.writerow({k: row.get(k, "") for k in JOURNAL_FIELDS})
    if JOURNAL_CHANNEL:
        try:
            preview = format_signal_preview(row)
            tg_send_message(JOURNAL_CHANNEL, f"🗒 Journal | {row.get('source','?')} | {row.get('symbol','?')}\n{preview}")
        except Exception:
            app.logger.exception("Failed sending to journal channel")

def journal_upsert(row: dict):
    """Upsert by trade_id."""
    journal_ensure()
    trade_id = row.get("trade_id")
    if not trade_id:
        raise ValueError("journal_upsert requires trade_id")
    rows = []
    found = False
    with open(JOURNAL_CSV_PATH, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for rec in r:
            if rec["trade_id"] == trade_id:
                rec.update({k: str(v) for k, v in row.items() if v is not None})
                found = True
            rows.append(rec)
    if not found:
        base = {k: "" for k in JOURNAL_FIELDS}
        base.update(row)
        if not base.get("timestamp"):
            base["timestamp"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        rows.append(base)
    with open(JOURNAL_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
        w.writeheader()
        for rec in rows:
            w.writerow(rec)

def format_signal_preview(d: dict) -> str:
    entry = (
        f"{d.get('entry_min','')}-{d.get('entry_max','')}".strip("-")
        if d.get("entry_min") or d.get("entry_max")
        else "-"
    )
    return (
        f"🎯 {d.get('symbol','?')} | TF: {d.get('tf','-')} | Setup: {d.get('setup','-')}\n"
        f"💡 Decision: {d.get('decision','WAIT')} | RR≈{d.get('rr','-')}\n"
        f"📈 Entry: {entry}\n"
        f"🛡 SL: {d.get('sl','-')}\n"
        f"🎯 TP1: {d.get('tp1','-')} | TP2: {d.get('tp2','-')}\n"
        f"⚠️ {d.get('note','-')}\n"
        f"🆔 {d.get('trade_id','-')}"
    )

def format_signal_message(d: dict, with_id=True, mtf=None) -> str:
    msg = format_signal_preview(d)
    if mtf:
        msg += f"\n🧭 MTF: `{mtf}`"
    if not with_id:
        msg = "\n".join(msg.splitlines()[:-1])
    return msg

# ---------- Routes ----------
@app.route("/", methods=["GET"])
def root():
    return jsonify({"ok": True, "service": "gpt-trading-hybrid-bot-option2-plus"})

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
    data = request.get_json(silent=True) or {}
    coin = (data.get("text") or data.get("coin") or "").upper().strip()
    extra = (data.get("context") or "").strip()
    if not coin:
        return jsonify({"status": "error", "message": "No coin provided (use text/coin)"}), 400
    if not client:
        return jsonify({"status": "error", "message": "OPENAI_API_KEY not set"}), 500
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
        return jsonify({"status": "ok", "coin": coin, "analysis": analysis}), 200
    except Exception as e:
        app.logger.exception("Unhandled error in /gpt-signal")
        return jsonify({"status": "error", "detail": str(e), "trace": traceback.format_exc()}), 500

# ---- TradingView webhook: POST JSON { "secret": "...", "symbol":"BTCUSDT", "metrics": {...} }
@app.route("/tv-alert", methods=["POST"])
def tv_alert():
    data = request.get_json(silent=True) or {}
    if (data.get("secret") or "") != (TV_SECRET or ""):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    coin = (data.get("text") or data.get("symbol") or "").upper().strip()
    metrics = data.get("metrics") or {}
    mtf = data.get("mtf") or metrics.get("mtf") or ""
    provided_id = data.get("trade_id") or ""
    if not coin:
        return jsonify({"ok": False, "error": "no symbol"}), 400

    # Build trade payload
    trade_id = provided_id or f"{coin}-{metrics.get('tf','')}-{int(time.time())}"
    decision = "WAIT"
    setup = str(metrics.get("setup") or "")
    if setup == "strong_long":
        decision = "LONG"
    elif setup == "strong_short":
        decision = "SHORT"

    # Use numeric fields if sent from Pine
    entry_min = metrics.get("entry_min") or metrics.get("entryLow") or ""
    entry_max = metrics.get("entry_max") or metrics.get("entryHigh") or ""
    sl = metrics.get("sl") or ""
    tp1 = metrics.get("tp1") or ""
    tp2 = metrics.get("tp2") or ""
    rr = metrics.get("rr") or ""

    journal_upsert({
        "timestamp": datetime.utcnow().isoformat(timespec="seconds")+"Z",
        "trade_id": trade_id,
        "source": "tradingview",
        "symbol": coin,
        "tf": metrics.get("tf",""),
        "setup": setup,
        "entry_min": entry_min,
        "entry_max": entry_max,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "rr": rr,
        "decision": decision,
        "status": "open",
        "pnl": "",
        "note": mtf or ""
    })

    # Build message
    msg = format_signal_message({
        "trade_id": trade_id, "source":"tradingview", "symbol": coin, "tf": metrics.get("tf",""),
        "setup": setup, "entry_min": entry_min, "entry_max": entry_max, "sl": sl,
        "tp1": tp1, "tp2": tp2, "rr": rr, "decision": decision, "status": "open",
        "pnl": "", "note": metrics.get("risk","-")
    }, with_id=True, mtf=mtf)

    # Optionally include a compact GPT validation using only Pine numbers
    if client and os.environ.get("ENABLE_GPT_VALIDATION","1") in ("1","true","True"):
        try:
            prompt = (
                f"Use ONLY these numbers and rules.\n"
                f"COIN={coin} TF={metrics.get('tf')} CLOSE={metrics.get('close')} "
                f"EMA9={metrics.get('ema9')} EMA20={metrics.get('ema20')} "
                f"RSI={metrics.get('rsi')} ATR={metrics.get('atr')} "
                f"SETUP={setup}.\n\n"
                "Direction MUST follow SETUP: LONG if strong_long, SHORT if strong_short, otherwise WAIT.\n"
                "Return exactly 6 lines:\n"
                "1) Direction\n2) Entry range\n3) SL zone\n4) TP1/TP2\n5) RR≈1.5–2\n6) One-line risk note.\n"
                "No emojis. Max 8 lines total."
            )
            resp = client.chat.completions.create(
                model="gpt-4o-mini", temperature=0.1,
                messages=[{"role":"user","content":prompt}]
            )
            analysis = (resp.choices[0].message.content or "").strip()
            msg += f"\n\n🔎 Validation:\n{analysis}"
        except Exception:
            app.logger.exception("Validation failed")

    try:
        send_signal_to_channel(msg)
    except Exception as e:
        app.logger.exception("Failed to send to channel")
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "trade_id": trade_id}), 200

# ---- Telegram Bot webhook (supports text + photo vision)  — replies to user only + commands
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

    # ---- Commands for journal ----
    if text.startswith("/"):
        handle_command(text, reply)
        return {"ok": True}, 200

    # ---- PHOTO → simple vision summary (reply only)
    if photos:
        if not client:
            reply("Vision غیرفعال است (OPENAI_API_KEY ست نشده).")
            return {"ok": True}
        try:
            largest = sorted(photos, key=lambda p: p.get("file_size", 0))[-1]
            file_id = largest.get("file_id")
            r = requests.get(
                tg_api("getFile"),
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

            # Store vision trade row
            v_trade_id = f"V-{int(time.time())}"
            journal_upsert({
                "timestamp": datetime.utcnow().isoformat(timespec="seconds")+"Z",
                "trade_id": v_trade_id,
                "source": "vision",
                "symbol": coin_guess or "-",
                "tf": "-",
                "setup": "vision",
                "entry_min": "", "entry_max": "", "sl": "", "tp1": "", "tp2": "", "rr": "",
                "decision": "", "status": "open", "pnl": "", "note": analysis[:240]
            })
            return {"ok": True}, 200
        except Exception as e:
            app.logger.exception("tg-webhook vision error")
            reply(f"❌ Vision error: {e}")
            return {"ok": True}, 200

    # ---- TEXT-ONLY → reply only
    if text:
        if not client:
            reply("برای تحلیل متنی، کلید OPENAI را ست کن.")
            return {"ok": True}
        parts = text.split()
        coin = parts[0][:20].upper()
        extra = " ".join(parts[1:]).strip()

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
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0.2,
                messages=[{"role":"user","content": base_prompt}]
            )
            analysis = (resp.choices[0].message.content or "").strip()
            reply(f"📊 *GPT Analysis for {coin}:*\n\n{analysis}")
            # Store as text-signal
            t_trade_id = f"T-{coin}-{int(time.time())}"
            journal_upsert({
                "timestamp": datetime.utcnow().isoformat(timespec="seconds")+"Z",
                "trade_id": t_trade_id,
                "source": "tg-text",
                "symbol": coin, "tf": "-", "setup":"chat",
                "entry_min":"", "entry_max":"", "sl":"", "tp1":"", "tp2":"", "rr":"",
                "decision":"", "status":"open", "pnl":"", "note": analysis[:240]
            })
            return {"ok": True}, 200
        except Exception as e:
            app.logger.exception("tg-webhook text error")
            reply(f"❌ Error: {e}")
            return {"ok": True}, 200

    reply("✅ بات فعاله. برای آپدیت ژورنال:\n"
          "`/tp1 ID PRICE`, `/tp2 ID PRICE`, `/sl ID`, `/exit ID PNL`, `/cancel ID`, `/status ID`, `/fill ID PRICE`")
    return {"ok": True}, 200

# ---- Commands Handler ----
def handle_command(text: str, reply):
    parts = text.split()
    cmd = parts[0].lower()
    if cmd not in ("/tp1","/tp2","/sl","/exit","/cancel","/status","/fill"):
        reply("❗️فرمت یا دستور ناشناخته است.")
        return

    if len(parts) < 2:
        reply("❗️شناسه معامله (ID) را وارد کن.")
        return
    trade_id = parts[1]

    journal_ensure()
    rows = []
    found = None
    with open(JOURNAL_CSV_PATH, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for rec in r:
            if rec["trade_id"] == trade_id:
                found = rec
            rows.append(rec)

    if not found:
        reply(f"🔎 معامله‌ای با ID `{trade_id}` پیدا نشد.")
        return

    def save_rows():
        with open(JOURNAL_CSV_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
            w.writeheader()
            for rec in rows:
                w.writerow(rec)

    if cmd == "/tp1":
        if len(parts) < 3: 
            reply("مثال: `/tp1 ID 68500`")
            return
        found["status"] = "tp1"
        found["tp1"] = parts[2]
    elif cmd == "/tp2":
        if len(parts) < 3: 
            reply("مثال: `/tp2 ID 69000`")
            return
        found["status"] = "tp2"
        found["tp2"] = parts[2]
    elif cmd == "/sl":
        found["status"] = "stopped"
    elif cmd == "/exit":
        if len(parts) < 3:
            reply("مثال: `/exit ID 0.8R` یا `/exit ID 120$`")
            return
        found["status"] = "closed"
        found["pnl"] = parts[2]
    elif cmd == "/cancel":
        found["status"] = "canceled"
    elif cmd == "/status":
        msg = (
            f"🧾 *Status*\n"
            f"ID: `{found['trade_id']}`\n"
            f"Symbol: {found.get('symbol','')} | TF: {found.get('tf','')} | Setup: {found.get('setup','')}\n"
            f"Decision: {found.get('decision','')} | RR: {found.get('rr','')}\n"
            f"Entry: {found.get('entry_min','')}-{found.get('entry_max','')} | SL: {found.get('sl','')}\n"
            f"TP1: {found.get('tp1','')} | TP2: {found.get('tp2','')}\n"
            f"Status: *{found.get('status','')}* | PnL: {found.get('pnl','')} | Note: {found.get('note','')}"
        )
        reply(msg)
        return
    elif cmd == "/fill":
        if len(parts) < 3:
            reply("مثال: `/fill ID 67890`")
            return
        price = parts[2]
        note = (found.get("note","") + f" | filled@{price}").strip()
        found["note"] = note

    # replace record and save
    for i, rec in enumerate(rows):
        if rec["trade_id"] == trade_id:
            rows[i] = found
    save_rows()
    reply(f"✅ آپدیت انجام شد: *{cmd}* → `{trade_id}`")

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
        if not client:
            return {"ok": False, "where": "openai", "error":"OPENAI_API_KEY not set"}, 500
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
