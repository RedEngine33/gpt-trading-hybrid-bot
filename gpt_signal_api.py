
# gpt_signal_api.py
# Minimal-Pro+Vision+Text: Binance FreeData + CryptoPanic + Journal + Guards + TV webhook + Telegram Vision & Text
# Deploy: Render / Replit / PythonAnywhere
# Python 3.10+

import os
import time
import csv
import json
import hashlib
import logging
from datetime import datetime, timezone, date
from typing import Dict, Any, Optional

import requests
from flask import Flask, request, jsonify

# ------------------------- Config & Logging -------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gpt-signal-api")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
BOT_TOKEN      = os.getenv("BOT_TOKEN")
CHANNEL_ID     = os.getenv("CHANNEL_ID")  # main signals channel
JOURNAL_CHANNEL_ID = os.getenv("JOURNAL_CHANNEL_ID")  # optional archive channel
TV_SECRET      = os.getenv("TV_SECRET", "change_me")

# Guards / knobs
NEWS_BLOCK_ENABLED = os.getenv("NEWS_BLOCK_ENABLED", "1") == "1"
COOLDOWN_SECONDS   = int(os.getenv("COOLDOWN_SECONDS", "300"))
DEDUP_WINDOW_SECONDS = int(os.getenv("DEDUP_WINDOW_SECONDS", "180"))
QUALITY_MIN_SCORE  = int(os.getenv("QUALITY_MIN_SCORE", "1"))
FORBIDDEN_UTC_HOURS = os.getenv("FORBIDDEN_UTC_HOURS", "0-3")
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "2.0"))
MAX_DAILY_RISK_PCT = float(os.getenv("MAX_DAILY_RISK_PCT", "6.0"))
FORWARD_TO_CHANNEL = os.getenv("FORWARD_TO_CHANNEL", "1") == "1"

# News
CRYPTOPANIC_TOKEN = os.getenv("CRYPTOPANIC_API_TOKEN")

# Storage
JOURNAL_CSV_PATH = os.getenv("JOURNAL_CSV_PATH", "./trade_journal.csv")
os.makedirs(os.path.dirname(JOURNAL_CSV_PATH) or ".", exist_ok=True)

# Runtime state
_news_cache = {"t": 0, "by_symbol": {}}
_last_sig_time: Dict[str, float] = {}
_last_sig_hash: Dict[str, tuple[str, float]] = {}
_daily_risk_used: float = 0.0
_daily_risk_day: Optional[date] = None
_journal_mem: list[Dict[str, Any]] = []  # in-memory window

# Telegram API base
TG_API_BASE = "https://api.telegram.org"

# HTTP session
_session = requests.Session()
_session.headers.update({"User-Agent": "gpt-trading-bot/1.2"})

# ------------------------- Helpers -------------------------

def utc_now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def forbidden_hours() -> bool:
    rng = FORBIDDEN_UTC_HOURS or ""
    if not rng:
        return False
    h = datetime.utcnow().hour
    for part in rng.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            if a.strip().isdigit() and b.strip().isdigit():
                A, B = int(a), int(b)
                if A <= h <= B:
                    return True
        else:
            if part.isdigit() and int(part) == h:
                return True
    return False

def throttle_and_dedup(symbol: str, payload: str) -> Optional[str]:
    now = time.time()
    t0 = _last_sig_time.get(symbol, 0)
    if now - t0 < COOLDOWN_SECONDS:
        return f"COOLDOWN active ({int(COOLDOWN_SECONDS - (now - t0))}s left)"
    h = hashlib.md5(payload.encode("utf-8")).hexdigest()
    prev = _last_sig_hash.get(symbol)
    if prev:
        prev_h, prev_t = prev
        if prev_h == h and now - prev_t < DEDUP_WINDOW_SECONDS:
            return "DUPLICATE within window"
    _last_sig_time[symbol] = now
    _last_sig_hash[symbol] = (h, now)
    return None

