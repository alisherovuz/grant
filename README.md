# EduGrands Telegram Bot (Railway-ready)

Bu bot private chatga yuborilgan opportunity matnini Claude API orqali tekshiradi va natijani private Telegram guruhga jo'natadi.
Endi bot URL bo'lsa sahifadan matnni ham avtomatik olib, tahlilga qo'shadi.
Shuningdek, oldindan berilgan source URL'larni ham bot o'zi skan qila oladi.

## 1) BotFather orqali bot ochish

1. Telegram'da `@BotFather` ga kiring.
2. `/newbot` buyrug'ini bering.
3. Olingan tokenni saqlang (`TELEGRAM_BOT_TOKEN`).

## 2) Private guruhni tayyorlash

1. Private guruh yarating yoki mavjud guruhga botni qo'shing.
2. Botni guruhda admin qiling (message yubora olishi kerak).
3. Guruh `chat_id` sini oling:
   - Botga private chatdan biror xabar yuboring.
   - Quyidagini ishga tushiring:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates"
```

`"chat":{"id":-100...}` qiymatini `TARGET_CHAT_ID` ga qo'ying.

## 3) Claude API key

`CLAUDE_API_KEY` (yoki `ANTHROPIC_API_KEY`) yarating va envga qo'ying.

## 4) Local ishga tushirish

```bash
cd telegram_bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

## 5) Railway deploy

Railway'da yangi service oching va repo ulang.

`Settings -> Variables` da quyidagi env larni kiriting:
- `TELEGRAM_BOT_TOKEN`
- `CLAUDE_API_KEY` (yoki `ANTHROPIC_API_KEY`)
- `CLAUDE_MODEL` (masalan `claude-3-5-sonnet-latest`)
- `TARGET_CHAT_ID` (masalan `-1001234567890`)
- `BOT_TIMEZONE` (`Asia/Tashkent`)
- `SOURCE_URLS` (vergul yoki yangi qatorda source linklar)
- `AUTO_SCAN_INTERVAL_MINUTES` (masalan `60`, `0` bo'lsa auto off)
- `LLM_TIMEOUT_SECONDS` (masalan `60`)
- `MAX_CANDIDATES_PER_SOURCE` (masalan `8`)
- `SCAN_MAX_EVALUATIONS` (masalan `20`)
- `ENABLE_SPELLCHECK` (`1` yoki `0`)
- `MONTHLY_SCHEDULE` (masalan `1:Botirjon,Nargiza;2:Elbek,Muattar`)
- `MEMBER_USERNAMES` (masalan `Botirjon=@botirjon_uz,Nargiza=@nargiza_uz`)
- `DAILY_REMINDER_HOUR` (masalan `9`)
- `DAILY_REMINDER_MINUTE` (masalan `0`)

Start command:

```bash
cd telegram_bot && pip install -r requirements.txt && python bot.py
```

Yoki Railway Procfile o'qisa, `telegram_bot/Procfile`dagi worker ishlaydi.

## Foydalanish

1. Botga private chatdan URL + raw text yuboring.
2. Bot opportunity'ni baholaydi.
3. Natija private guruhga yuboriladi:
   - `eligible=true` bo'lsa: EduGrands formatdagi post
   - `eligible=false` bo'lsa: qisqa sabab

URL rejimi:
- Faqat URL yuborsangiz ham bot sahifadan matn ajratib olishga harakat qiladi.
- Agar sayt himoyalangan bo'lsa yoki matn ajralmasa, bot siz yuborgan matn bilan davom etadi.

Auto source rejimi:
- `/scan` komandasi: source URL'lardan darhol bir martalik scan qiladi.
- `/status` komandasi: bot bandmi (`busy`) yoki bo'shmi (`idle`) ko'rsatadi.
- `/duty` komandasi: bugungi navbatchilar va username mentionlarni ko'rsatadi.
- `AUTO_SCAN_INTERVAL_MINUTES>0` bo'lsa bot periodik scan ham qiladi.
- Avval tekshirilgan linklar `seen_links.json` fayliga yoziladi, qayta yubormaydi.
- Uzoq ishlash va xarajatni boshqarish uchun limit env'lardan foydalaning.

Schedule format:
- `MONTHLY_SCHEDULE`: `kun:ism,ism;kun:ism,ism`
- Misol: `3:Botirjon,Nargiza;4:Elbek,Muattar;5:Botirjon,Elbek`
- `MEMBER_USERNAMES`: `Ism=@username` ko'rinishida yoziladi

Imlo tekshiruvi:
- Eligible post yuborilishidan oldin bot avtomatik imlo tekshiradi.
- Natija alohida xabar bo'lib chiqadi: `xatolar tuzatildi` yoki `xato topilmadi`.
