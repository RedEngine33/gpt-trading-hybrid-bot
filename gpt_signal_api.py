import os
import logging
import traceback
import csv
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file
import requests
from openai import OpenAI

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ===== ENV =====
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY")
BOT_TOKEN        = os.environ.get("BOT_TOKEN")
CHANNEL_ID       = os.environ.get("CHANNEL_ID")              # -100...
JOURNAL_CHANNEL  = os.environ.get("JOURNAL_CHANNEL_ID")      # optional
ALLOWED_CHAT_ID  = os.environ.get("ALLOWED_CHAT_ID")         # optional
PARSE_MODE       = os.environ.get("PARSE_MODE", "Markdown")  # or MarkdownV2
TV_SECRET        = os.environ.get("TV_SECRET")
FORWARD_SWITCH   = os.environ.get("FORWARD_TO_CHANNEL", "0") in ("1","true","True")

ACCOUNT_USDT     = float(os.environ.get("ACCOUNT_USDT", "10000"))
RISK_PCT         = float(os.environ.get("RISK_PCT", "1.0")) / 100.0

JOURNAL_CSV_PATH = os.environ.get("JOURNAL_CSV_PATH", "./trade_journal.csv")

client = OpenAI(api_key=OPENAI_API_KEY)

# ===== Helpers =====
def tg_escape_markdown_v2(text: str) -> str:
    specials = r'_\*\[\]\(\)~`>#+-=|{}.!'
    return ''.join("\\"+ch if ch in specials else ch for ch in text)