def reset_daily_risk_if_new_day():
    global _daily_risk_day, _daily_risk_used
    today = datetime.now(timezone.utc).date()
    if _daily_risk_day != today:
        _daily_risk_day = today
        _daily_risk_used = 0.0

def daily_risk_allowed() -> bool:
    reset_daily_risk_if_new_day()
    return (_daily_risk_used + RISK_PER_TRADE_PCT) <= MAX_DAILY_RISK_PCT

def apply_loss_to_risk_budget(loss_happened: bool):
    global _daily_risk_used
    reset_daily_risk_if_new_day()
    if loss_happened:
        _daily_risk_used += RISK_PER_TRADE_PCT

# ------------------------- FreeData (Binance) -------------------------

def binance_funding_rate(symbol: str) -> Optional[float]:
    url = "https://fapi.binance.com/fapi/v1/premiumIndex"
    try:
        r = _session.get(url, params={"symbol": symbol}, timeout=5)
        r.raise_for_status()
        data = r.json()
        fr = data.get("lastFundingRate")
        return float(fr) if fr is not None else None
    except Exception as e:
        log.warning(f"funding error: {e}")
        return None

def binance_global_ls_ratio(interval="5m") -> Optional[float]:
    url = "https://fapi.binance.com/futures/data/topLongShortAccountRatio"
    try:
        r = _session.get(url, params={"period": interval, "symbol": "BTCUSDT", "limit": 1}, timeout=5)
        r.raise_for_status()
        arr = r.json()
        if isinstance(arr, list) and arr:
            return float(arr[-1]["longAccount"]) / float(arr[-1]["shortAccount"])
        return None
    except Exception as e:
        log.warning(f"lsr error: {e}")
        return None

_liq_cache = {"t": 0, "count": 0}
def binance_liquidations_recent(ttl=60) -> int:
    now = time.time()
    if now - _liq_cache["t"] < ttl:
        return _liq_cache["count"]
    url = "https://fapi.binance.com/fapi/v1/aggTrades"
    try:
        r = _session.get(url, params={"symbol": "BTCUSDT", "limit": 50}, timeout=5)
        r.raise_for_status()
        data = r.json()
        count = sum(1 for t in data if float(t.get("q", 0)) * float(t.get("p", 0)) > 2_000_000)
    except Exception as e:
        log.warning(f"liq proxy error: {e}")
        count = 0
    _liq_cache["t"] = now
    _liq_cache["count"] = count
    return count

# ------------------------- CryptoPanic -------------------------

def fetch_cryptopanic(symbol: str, max_posts: int = 10, ttl: int = 60):
    now = time.time()
    key = symbol.upper()
    if now - _news_cache["t"] < ttl and key in _news_cache["by_symbol"]:
        return _news_cache["by_symbol"][key]
    if not CRYPTOPANIC_TOKEN:
        return []
    try:
        params = {
            "auth_token": CRYPTOPANIC_TOKEN,
            "currencies": key,
            "filter": "rising",
            "regions": "en",
            "public": "true",
        }
        url = "https://cryptopanic.com/api/v1/posts/"
        r = _session.get(url, params=params, timeout=6)
        r.raise_for_status()
        data = r.json().get("results", [])[:max_posts]
    except Exception as e:
        log.warning(f"cryptopanic error: {e}")
        data = []
    mapped = []
    for it in data:
        title = (it.get("title") or "")[:200]
        labels = it.get("labels") or []
        kind = it.get("kind")
        if kind: labels.append(kind)
        txt = (title + " " + " ".join(labels)).lower()
        score = 0
        if "bullish" in txt: score += 1
        if "bearish" in txt: score -= 1
        if "important" in txt: score += 1
        mapped.append({"title": title, "score": score})
    _news_cache["t"] = now
    _news_cache["by_symbol"][key] = mapped
    return mapped

