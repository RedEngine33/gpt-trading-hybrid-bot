
# GPT Trading Hybrid Bot — Minimal-Pro + Vision + Text

نسخه‌ی کامل برای جایگزینی مستقیم:  
- **TradingView Webhook** → سیگنال خودکار  
- **FreeData (Binance) + CryptoPanic** → تقویت کانتکست  
- **Guards** (forbidden hours, cooldown, dedup, daily risk cap)  
- **Journal** (in-memory + CSV)  
- **Telegram Vision** → ارسال اسکرین‌شات TV/Coinglass و گرفتن سیگنال  
- **Telegram Text** → با نوشتن دستور ساده، سیگنال بگیر

---

## 1) نصب (Render/…)

### requirements.txt
```
flask==3.0.3
requests==2.32.3
gunicorn==21.2.0
```

### Procfile (برای Render/Heroku)
```
web: gunicorn gpt_signal_api:app --bind 0.0.0.0:$PORT
```

### .env.example
```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
BOT_TOKEN=123456:ABC-DEF...
CHANNEL_ID=-100111222333
JOURNAL_CHANNEL_ID=
TV_SECRET=your_secret
FORWARD_TO_CHANNEL=1

CRYPTOPANIC_API_TOKEN=

NEWS_BLOCK_ENABLED=1
COOLDOWN_SECONDS=300
DEDUP_WINDOW_SECONDS=180
QUALITY_MIN_SCORE=1
FORBIDDEN_UTC_HOURS=0-3
RISK_PER_TRADE_PCT=2.0
MAX_DAILY_RISK_PCT=6.0
```

### اجرا محلی
```bash
pip install -r requirements.txt
python gpt_signal_api.py
```

Health:
```
GET /diag
GET /ping-openai
GET /ping-tg
GET /journal
GET /journal/export.csv
```

---

## 2) TradingView (Pine v5)

```pinescript
//@version=5
indicator("TV → GPT Router (EMA9/20 + RSI14 + MTF flag)", overlay=true)
emaFast = ta.ema(close, 9)
emaSlow = ta.ema(close, 20)
rsi     = ta.rsi(close, 14)

longCond  = ta.crossover(emaFast, emaSlow) and rsi > 55 and close > emaFast
shortCond = ta.crossunder(emaFast, emaSlow) and rsi < 45 and close < emaFast
setup     = longCond ? "strong_long" : shortCond ? "strong_short" : "neutral"

mtfFlag = "MTF=OK"

msg = str.format(
  '{{"secret":"{0}","text":"{1}","context":"TF={2}; {8}; close={3}; ema9={4}; ema20={5}; rsi14={6}; setup={7}"}}',
  "YOUR_TV_SECRET", syminfo.ticker, timeframe.period, close, emaFast, emaSlow, rsi, setup, mtfFlag
)

if longCond or shortCond
    alert(msg, alert.freq_once_per_bar_close)
```

Alert:
- Condition: اندیکاتور بالا (Any alert() function call)  
- Webhook URL: `https://YOUR-APP.onrender.com/tv-alert`  
- Message: **خالی**  
- ENV سرور: `TV_SECRET` = همان مقدار `"YOUR_TV_SECRET"`

تست:
```bash
curl -X POST "https://YOUR-APP.onrender.com/tv-alert"  -H "Content-Type: application/json"  -d '{"secret":"your_secret","text":"BTCUSDT","context":"TF=15m; setup=strong_long; close=67890"}'
```

---

## 3) Telegram — Vision + Text

### setWebhook
یکبار تنظیم کن:
```
https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=https://YOUR-APP.onrender.com/tg-webhook
```

### ارسال تصویر
- اسکرین‌شات TV/Coinglass را بفرست.  
- در کپشن ترجیحاً بنویس:  
  `BTCUSDT 15m strong_long 67890`  
- بات تصویر را می‌خواند، ویژن اجرا می‌کند، سیگنال می‌دهد و ژورنال می‌کند.

### متن ساده (بدون تصویر)
- در چت بات بفرست:  
  `/signal BTCUSDT 15m strong_long 67890`  
  یا حتی بدون فرمان:  
  `BTCUSDT 15m strong_long 67890`  
- بات FreeData/News/Guards را اعمال می‌کند → خروجی استاندارد + ژورنال.

---

## 4) ژورنال
- In-memory: `GET /journal?n=20`  
- CSV: `/mnt/data/trade_journal.csv` (دانلود: `GET /journal/export.csv`)  
- اگر `JOURNAL_CHANNEL_ID` ست شود، یک کپی خلاصه به کانال آرشیو می‌رود.

---

## 5) نکته‌ها
- نبود `OPENAI_API_KEY` → خروجی تستی `WAIT` برای چک مسیر.  
- نبود CryptoPanic → News score = 0 و بلاک خبری غیرفعال.  
- liq «پروکسی» است (بدون سرویس پولی).  
- آستانه‌ها را با ENV تغییر بده (COOLDOWN/QUALITY/…).