def tg_send_message(chat_id: str, message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    text = message if PARSE_MODE != "MarkdownV2" else tg_escape_markdown_v2(message)
    payload = {"chat_id": chat_id, "text": text, "parse_mode": PARSE_MODE, "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=25)
    if r.status_code >= 400:
        raise requests.HTTPError(f"{r.status_code} {r.reason}: {r.text}", response=r)
    return r.json()

def send_signal_to_channel(message: str):
    if not CHANNEL_ID:
        raise RuntimeError("CHANNEL_ID not set")
    return tg_send_message(CHANNEL_ID, message)

def ensure_csv():
    hdr = ["ts_utc","trade_id","source","symbol","tf","direction",
           "entry_min","entry_max","sl","tp1","tp2","rr_target",
           "ema9","ema20","rsi","atr","setup","confidence",
           "status","fill_price","exit_price","pnl_abs","pnl_pct","rr_realized","fees",
           "posted_message_id","note"]
    new_file = not os.path.exists(JOURNAL_CSV_PATH)
    if new_file:
        with open(JOURNAL_CSV_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(hdr)
    return hdr

def csv_read_all():
    ensure_csv()
    rows = []
    with open(JOURNAL_CSV_PATH, "r", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            rows.append(r)
    return rows

def csv_write_all(rows, hdr=None):
    if hdr is None:
        hdr = ensure_csv()
    with open(JOURNAL_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=hdr)
        w.writeheader()
        for r in rows:
            w.writerow(r)

def gen_trade_id(symbol: str, tf: str):
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return f"{symbol}-{tf}-{ts}"

def parse_numbers_from_analysis(analysis: str):
    import re
    lines = [ln.strip() for ln in analysis.splitlines() if ln.strip()]
    d = {"direction":"","entry_min":"","entry_max":"","sl":"","tp1":"","tp2":"","rr_text":""}
    for ln in lines:
        if ln.startswith("1)"):
            d["direction"] = ln.split(":",1)[-1].strip()
        elif ln.startswith("2)"):
            nums = re.findall(r"[\d\.]+", ln)
            if len(nums)>=1: d["entry_min"] = nums[0]
            if len(nums)>=2: d["entry_max"] = nums[1]
        elif ln.startswith("3)"):
            nums = re.findall(r"[\d\.]+", ln);  d["sl"]  = nums[0] if nums else ""
        elif ln.startswith("4)"):
            nums = re.findall(r"[\d\.]+", ln)
            d["tp1"] = nums[0] if len(nums)>=1 else ""
            d["tp2"] = nums[1] if len(nums)>=2 else ""
        elif ln.startswith("5)"):
            d["rr_text"] = ln.replace("5)","").strip()
    return d

def compute_confidence(setup: str, ema9: float, ema20: float, rsi: float, atr: float):
    try:
        atr = max(float(atr), 1e-9)
        ema9 = float(ema9); ema20=float(ema20); rsi=float(rsi)
    except:
        return 50
    trend_strength = min(abs(ema9-ema20)/atr, 2.0)
    if setup == "strong_long":
        rsi_bias = max(0.0, (rsi-50.0)/20.0)
    elif setup == "strong_short":
        rsi_bias = max(0.0, (50.0-rsi)/20.0)
    else:
        rsi_bias = 0.0
    conf = int(round(min(1.0, 0.6*trend_strength/2.0 + 0.4*rsi_bias)*100))
    return conf

def format_signal_message(coin, tf, direction, entry_min, entry_max, sl, tp1, tp2, rr_text, confidence, setup, delta_sl, pos_size):
    dir_clean = direction if direction else "WAIT"
    entry_str = f"{entry_min}" + (f"–{entry_max}" if entry_max else "")
    tp_str    = f"{tp1}" + (f" / {tp2}" if tp2 else "")
    inv_note  = "EMA9 cross ↑ EMA20 یا RSI<45" if (setup == "strong_long") else ("EMA9 cross ↓ EMA20 یا RSI>55" if setup=="strong_short" else "setup invalidated")
    header = f"📊 Signal | {coin} | TF: {tf}\nDirection: {dir_clean}   | Confidence: {confidence}%"
    body   = f"\n\nEntry: {entry_str}\nSL: {sl}\nTP1 / TP2: {tp_str}\n{rr_text}"
    risk   = f"\n\nRisk Plan (Risk={int(RISK_PCT*100)}%): Δ(SL)≈{delta_sl:.2f} → size≈{pos_size:.4f} {coin.replace('USDT','').replace('USD','')}"
    foot   = f"\nInvalidation: {inv_note}\nNote: معامله فقط در محدوده ورود؛ خارج از محدوده → لغو."
    return header + body + risk + foot

# ===== Basics =====
@app.get("/")
def root():   return jsonify({"ok": True, "service": "gpt-trading-full-pack"})

@app.get("/health")
def health(): return jsonify({"status": "healthy"})

@app.get("/diag")
def diag():
    return {
        "OPENAI_API_KEY_set": bool(OPENAI_API_KEY),
        "BOT_TOKEN_set": bool(BOT_TOKEN),
        "CHANNEL_ID_set": bool(CHANNEL_ID),
        "JOURNAL_CHANNEL_set": bool(JOURNAL_CHANNEL),
        "ALLOWED_CHAT_ID_set": bool(ALLOWED_CHAT_ID),
        "PARSE_MODE": PARSE_MODE,
        "TV_SECRET_set": bool(TV_SECRET),
        "FORWARD_TO_CHANNEL": FORWARD_SWITCH,
        "ACCOUNT_USDT": ACCOUNT_USDT,
        "RISK_PCT": RISK_PCT,
        "JOURNAL_CSV_PATH": JOURNAL_CSV_PATH,
        "model": "gpt-4o-mini"
    }

# ===== TradingView webhook (STRICT metrics) =====
@app.post("/tv-alert")
def tv_alert():
    data = request.get_json(silent=True) or {}
    if (data.get("secret") or "") != (TV_SECRET or ""):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    coin    = (data.get("text") or data.get("symbol") or "").upper().strip()
    setup   = (data.get("setup") or (data.get("metrics") or {}).get("setup") or "").lower()
    metrics = data.get("metrics") or {
        "tf": data.get("tf"),
        "close": data.get("price"),
        "ema9": data.get("ema9"),
        "ema20": data.get("ema20"),
        "rsi": data.get("rsi"),
        "atr": data.get("atr"),
        "setup": setup
    }
    if not coin:
        return jsonify({"ok": False, "error": "no symbol"}), 400

    prompt = (
        f"Use ONLY these numbers.\n"
        f"COIN={coin} TF={metrics.get('tf')} CLOSE={metrics.get('close')} "
        f"EMA9={metrics.get('ema9')} EMA20={metrics.get('ema20')} "
        f"RSI={metrics.get('rsi')} ATR={metrics.get('atr')} "
        f"SETUP={metrics.get('setup')}.\n\n"
        "Direction MUST follow SETUP: LONG if strong_long, SHORT if strong_short, otherwise WAIT.\n"
        "Return exactly 6 lines:\n"
        "1) Direction\n2) Entry range\n3) SL zone\n4) TP1/TP2\n5) RR≈1.5–2\n6) One-line risk note.\n"
        "No emojis. Max 8 lines total."
    )
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.1,
            messages=[{"role": "user", "content": prompt}]
        )
        analysis = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        return jsonify({"ok": False, "error": f"openai: {e}"}), 502

    parsed = parse_numbers_from_analysis(analysis)
    tf   = metrics.get("tf") or ""
    try:
        ema9 = float(metrics.get("ema9") or 0.0)
        ema20= float(metrics.get("ema20") or 0.0)
        rsi  = float(metrics.get("rsi") or 0.0)
        atr  = float(metrics.get("atr") or 0.0)
    except:
        ema9=ema20=rsi=atr=0.0

    confidence = compute_confidence(setup, ema9, ema20, rsi, atr)

    try:
        em = float(parsed["entry_min"] or 0.0)
        ex = float(parsed["entry_max"] or 0.0)
        sl = float(parsed["sl"] or 0.0)
        ep_mid = (em+ex)/2.0 if em and ex else em or ex or float(metrics.get("close") or 0.0)
        delta  = abs(ep_mid - sl) if sl else (atr or 0.0)
        pos_size = (ACCOUNT_USDT * RISK_PCT) / max(delta, 1e-9)
    except Exception:
        delta = 0.0
        pos_size = 0.0

    trade_id = gen_trade_id(coin, tf or "NA")
    ensure_csv()
    rows = csv_read_all()
    rows.append({
        "ts_utc": datetime.utcnow().isoformat(timespec="seconds")+"Z",
        "trade_id": trade_id,
        "source": "tv-alert",
        "symbol": coin,
        "tf": tf,
        "direction": parsed["direction"],
        "entry_min": parsed["entry_min"],
        "entry_max": parsed["entry_max"],
        "sl": parsed["sl"],
        "tp1": parsed["tp1"],
        "tp2": parsed["tp2"],
        "rr_target": parsed["rr_text"],
        "ema9": str(ema9),
        "ema20": str(ema20),
        "rsi": str(rsi),
        "atr": str(atr),
        "setup": setup,
        "confidence": str(confidence),
        "status": "new",
        "fill_price": "",
        "exit_price": "",
        "pnl_abs": "",
        "pnl_pct": "",
        "rr_realized": "",
        "fees": "",
        "posted_message_id": "",
        "note": ""
    })
    csv_write_all(rows)

    msg = format_signal_message(
        coin, tf, parsed["direction"],
        parsed["entry_min"], parsed["entry_max"],
        parsed["sl"], parsed["tp1"], parsed["tp2"],
        parsed["rr_text"], confidence, setup, delta, pos_size
    )
    try:
        sent = send_signal_to_channel(f"{msg}\n\nID: `{trade_id}`")
        message_id = str((sent.get("result") or {}).get("message_id") or "")
    except Exception as e:
        return jsonify({"ok": False, "error": f"telegram: {e}"}), 502

    rows = csv_read_all()
    for r in rows:
        if r["trade_id"] == trade_id:
            r["posted_message_id"] = message_id
            break
    csv_write_all(rows)

    if JOURNAL_CHANNEL:
        try:
            jtxt = (
                f"🗒 Journal | {coin} | {tf} | id={trade_id}\n"
                f"Dir:{parsed['direction']} Conf:{confidence}% Setup:{setup} "
                f"| EMA9:{ema9} EMA20:{ema20} RSI:{rsi} ATR:{atr}\n"
                f"Entry:{parsed['entry_min']}{'–'+parsed['entry_max'] if parsed['entry_max'] else ''} "
                f"| SL:{parsed['sl']} | TP1/TP2:{parsed['tp1']}{'/'+parsed['tp2'] if parsed['tp2'] else ''}"
            )
            tg_send_message(JOURNAL_CHANNEL, jtxt)
        except Exception:
            app.logger.exception("journal channel send error")

    return jsonify({"ok": True, "trade_id": trade_id, "coin": coin, "telegram_message_id": message_id})

# ===== Telegram webhook: commands + simple chat (reply only) =====
@app.post("/tg-webhook")
def tg_webhook():
    update = request.get_json(silent=True) or {}
    message = (update.get("message") or update.get("edited_message") or {})
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if not chat_id:
        return {"ok": True}
    if ALLOWED_CHAT_ID and str(chat_id) != str(ALLOWED_CHAT_ID):
        return {"ok": True}
    # --- PHOTO (chart) support ---
    photos = message.get("photo") or []
    if photos:
        try:
            # بزرگ‌ترین سایز عکس
            largest = sorted(photos, key=lambda p: p.get("file_size", 0))[-1]
            file_id = largest.get("file_id")

            # گرفتن آدرس فایل از تلگرام
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
            caption = (message.get("caption") or "").strip()
            coin_guess = caption.split()[0].upper() if caption else ""

            vision_prompt = (
                "Analyze this trading chart briefly and return exactly 6 lines:\n"
                "1) Direction (LONG/SHORT/WAIT)\n"
                "2) Entry range\n"
                "3) SL zone\n"
                "4) TP1/TP2\n"
                "5) RR≈1.5–2\n"
                "6) One-line risk note.\n"
                "No emojis. Max 8 lines."
            )
            if caption:
                vision_prompt += f"\nUser context: {caption}"

            # OpenAI Vision (gpt-4o-mini) با image_url
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0.2,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": vision_prompt},
                        {"type": "image_url", "image_url": {"url": file_url}}
                    ]
                }]
            )
            analysis = (resp.choices[0].message.content or "").strip()
            header = f"🖼️ Vision Analysis{f' for *{coin_guess}*' if coin_guess else ''}:"
            reply(f"{header}\n\n{analysis}")

            # (اختیاری) لاگ به ژورنال CSV
            try:
                ensure_csv()
                rows = csv_read_all()
                rows.append({
                    "ts_utc": datetime.utcnow().isoformat(timespec="seconds")+"Z",
                    "trade_id": gen_trade_id(coin_guess or "IMG", "N/A"),
                    "source": "tg-webhook-vision",
                    "symbol": coin_guess or "N/A",
                    "tf": "N/A",
                    "direction": "",
                    "entry_min": "", "entry_max": "", "sl": "",
                    "tp1": "", "tp2": "",
                    "rr_target": "",
                    "ema9": "", "ema20": "", "rsi": "", "atr": "",
                    "setup": "", "confidence": "",
                    "status": "new",
                    "fill_price": "", "exit_price": "",
                    "pnl_abs": "", "pnl_pct": "", "rr_realized": "", "fees": "",
                    "posted_message_id": "", "note": f"vision; caption={caption}"
                })
                csv_write_all(rows)
            except Exception:
                app.logger.exception("journal append failed for vision")

            return {"ok": True}
        except Exception as e:
            app.logger.exception("photo handling error")
            reply("❌ پردازش عکس نشد. لطفاً دوباره بفرست یا کپشن کوتاه اضافه کن.")
            return {"ok": False}, 200
    text = (message.get("text") or "").strip()

    def reply(msg: str):
        try:
            tg_send_message(chat_id, msg)
        except Exception:
            app.logger.exception("reply error")

    try:
        if text.startswith("/"):
            parts = text.split()
            cmd = parts[0].lower()
            if cmd in ("/fill","/tp1","/tp2","/sl","/exit","/status","/cancel"):
                if len(parts) < 2:
                    reply("Usage: /fill TRADE_ID [price]\nOther: /tp1 id | /tp2 id | /sl id | /exit id price | /cancel id | /status id")
                    return {"ok": True}
                trade_id = parts[1]
                price = float(parts[2]) if len(parts)>=3 else None

                rows = csv_read_all()
                found = None
                for r in rows:
                    if r["trade_id"] == trade_id:
                        found = r; break
                if not found:
                    reply(f"Trade not found: {trade_id}")
                    return {"ok": True}

                if cmd == "/fill":
                    if price is None: reply("Usage: /fill TRADE_ID PRICE")
                    else:
                        found["status"] = "filled"; found["fill_price"] = str(price)
                        reply(f"✅ Filled | {trade_id} @ {price}")
                elif cmd == "/tp1":
                    found["status"] = "tp1"; reply(f"✅ TP1 | {trade_id}")
                elif cmd == "/tp2":
                    found["status"] = "tp2"; found["exit_price"] = found["tp2"]; found["rr_realized"] = found["rr_target"]; reply(f"✅ TP2 | {trade_id}")
                elif cmd == "/sl":
                    found["status"] = "sl"; found["exit_price"] = found["sl"]; reply(f"❌ SL | {trade_id}")
                elif cmd == "/exit":
                    if price is None: reply("Usage: /exit TRADE_ID PRICE")
                    else:
                        found["status"] = "exit"; found["exit_price"] = str(price); reply(f"🔚 Exit | {trade_id} @ {price}")
                elif cmd == "/cancel":
                    found["status"] = "cancel"; reply(f"🚫 Cancel | {trade_id}")
                elif cmd == "/status":
                    reply(f"Status | {trade_id}: {found.get('status')}")

                try:
                    fp = float(found.get("fill_price") or 0.0)
                    xp = float(found.get("exit_price") or 0.0)
                    if fp and xp:
                        direction = (found.get("direction") or "").upper()
                        pnl = (xp - fp) if direction=="LONG" else (fp - xp)
                        found["pnl_abs"] = f"{pnl:.6f}"
                        found["pnl_pct"] = f"{(pnl / fp * 100.0):.3f}" if fp else ""
                except Exception:
                    pass

                csv_write_all(rows)
                return {"ok": True}

            if cmd in ("/help", "/start"):
                reply("Commands:\n/fill id price\n/tp1 id\n/tp2 id\n/sl id\n/exit id price\n/cancel id\n/status id")
                return {"ok": True}

        if text:
            base_prompt = (
                "Provide a compact crypto plan.\n"
                "Return exactly 6 lines:\n"
                "1) Direction (LONG/SHORT/WAIT)\n2) Entry range\n3) SL zone\n"
                "4) TP1/TP2\n5) RR≈1.5–2\n6) One-line risk note.\nNo emojis."
            )
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0.2,
                messages=[{"role":"user","content": base_prompt + f"\nSymbol: {text}"}]
            )
            analysis = (resp.choices[0].message.content or "").strip()
            reply(f"📊 *GPT Analysis for {text.upper()}:*\n\n{analysis}")
            return {"ok": True}
        return {"ok": True}

    except Exception as e:
        app.logger.exception("tg-webhook error")
        try: reply(f"❌ Error: {e}")
        except Exception: pass
        return {"ok": False}, 200