def news_signal(symbol: str) -> dict:
    posts = fetch_cryptopanic(symbol)
    tot = sum(p["score"] for p in posts)
    block = NEWS_BLOCK_ENABLED and tot <= -2
    brief = "; ".join([f'{p["title"][:80]}({p["score"]:+d})' for p in posts[:5]])
    return {"news_score": tot, "news_brief": brief, "block": block}

# ------------------------- Quality Score -------------------------

def quality_score(setup: str, funding: Optional[float], lsr: Optional[float], liq_recent: Optional[int]) -> int:
    score = 0
    long_bias = setup == "strong_long"
    short_bias = setup == "strong_short"
    if funding is not None:
        if long_bias and funding < 0.0005: score += 1
        if short_bias and funding > -0.0005: score += 1
    if lsr is not None:
        if long_bias and lsr < 1.0: score += 1
        if short_bias and lsr > 1.0: score += 1
    if liq_recent is not None and liq_recent >= 1: score += 1
    return score

# ------------------------- OpenAI -------------------------

def build_prompt(symbol, tf, setup, price, freedata, news) -> str:
    lines = [
        "You are a disciplined crypto trader. Decide ONLY one of: LONG, SHORT, WAIT.",
        f"Symbol: {symbol} | TF: {tf} | Setup(TV): {setup} | Price: {price}",
        f"FreeData → funding: {freedata.get('funding')}, L/S ratio 5m: {freedata.get('lsr_5m')}, liq_recent: {freedata.get('liq_recent')}",
        f"News → score: {news.get('news_score')} | brief: {str(news.get('news_brief') or '')[:300]}",
        "",
        "Rules:",
        "- Prefer WAIT if data conflicts or risk is elevated.",
        "- If GO: RR≈1.5–2, give Entry, SL, TP1, TP2.",
        "- Provide exactly 2 concise reasons + 1 risk note.",
        "",
        "Output (strict):",
        "Decision: LONG/SHORT/WAIT",
        "Entry: <number>",
        "SL: <number>",
        "TP1: <number>",
        "TP2: <number>",
        "RR: <number 1.3..2.2>",
        "Why: 1) ... 2) ...",
        "Risk: ...",
    ]
    return "\n".join(lines)

def openai_chat(prompt: str) -> str:
    if not OPENAI_API_KEY:
        return "Decision: WAIT\nEntry: 0\nSL: 0\nTP1: 0\nTP2: 0\nRR: 1.5\nWhy: 1) openai_key_missing 2) fallback\nRisk: unavailable"
    try:
        resp = _session.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": OPENAI_MODEL,
                "messages": [
                    {"role": "system", "content": "You output strictly in the requested schema."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.2,
                "max_tokens": 350
            },
            timeout=25
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"openai error: {e}")
        return "Decision: WAIT\nEntry: 0\nSL: 0\nTP1: 0\nTP2: 0\nRR: 1.5\nWhy: 1) openai_error 2) fallback\nRisk: unavailable"

# ---- Vision ----

def tg_get_file_url(file_id: str) -> Optional[str]:
    if not BOT_TOKEN:
        return None
    try:
        r = _session.get(f"{TG_API_BASE}/bot{BOT_TOKEN}/getFile", params={"file_id": file_id}, timeout=8)
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]
        return f"{TG_API_BASE}/file/bot{BOT_TOKEN}/{file_path}"
    except Exception as e:
        log.warning(f"getFile failed: {e}")
        return None

