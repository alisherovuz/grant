import json
import logging
import os
import re
from html import escape
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from anthropic import Anthropic
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("edugrands-bot")
SEEN_LINKS_FILE = "seen_links.json"


@dataclass
class EvaluationResult:
    eligible: bool
    reason: str
    post: str


@dataclass
class ScanStats:
    sources_total: int = 0
    candidates_total: int = 0
    checked_total: int = 0
    eligible_total: int = 0


def get_required_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"{key} env o'zgaruvchisi topilmadi")
    return value


def extract_first_url(text: str) -> str:
    match = re.search(r"https?://\S+", text)
    return match.group(0).rstrip(").,;!?\"]") if match else "Noma'lum"


def sanitize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def fetch_url_content(url: str, max_chars: int = 12000) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Yaroqsiz URL")

    response = requests.get(
        url,
        timeout=20,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        },
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    for tag_name in [
        "script",
        "style",
        "noscript",
        "svg",
        "nav",
        "footer",
        "header",
        "form",
        "aside",
    ]:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    title = sanitize_text(soup.title.get_text()) if soup.title and soup.title.get_text() else ""
    main = soup.find("main") or soup.find("article") or soup.body or soup
    paragraphs = [sanitize_text(p.get_text(" ", strip=True)) for p in main.find_all(["p", "li"])]
    paragraphs = [p for p in paragraphs if len(p) > 40]

    combined = "\n".join(paragraphs)
    if not combined:
        combined = sanitize_text(main.get_text(" ", strip=True))

    if title:
        combined = f"Sarlavha: {title}\n\n{combined}"

    return combined[:max_chars]


def parse_source_urls(raw: str | None) -> list[str]:
    if not raw:
        return []
    chunks = re.split(r"[\n,]+", raw)
    urls = []
    for chunk in chunks:
        item = chunk.strip()
        if item.startswith("http://") or item.startswith("https://"):
            urls.append(item)
    return list(dict.fromkeys(urls))


def extract_candidate_links(source_url: str, html: str, max_links: int = 20) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    keywords = (
        "scholar",
        "grant",
        "fellow",
        "olymp",
        "competition",
        "program",
        "intern",
        "stipend",
        "apply",
        "admission",
        "call",
        "opportunit",
        "award",
        "course",
    )
    candidates: list[str] = []

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
            continue
        full_url = urljoin(source_url, href)
        parsed = urlparse(full_url)
        if parsed.scheme not in {"http", "https"}:
            continue
        haystack = f"{full_url} {a.get_text(' ', strip=True)}".lower()
        if any(k in haystack for k in keywords):
            candidates.append(full_url)

    unique = list(dict.fromkeys(candidates))
    return unique[:max_links]


def load_seen_links() -> set[str]:
    if not os.path.exists(SEEN_LINKS_FILE):
        return set()
    try:
        with open(SEEN_LINKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(str(x) for x in data)
    except Exception:
        logger.warning("seen_links.json ni o'qib bo'lmadi, bo'sh ro'yxat ishlatiladi")
    return set()


def save_seen_links(links: set[str]) -> None:
    with open(SEEN_LINKS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(links), f, ensure_ascii=False, indent=2)


def safe_json_extract(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])

    raise ValueError("Modeldan yaroqli JSON olinmadi")


def evaluate_opportunity(client: Anthropic, model: str, today: str, source_url: str, raw_text: str) -> EvaluationResult:
    user_prompt = USER_PROMPT_TEMPLATE.format(
        today=today,
        url=source_url,
        raw_text=raw_text.strip(),
    )

    response = client.messages.create(
        model=model,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=2500,
        temperature=0,
    )

    parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))

    content = "\n".join(parts).strip()
    parsed = safe_json_extract(content)

    eligible = bool(parsed.get("eligible", False))
    reason = str(parsed.get("reason", "") or "").strip()
    post = str(parsed.get("post", "") or "").strip()

    if eligible and not post:
        raise ValueError("eligible=true bo'lsa ham post bo'sh qaytdi")

    if not eligible and not reason:
        reason = "Aniq sabab qaytarilmadi"

    return EvaluationResult(eligible=eligible, reason=reason, post=post)


