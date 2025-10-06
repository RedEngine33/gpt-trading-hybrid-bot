import os
import logging
import traceback
import time
from datetime import datetime, timedelta
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

# External data API keys
COINGLASS_API_KEY     = os.environ.get("COINGLASS_API_KEY")
GLASSNODE_API_KEY     = os.environ.get("GLASSNODE_API_KEY")
CRYPTOPANIC_API_TOKEN = os.environ.get("CRYPTOPANIC_API_TOKEN")
NEWS_FEEDS            = os.environ.get("NEWS_FEEDS", "")

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

def _safe_get(url, headers=None, params=None, timeout=12):
    try:
        r = requests.get(url, headers=headers or {}, params=params or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        app.logger.warning(f"GET fail {url}: {e}")
        return None

# --- Simple in-memory cache to avoid rate limits ---
_CACHE = {}
def cache_get(key, ttl=60):
    v = _CACHE.get(key)
    if not v: return None
    val, ts = v
    if time.time() - ts > ttl:
        _CACHE.pop(key, None)
        return None
    return val

def cache_set(key, val):
    _CACHE[key] = (val, time.time())

# -------- Coinglass: funding / OI / liq heat --------
def fetch_coinglass(pair="BTCUSDT"):
    """Return dict: funding_rate, open_interest_change, liq_note (best-effort)."""
    if not COINGLASS_API_KEY:
        return {"funding_rate": None, "open_interest_change": None, "liq_note": None}

    cache_key = f"coinglass:{pair}"
    hit = cache_get(cache_key, ttl=60)
    if hit: return hit

    headers = {"coinglassSecret": COINGLASS_API_KEY}
    out = {"funding_rate": None, "open_interest_change": None, "liq_note": None}
    symbol = pair.replace("USDT","").replace("USD","")

    # Funding (endpoint/shape may vary by plan; adjust to your docs)
    fr = _safe_get("https://open-api.coinglass.com/api/futures/funding_rate",
                   headers=headers, params={"symbol": symbol})
    if fr and fr.get("data"):
        try:
            # try uMarginList or generic field
            rate = None
            row = fr["data"][0]
            if "uMarginList" in row and row["uMarginList"]:
                rate = row["uMarginList"][0].get("rate")
            rate = rate if rate is not None else row.get("rate")
            if rate is not None:
                out["funding_rate"] = float(rate)
        except Exception:
            pass

    # Open Interest change (24h)
    oi = _safe_get("https://open-api.coinglass.com/api/futures/open_interest",
                   headers=headers, params={"symbol": symbol})
    if oi and oi.get("data"):
        try:
            out["open_interest_change"] = oi["data"][0].get("change24h")
        except Exception:
            pass

    # Liquidation heatmap (placeholder if no endpoint in plan)
    out["liq_note"] = "liq clusters near key levels (check heatmap)"

    cache_set(cache_key, out)
    return out

# -------- Glassnode: on-chain (in/out flow, whales) --------
def fetch_glassnode(asset="BTC"):
    """Return dict: exchange_inflow, exchange_outflow, whale_activity (best-effort)."""
    if not GLASSNODE_API_KEY:
        return {"exchange_inflow": None, "exchange_outflow": None, "whale_activity": None}

    cache_key = f"glassnode:{asset}"
    hit = cache_get(cache_key, ttl=300)
    if hit: return hit

    base = "https://api.glassnode.com/v1/metrics"
    params = {"api_key": GLASSNODE_API_KEY, "a": asset, "s": int((datetime.utcnow()-timedelta(days=2)).timestamp())}

    # Endpoints may vary by plan; adjust with your docs
    inflow = _safe_get(f"{base}/signals/exchange_inflow", params=params)
    outflow = _safe_get(f"{base}/signals/exchange_outflow", params=params)

    def _last_value(arr):
        if isinstance(arr, list) and arr:
            return arr[-1].get("v")
        return None

    res = {
        "exchange_inflow": _last_value(inflow),
        "exchange_outflow": _last_value(outflow),
        "whale_activity": "unknown"
    }
    cache_set(cache_key, res)
    return res

# -------- Simple News: CryptoPanic API or RSS fallback --------
def fetch_news_summary():
    """Return short news summary dict with headline/sentiment/impact (best-effort)."""
    cache_key = "news:short"
    hit = cache_get(cache_key, ttl=120)
    if hit: return hit

    # CryptoPanic (if token provided)
    if CRYPTOPANIC_API_TOKEN:
        jp = _safe_get("https://cryptopanic.com/api/v1/posts/",
                       params={"auth_token": CRYPTOPANIC_API_TOKEN, "filter": "news", "kind": "news"})
        if jp and jp.get("results"):
            top = jp["results"][0]
            out = {
                "headline": top.get("title"),
                "sentiment": top.get("vote", {}).get("value", "neutral"),
                "impact": "medium"
            }
            cache_set(cache_key, out)
            return out

    # RSS fallback (very naive title extraction)
    feeds = [u.strip() for u in NEWS_FEEDS.split(",") if u.strip()]
    for url in feeds:
        try:
            txt = requests.get(url, timeout=8).text
            start = txt.find("<title>")
            end = txt.find("</title>", start+7)
            if start != -1 and end != -1:
                title = txt[start+7:end].strip()
                if title:
                    out = {"headline": title[:120], "sentiment":"neutral", "impact":"low"}
                    cache_set(cache_key, out)
                    return out
        except Exception as e:
            app.logger.warning(f"RSS fail {url}: {e}")

    return {"headline": None, "sentiment": None, "impact": None}

def generate_gpt_analysis(coin_name: str, extra_context: str = "") -> str:
    """Analysis using GPT with combined context (TV + Coinglass + Glassnode + News)."""
    base_prompt = (
        f"You are a professional crypto analyst. Combine technical context with:\n"
        f"- Coinglass (funding, OI, liquidations)\n"
        f"- Glassnode (exchange flows, whales)\n"
        f"- News sentiment\n\n"
        f"for {coin_name}. Decide: LONG / SHORT / NO-TRADE.\n"
        f"Return strictly:\n"
        f"1) Decision, 2) Entry zone, 3) SL, 4) TP1/TP2, 5) Two-bullet rationale, 6) One-line risk note.\n"
        f"Max 9 lines. No emojis."
    )
    if extra_context:
        base_prompt += f"\nContext:\n{extra_context}\n"

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
    return jsonify({"ok": True, "service": "gpt-trading-hybrid-bot (enriched)"})

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
        "COINGLASS_API_KEY_set": bool(COINGLASS_API_KEY),
        "GLASSNODE_API_KEY_set": bool(GLASSNODE_API_KEY),
        "CRYPTOPANIC_API_TOKEN_set": bool(CRYPTOPANIC_API_TOKEN),
        "NEWS_FEEDS_len": len([u for u in NEWS_FEEDS.split(',') if u.strip()]),
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
        # Enrich here as well (optional)
        asset = (coin.replace("USDT","").replace("USD","") or "BTC")
        cg = fetch_coinglass(coin)
        gn = fetch_glassnode(asset)
        nw = fetch_news_summary()
        ext_ctx = (
            f"COINGLASS: funding={cg.get('funding_rate')}, OI_change={cg.get('open_interest_change')}, liq='{cg.get('liq_note')}'. "
            f"GLASSNODE: inflow={gn.get('exchange_inflow')}, outflow={gn.get('exchange_outflow')}, whales='{gn.get('whale_activity')}'. "
            f"NEWS: headline='{nw.get('headline')}', sentiment={nw.get('sentiment')}, impact={nw.get('impact')}'. "
            f"USER: {extra}"
        )

        analysis = generate_gpt_analysis(coin, ext_ctx)
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
        # --- Enrich with external data ---
        asset = (coin.replace("USDT","").replace("USD","") or "BTC")
        cg = fetch_coinglass(coin)
        gn = fetch_glassnode(asset)
        nw = fetch_news_summary()

        ext_ctx = (
            f"COINGLASS: funding={cg.get('funding_rate')}, OI_change={cg.get('open_interest_change')}, liq='{cg.get('liq_note')}'. "
            f"GLASSNODE: inflow={gn.get('exchange_inflow')}, outflow={gn.get('exchange_outflow')}, whales='{gn.get('whale_activity')}'. "
            f"NEWS: headline='{nw.get('headline')}', sentiment={nw.get('sentiment')}, impact={nw.get('impact')}. "
            f"TV_CONTEXT: {extra}"
        )

        analysis = generate_gpt_analysis(coin, ext_ctx)
        msg = f"📊 *GPT Analysis for {coin}:*\n\n{analysis}"
        res = send_signal_to_channel(msg)
        return jsonify({"ok": True, "coin": coin, "telegram": res, "coinglass": cg, "glassnode": gn, "news": nw}), 200
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
                "Return compact playbook: Decision (LONG/SHORT/WAIT), Entry zone, SL zone, TP1/TP2, RR≈1.5–2, one risk note. "
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

        # also enrich text-only requests
        asset = (coin.replace("USDT","").replace("USD","") or "BTC")
        cg = fetch_coinglass(coin)
        gn = fetch_glassnode(asset)
        nw = fetch_news_summary()
        ext_ctx = (
            f"COINGLASS: funding={cg.get('funding_rate')}, OI_change={cg.get('open_interest_change')}, liq='{cg.get('liq_note')}'. "
            f"GLASSNODE: inflow={gn.get('exchange_inflow')}, outflow={gn.get('exchange_outflow')}, whales='{gn.get('whale_activity')}'. "
            f"NEWS: headline='{nw.get('headline')}', sentiment={nw.get('sentiment')}, impact={nw.get('impact')}. "
            f"USER: {extra}"
        )

        analysis = generate_gpt_analysis(coin, ext_ctx)
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