def openai_vision_decide(image_url: str, caption: str = "") -> str:
    if not OPENAI_API_KEY:
        return ("Decision: WAIT\nEntry: 0\nSL: 0\nTP1: 0\nTP2: 0\nRR: 1.5\n"
                "Why: 1) openai_key_missing 2) fallback\nRisk: unavailable")
    try:
        prompt = (
            "Extract trading signal from the screenshot. You MUST output exactly this schema:\n"
            "Decision: LONG/SHORT/WAIT\nEntry: <number>\nSL: <number>\nTP1: <number>\nTP2: <number>\n"
            "RR: <number 1.3..2.2>\nWhy: 1) ... 2) ...\nRisk: ...\n"
            "Use prudent RR≈1.5–2. If unclear → WAIT. Caption (hint): " + (caption or "none")
        )
        payload = {
            "model": OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": "Return ONLY the schema requested."},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]}
            ],
            "temperature": 0.2,
            "max_tokens": 350
        }
        r = _session.post("https://api.openai.com/v1/chat/completions",
                          headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                                   "Content-Type": "application/json"},
                          json=payload, timeout=35)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"openai vision error: {e}")
        return ("Decision: WAIT\nEntry: 0\nSL: 0\nTP1: 0\nTP2: 0\nRR: 1.5\n"
                "Why: 1) openai_vision_error 2) fallback\nRisk: unavailable")

# ------------------------- Parse & Journal -------------------------

def parse_signal(text: str) -> Dict[str, Any]:
    out = {"Decision": "WAIT", "Entry": 0.0, "SL": 0.0, "TP1": 0.0, "TP2": 0.0, "RR": 1.5,
           "Why": "", "Risk": ""}
    try:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for ln in lines:
            low = ln.lower()
            if low.startswith("decision:"):
                v = ln.split(":", 1)[1].strip().upper()
                if v in ("LONG", "SHORT", "WAIT"):
                    out["Decision"] = v
            elif low.startswith("entry:"):
                out["Entry"] = float(ln.split(":", 1)[1].strip())
            elif low.startswith("sl:"):
                out["SL"] = float(ln.split(":", 1)[1].strip())
            elif low.startswith("tp1:"):
                out["TP1"] = float(ln.split(":", 1)[1].strip())
            elif low.startswith("tp2:"):
                out["TP2"] = float(ln.split(":", 1)[1].strip())
            elif low.startswith("rr:"):
                out["RR"] = float(ln.split(":", 1)[1].strip())
            elif low.startswith("why:"):
                out["Why"] = ln.split(":", 1)[1].strip()
            elif low.startswith("risk:"):
                out["Risk"] = ln.split(":", 1)[1].strip()
    except Exception as e:
        log.warning(f"parse error: {e}")
    return out

def ensure_csv_headers():
    if not os.path.exists(JOURNAL_CSV_PATH):
         os.makedirs(os.path.dirname(JOURNAL_CSV_PATH) or ".", exist_ok=True)
        with open(JOURNAL_CSV_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts", "symbol", "tf", "setup", "price",
                        "decision", "entry", "sl", "tp1", "tp2", "rr",
                        "why", "risk", "funding", "lsr_5m", "liq_recent", "news_score"])