def format_group_message(result: EvaluationResult, source_url: str, from_user: str) -> tuple[str, str | None]:
    safe_url = escape(source_url)
    safe_user = escape(from_user)

    if result.eligible:
        text = (
            "<b>Yangi mos imkoniyat topildi</b>\n"
            f"Manba: {safe_url}\n"
            f"Yuboruvchi: {safe_user}"
        )
        return text, None

    text = (
        "<b>Imkoniyat mos emas</b>\n"
        f"Manba: {safe_url}\n"
        f"Yuboruvchi: {safe_user}\n"
        f"Sabab: {escape(result.reason)}"
    )
    return text, None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Opportunity matnini yuboring. Bot uni tekshiradi va natijani private guruhga jo'natadi.\n\n"
        "Format erkin: URL + tavsif matni bo'lsa yetarli.\n"
        "Agar SOURCE_URLS berilgan bo'lsa, /scan komandasi bilan bot manbalarni o'zi skan qiladi."
    )


async def run_source_scan(context: ContextTypes.DEFAULT_TYPE, notify_chat_id: int | None = None) -> ScanStats:
    app = context.application
    client: Anthropic = app.bot_data["llm_client"]
    model: str = app.bot_data["llm_model"]
    tz: ZoneInfo = app.bot_data["tz"]
    source_urls: list[str] = app.bot_data.get("source_urls", [])
    target_chat_id: str | None = app.bot_data.get("target_chat_id")
    destination_chat_id = str(notify_chat_id) if notify_chat_id is not None else target_chat_id

    stats = ScanStats(sources_total=len(source_urls))
    if not source_urls:
        if destination_chat_id:
            await context.bot.send_message(
                chat_id=destination_chat_id,
                text="SOURCE_URLS bo'sh. .env da manbalarni kiriting.",
            )
        return stats

    seen_links = load_seen_links()
    today = datetime.now(tz).date().isoformat()

    for source_url in source_urls:
        try:
            listing_resp = requests.get(
                source_url,
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            listing_resp.raise_for_status()
            candidates = extract_candidate_links(source_url, listing_resp.text)
        except Exception as exc:
            logger.warning("Source o'qilmadi: %s | %s", source_url, exc)
            continue

        stats.candidates_total += len(candidates)

        for candidate_url in candidates:
            if candidate_url in seen_links:
                continue

            seen_links.add(candidate_url)
            stats.checked_total += 1

            try:
                raw_text = fetch_url_content(candidate_url)
                result = evaluate_opportunity(
                    client=client,
                    model=model,
                    today=today,
                    source_url=candidate_url,
                    raw_text=raw_text,
                )
            except Exception as exc:
                logger.warning("Candidate tekshirilmadi: %s | %s", candidate_url, exc)
                continue

            if result.eligible and destination_chat_id:
                stats.eligible_total += 1
                try:
                    await context.bot.send_message(
                        chat_id=destination_chat_id,
                        text=(
                            "<b>Auto-scan: mos imkoniyat topildi</b>\n"
                            f"Manba: {escape(candidate_url)}"
                        ),
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                    await context.bot.send_message(
                        chat_id=destination_chat_id,
                        text=result.post,
                        disable_web_page_preview=True,
                    )
                except Exception as exc:
                    logger.warning("Auto-scan natija yuborilmadi: %s", exc)

    save_seen_links(seen_links)
    return stats


async def scan_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text("Source scan boshlandi...")
    stats = await run_source_scan(context=context, notify_chat_id=update.effective_chat.id)
    await update.message.reply_text(
        "Scan tugadi.\n"
        f"Manbalar: {stats.sources_total}\n"
        f"Topilgan kandidatlar: {stats.candidates_total}\n"
        f"Tekshirilganlar: {stats.checked_total}\n"
        f"Eligible: {stats.eligible_total}"
    )


async def auto_scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    stats = await run_source_scan(context=context)
    logger.info(
        "Auto-scan done | sources=%s candidates=%s checked=%s eligible=%s",
        stats.sources_total,
        stats.candidates_total,
        stats.checked_total,
        stats.eligible_total,
    )


async def analyze_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if not text:
        return

    await update.message.reply_text("Tahlil boshlandi...")

    app = context.application
    client: Anthropic = app.bot_data["llm_client"]
    model: str = app.bot_data["llm_model"]
    tz: ZoneInfo = app.bot_data["tz"]
    target_chat_id: str | None = app.bot_data.get("target_chat_id")

    source_url = extract_first_url(text)
    today = datetime.now(tz).date().isoformat()
    raw_text_for_ai = text

    if source_url != "Noma'lum":
        try:
            fetched_text = fetch_url_content(source_url)
            if fetched_text:
                raw_text_for_ai = (
                    f"{text}\n\n"
                    "Quyida URLdan olingan matn:\n"
                    f"{fetched_text}"
                )
                await update.message.reply_text("URLdan matn olindi, endi AI tahlil qilmoqda...")
        except Exception as exc:
            logger.warning("URLdan matn olishda xatolik: %s", exc)
            await update.message.reply_text("URLdan matnni avtomatik olishda xatolik bo'ldi, mavjud matn bilan davom etyapman.")

    try:
        result = evaluate_opportunity(
            client=client,
            model=model,
            today=today,
            source_url=source_url,
            raw_text=raw_text_for_ai,
        )
    except Exception as exc:
        logger.exception("Tahlilda xatolik")
        await update.message.reply_text(f"Xatolik: {exc}")
        return

    status_line = "✅ ELIGIBLE" if result.eligible else "❌ INELIGIBLE"
    await update.message.reply_text(
        f"Natija: {status_line}\n"
        f"Sabab: {result.reason or '—'}"
    )

    if target_chat_id:
        from_user = update.effective_user.full_name if update.effective_user else "Noma'lum"
        group_message, _ = format_group_message(result, source_url, from_user)

        try:
            await context.bot.send_message(
                chat_id=target_chat_id,
                text=group_message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

            if result.eligible:
                await context.bot.send_message(
                    chat_id=target_chat_id,
                    text=result.post,
                    disable_web_page_preview=True,
                )

            await update.message.reply_text("Natija private guruhga yuborildi.")
        except Exception as exc:
            logger.exception("Guruhga yuborishda xatolik")
            await update.message.reply_text(f"Guruhga yuborishda xatolik: {exc}")
    else:
        await update.message.reply_text("TARGET_CHAT_ID o'rnatilmagan. Faqat sizga javob yuborildi.")


def main() -> None:
    telegram_bot_token = get_required_env("TELEGRAM_BOT_TOKEN")
    claude_api_key = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not claude_api_key:
        raise RuntimeError("CLAUDE_API_KEY yoki ANTHROPIC_API_KEY env o'zgaruvchisi topilmadi")
    claude_model = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-latest")
    timezone_name = os.getenv("BOT_TIMEZONE", "Asia/Tashkent")
    target_chat_id = os.getenv("TARGET_CHAT_ID")
    source_urls = parse_source_urls(os.getenv("SOURCE_URLS"))
    auto_scan_interval_minutes = int(os.getenv("AUTO_SCAN_INTERVAL_MINUTES", "0"))

    tz = ZoneInfo(timezone_name)
    client = Anthropic(api_key=claude_api_key)

    application = Application.builder().token(telegram_bot_token).build()

    application.bot_data["llm_client"] = client
    application.bot_data["llm_model"] = claude_model
    application.bot_data["tz"] = tz
    application.bot_data["target_chat_id"] = target_chat_id
    application.bot_data["source_urls"] = source_urls

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("scan", scan_sources))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analyze_message))

    if auto_scan_interval_minutes > 0:
        application.job_queue.run_repeating(
            auto_scan_job,
            interval=auto_scan_interval_minutes * 60,
            first=20,
            name="auto-source-scan",
        )

    logger.info(
        "Bot ishga tushdi | source_urls=%s | auto_scan_interval_minutes=%s",
        len(source_urls),
        auto_scan_interval_minutes,
    )
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