# ===== Stats & Export =====
@app.get("/stats")
def stats():
    rng = (request.args.get("range") or "7d").lower()
    now = datetime.utcnow()
    if rng == "today": since = datetime(now.year, now.month, now.day)
    elif rng == "30d": since = now - timedelta(days=30)
    else: since = now - timedelta(days=7)

    def _to_float(x, d=None):
        try: return float(x)
        except: return d
    def _avg(seq):
        vals = [v for v in (_to_float(x) for x in seq) if v is not None]
        if not vals: return None
        return sum(vals)/len(vals)

    rows = csv_read_all()
    sel = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["ts_utc"].replace("Z",""))
            if ts >= since: sel.append(r)
        except: sel.append(r)

    total = len(sel)
    wins = sum(1 for r in sel if r.get("status") in ("tp1","tp2","exit") and (float(r.get("pnl_abs") or 0.0) > 0))
    losses = sum(1 for r in sel if r.get("status") in ("sl","exit") and (float(r.get("pnl_abs") or 0.0) < 0))
    filled = sum(1 for r in sel if r.get("status") in ("filled","tp1","tp2","sl","exit"))
    avg_rr = _avg([_to_float(r.get("rr_realized") or r.get("rr_target")) for r in sel if (r.get("rr_realized") or r.get("rr_target"))])
    avg_pnl_pct = _avg([_to_float(r.get("pnl_pct")) for r in sel if r.get("pnl_pct")])

    return jsonify({
        "range": rng, "since_utc": since.isoformat()+"Z",
        "total_trades": total, "filled_or_closed": filled,
        "wins": wins, "losses": losses,
        "win_rate_pct": round((wins / max(1, wins+losses)) * 100.0, 2),
        "avg_rr": round(avg_rr, 3) if avg_rr is not None else None,
        "avg_pnl_pct": round(avg_pnl_pct, 3) if avg_pnl_pct is not None else None
    })

@app.get("/export")
def export_csv():
    ensure_csv()
    return send_file(JOURNAL_CSV_PATH, as_attachment=True, download_name="trade_journal.csv")

# ===== Pings =====
@app.get("/ping-tg")
def ping_tg():
    try:
        res = send_signal_to_channel("✅ Test: Telegram is connected.")
        return {"ok": True, "telegram": res}
    except Exception as e:
        return {"ok": False, "where": "telegram", "error": str(e)}, 500

@app.get("/ping-openai")
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