def append_journal(row: Dict[str, Any]):
    ensure_csv_headers()
    _journal_mem.append(row)
    if len(_journal_mem) > 300:
        _journal_mem[:] = _journal_mem[-300:]
    try:
        with open(JOURNAL_CSV_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([row.get("ts"), row.get("symbol"), row.get("tf"), row.get("setup"), row.get("price"),
                        row.get("decision"), row.get("entry"), row.get("sl"), row.get("tp1"), row.get("tp2"), row.get("rr"),
                        row.get("why"), row.get("risk"), row.get("funding"), row.get("lsr_5m"), row.get("liq_recent"), row.get("news_score")])
    except Exception as e:
        log.warning(f"csv write error: {e}")

def tg_send_message(text: str, channel_id: Optional[str] = None):
    if not BOT_TOKEN or not (CHANNEL_ID or channel_id):
        return False
    cid = channel_id or CHANNEL_ID
    try:
        url = f"{TG_API_BASE}/bot{BOT_TOKEN}/sendMessage"
        r = _session.post(url, data={"chat_id": cid, "text": text, "parse_mode": "HTML"}, timeout=10)
        ok = r.status_code == 200
        if not ok:
            log.warning(f"tg send failed {r.status_code}: {r.text}")
        return ok
    except Exception as e:
        log.warning(f"tg error: {e}")
        return False

# ------------------------- Core Signal Flow -------------------------

def run_signal_flow(symbol: str, tf: str, setup: str, price: float, context_text: str = "") -> Dict[str, Any]:
    if forbidden_hours():
        return {"status": "WAIT", "reason": "forbidden_hours"}
    payload_for_hash = f"{symbol}|{tf}|{setup}|{price}|{context_text}"
    ded = throttle_and_dedup(symbol, payload_for_hash)
    if ded:
        return {"status": "WAIT", "reason": ded}
    if not daily_risk_allowed():
        return {"status": "WAIT", "reason": "daily_risk_cap"}

    funding = binance_funding_rate(symbol)
    lsr    = binance_global_ls_ratio("5m")
    liqs   = binance_liquidations_recent()
    news   = news_signal(symbol)

    q = quality_score(setup, funding, lsr, liqs)
    if news["block"]:
        return {"status": "WAIT", "reason": "negative_news_block"}
    if q < QUALITY_MIN_SCORE:
        return {"status": "WAIT", "reason": f"quality<{QUALITY_MIN_SCORE}"}

    freedata = {"funding": funding, "lsr_5m": lsr, "liq_recent": liqs}
    prompt = build_prompt(symbol, tf, setup, price, freedata, news)
    llm_out = openai_chat(prompt)
    parsed = parse_signal(llm_out)

    row = {
        "ts": utc_now_str(),
        "symbol": symbol, "tf": tf, "setup": setup, "price": price,
        "decision": parsed["Decision"], "entry": parsed["Entry"], "sl": parsed["SL"],
        "tp1": parsed["TP1"], "tp2": parsed["TP2"], "rr": parsed["RR"],
        "why": parsed["Why"], "risk": parsed["Risk"],
        "funding": funding, "lsr_5m": lsr, "liq_recent": liqs, "news_score": news["news_score"],
    }
    append_journal(row)

    if FORWARD_TO_CHANNEL and CHANNEL_ID:
        msg = (
            f"⏱ <b>{row['ts']}</b>\n"
            f"🎯 <b>{symbol}</b> | TF: <b>{tf}</b> | Setup: <b>{setup}</b>\n"
            f"💡 Decision: <b>{row['decision']}</b> | RR≈{row['rr']}\n"
            f"📈 Entry: <code>{row['entry']}</code> | SL: <code>{row['sl']}</code>\n"
            f"🎯 TP1: <code>{row['tp1']}</code> | TP2: <code>{row['tp2']}</code>\n"
            f"🧠 Why: {row['why']}\n"
            f"⚠️ Risk: {row['risk']}\n"
            f"📊 Data→ funding: {funding} | L/S(5m): {lsr} | liq: {liqs} | news: {news['news_score']}"
        )
        tg_send_message(msg, CHANNEL_ID)
        if JOURNAL_CHANNEL_ID:
            tg_send_message("[JOURNAL ARCHIVE]\n" + msg, JOURNAL_CHANNEL_ID)

    return {"status": "OK", "row": row, "llm_raw": llm_out}

def run_signal_from_vision(symbol_hint: str, tf_hint: str, setup_hint: str,
                           price_hint: float, image_url: str, caption: str = "") -> Dict[str, Any]:
    funding = binance_funding_rate(symbol_hint or "BTCUSDT")
    lsr    = binance_global_ls_ratio("5m")
    liqs   = binance_liquidations_recent()

    raw = openai_vision_decide(image_url, caption)
    parsed = parse_signal(raw)

    sym = (symbol_hint or "BTCUSDT").upper()
    tf  = tf_hint or "15m"
    setup = setup_hint or "neutral"
    price = price_hint or 0.0

    row = {
        "ts": utc_now_str(),
        "symbol": sym, "tf": tf, "setup": setup, "price": price,
        "decision": parsed["Decision"], "entry": parsed["Entry"], "sl": parsed["SL"],
        "tp1": parsed["TP1"], "tp2": parsed["TP2"], "rr": parsed["RR"],
        "why": parsed["Why"], "risk": parsed["Risk"],
        "funding": funding, "lsr_5m": lsr, "liq_recent": liqs, "news_score": 0,
    }
    append_journal(row)

    if FORWARD_TO_CHANNEL and CHANNEL_ID:
        msg = (
            f"🖼 <b>Vision Signal</b>\n"
            f"⏱ <b>{row['ts']}</b>\n"
            f"🎯 <b>{sym}</b> | TF: <b>{tf}</b> | Setup: <b>{setup}</b>\n"
            f"💡 Decision: <b>{row['decision']}</b> | RR≈{row['rr']}\n"
            f"📈 Entry: <code>{row['entry']}</code> | SL: <code>{row['sl']}</code>\n"
            f"🎯 TP1: <code>{row['tp1']}</code> | TP2: <code>{row['tp2']}</code>\n"
            f"🧠 Why: {row['why']}\n"
            f"⚠️ Risk: {row['risk']}\n"
            f"📊 Data→ funding: {funding} | L/S(5m): {lsr} | liq: {liqs}"
        )
        tg_send_message(msg, CHANNEL_ID)
        if JOURNAL_CHANNEL_ID:
            tg_send_message("[JOURNAL ARCHIVE]\n" + msg, JOURNAL_CHANNEL_ID)

    return {"status": "OK", "row": row, "llm_raw": raw}

# ------------------------- Flask Endpoints -------------------------

@app.route("/diag")
def diag():
    return jsonify({
        "ok": True,
        "has_openai_key": bool(OPENAI_API_KEY),
        "has_bot": bool(BOT_TOKEN and CHANNEL_ID),
        "news_enabled": bool(CRYPTOPANIC_TOKEN),
        "forbidden_hours": FORBIDDEN_UTC_HOURS,
        "cooldown_s": COOLDOWN_SECONDS,
        "dedup_s": DEDUP_WINDOW_SECONDS,
        "quality_min": QUALITY_MIN_SCORE,
        "risk_per_trade_pct": RISK_PER_TRADE_PCT,
        "max_daily_risk_pct": MAX_DAILY_RISK_PCT,
    })

@app.route("/ping-openai")
def ping_openai():
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "error": "OPENAI_API_KEY missing"}), 400
    return jsonify({"ok": True, "model": OPENAI_MODEL})

@app.route("/ping-tg")
def ping_tg():
    if not (BOT_TOKEN and CHANNEL_ID):
        return jsonify({"ok": False, "error": "BOT_TOKEN or CHANNEL_ID missing"}), 400
    return jsonify({"ok": True})

@app.route("/journal")
def journal_list():
    n = int(request.args.get("n", "20"))
    data = _journal_mem[-n:] if n > 0 else _journal_mem[:]
    return jsonify({"n": len(data), "items": data})

@app.route("/journal/export.csv")
def journal_export():
    if not os.path.exists(JOURNAL_CSV_PATH):
        ensure_csv_headers()
    try:
        with open(JOURNAL_CSV_PATH, "rb") as f:
            content = f.read()
        return app.response_class(content, mimetype="text/csv")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/gpt-signal", methods=["POST"])
def gpt_signal():
    try:
        data = request.get_json(force=True)
        symbol = (data.get("symbol") or "BTCUSDT").upper()
        tf     = data.get("tf") or "15m"
        setup  = data.get("setup") or "neutral"
        price  = float(data.get("price") or 0)
        ctx    = data.get("context") or ""
        res = run_signal_flow(symbol, tf, setup, price, ctx)
        return jsonify(res)
    except Exception as e:
        log.exception("gpt-signal error")
        return jsonify({"error": str(e)}), 500

@app.route("/tv-alert", methods=["POST"])
def tv_alert():
    try:
        data = request.get_json(force=True)
        secret = data.get("secret")
        if secret != TV_SECRET:
            return jsonify({"error": "bad secret"}), 403
        text = data.get("text") or "BTCUSDT"
        ctx  = data.get("context") or ""
        symbol = text.upper()
        tf = "15m"
        setup = "neutral"
        price = 0.0
        parts = [p.strip() for p in ctx.split(";") if p.strip()]
        for p in parts:
            if p.startswith("TF="):
                tf = p.split("=", 1)[1]
            elif p.startswith("setup="):
                setup = p.split("=", 1)[1]
            elif p.startswith("close="):
                try:
                    price = float(p.split("=", 1)[1])
                except:
                    pass
        res = run_signal_flow(symbol, tf, setup, price, ctx)
        return jsonify(res)
    except Exception as e:
        log.exception("tv-alert error")
        return jsonify({"error": str(e)}), 500

@app.route("/tg-webhook", methods=["POST"])
def tg_webhook():
    try:
        upd = request.get_json(force=True)
        msg = upd.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        caption = msg.get("caption") or ""
        # 1) Photo → Vision flow
        if "photo" in msg and msg["photo"]:
            best = sorted(msg["photo"], key=lambda x: x.get("file_size", 0))[-1]
            file_id = best.get("file_id")
            url = tg_get_file_url(file_id)
            parts = caption.split()
            sym  = parts[0] if len(parts) > 0 else "BTCUSDT"
            tf   = parts[1] if len(parts) > 1 else "15m"
            setup= parts[2] if len(parts) > 2 else "neutral"
            try:
                price = float(parts[3]) if len(parts) > 3 else 0.0
            except:
                price = 0.0
            if url:
                res = run_signal_from_vision(sym, tf, setup, price, url, caption)
                return jsonify({"ok": True, "res": res})
            return jsonify({"ok": False, "error": "no_url"}), 400

        # 2) Text → Direct signal flow ("/signal ..." or plain text "BTCUSDT 15m ...")
        text = msg.get("text") or ""
        if text:
            parts = text.replace("\n", " ").split()
            # detect command
            if parts and parts[0].lower() in ("/signal", "/gpt", "/analyze", "signal"):
                parts = parts[1:]
            sym  = parts[0] if len(parts) > 0 else "BTCUSDT"
            tf   = parts[1] if len(parts) > 1 else "15m"
            setup= parts[2] if len(parts) > 2 else "neutral"
            try:
                price = float(parts[3]) if len(parts) > 3 else 0.0
            except:
                price = 0.0
            ctx = " ".join(parts[4:]) if len(parts) > 4 else ""
            res = run_signal_flow(sym.upper(), tf, setup, price, ctx)
            # optional echo to same chat
            if chat_id:
                row = res.get("row") or {}
                if row:
                    msgtxt = (
                        f"💬 <b>Text Signal</b>\n"
                        f"🎯 {row.get('symbol')} | TF {row.get('tf')} | Setup {row.get('setup')}\n"
                        f"Decision: <b>{row.get('decision')}</b> | RR≈{row.get('rr')}\n"
                        f"Entry: <code>{row.get('entry')}</code> | SL: <code>{row.get('sl')}</code> | TP1: <code>{row.get('tp1')}</code> | TP2: <code>{row.get('tp2')}</code>"
                    )
                    _session.post(f"{TG_API_BASE}/bot{BOT_TOKEN}/sendMessage",
                                  data={"chat_id": chat_id, "text": msgtxt, "parse_mode": "HTML"}, timeout=10)
            return jsonify({"ok": True, "res": res})

        return jsonify({"ok": True})
    except Exception as e:
        log.exception("tg-webhook error")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/")
def root():
    return jsonify({"hello": "gpt-trading-bot", "time": utc_now_str()})

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
