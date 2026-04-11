import json
import logging
import os
import re
import asyncio
from html import escape
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from typing import Any
from urllib.parse import urljoin, urlparse, parse_qs, unquote, quote_plus
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
SPELLCHECK_SYSTEM_PROMPT = """You are an Uzbek Latin spelling and grammar editor.
You will receive one Telegram post text.
Task:
1) Fix only spelling, punctuation, and minor grammar issues.
2) Keep original meaning unchanged.
3) Preserve structure, markdown, emojis, line breaks, bullets, and links.
4) Do not add new facts.
Return JSON only:
{
  "changed": true or false,
  "corrected_post": "full corrected text",
  "notes": ["short note 1", "short note 2"]
}
"""
CMS_DRAFT_SYSTEM_PROMPT = """You are a CMS content structuring assistant for an education opportunities website.
Given program name and multiple source texts/links, create a single structured draft.
Rules:
1) Use ONLY facts that appear in sources.
2) If unsure, use \"UNKNOWN\".
3) Keep output concise and factual.
4) All long text fields should be in Uzbek Latin.
5) For categorical fields, choose ONLY from allowed options below, otherwise \"UNKNOWN\".

Allowed values:
Imkoniyat turi: To'liq ta'lim, Almashinuv dasturi, Konfrensiya, Yozgi maktab, Til kursi, Volontyorlik, Amaliyot, Work and Travel, Tadqiqot, Malaka oshirish, Xalqaro Musobaqalar, UNKNOWN
Daraja: Almashinuv (Maktab), Bakalavr, Almashinuv (Bakalavr), Magistratura, PhD, Professional rivojlanish, UNKNOWN
Moliyalashtirish: To'liq moliyalash, Qisman moliyalash, O'z-o'zini moliyalash, Bepul ishtirok, Stipendiya, UNKNOWN
Format: Offlayn, Onlayn, Gibrid (Onlayn & Offlayn), UNKNOWN
Davomiylik: Juda qisqa (1-5 kun), Qisqa (1-3 hafta), O'rta (1-2 oy), Kengaytirilgan (3-9 oy), Uzoq (1-2 yil), Juda uzoq (3+ yil), UNKNOWN
Ariza to'lovi: Bor, Yo'q, UNKNOWN

Return JSON only with this schema:
{
  "title": "",
  "country": "",
  "official_link": "",
  "registration_link": "",
  "deadline_type": "Regular",
  "deadline": "",
  "opening_date": "",
  "imkoniyat_turi": "",
  "daraja": "",
  "moliyalashtirish": "",
  "format": "",
  "davomiylik": "",
  "ariza_tolovi": "",
  "description": "",
  "eligibility": "",
  "benefits": "",
  "application_process": "",
  "additional_information": "",
  "sources": [],
  "confidence": "high|medium|low",
  "notes": ""
}
"""
PUBLISH_STATE_KEY = "publish_wizard"
ALLOWED_UNKNOWN = "UNKNOWN"
IMKONIYAT_TURI_OPTIONS = {
    "To'liq ta'lim",
    "Almashinuv dasturi",
    "Konfrensiya",
    "Yozgi maktab",
    "Til kursi",
    "Volontyorlik",
    "Amaliyot",
    "Work and Travel",
    "Tadqiqot",
    "Malaka oshirish",
    "Xalqaro Musobaqalar",
    ALLOWED_UNKNOWN,
}
DARAJA_OPTIONS = {
    "Almashinuv (Maktab)",
    "Bakalavr",
    "Almashinuv (Bakalavr)",
    "Magistratura",
    "PhD",
    "Professional rivojlanish",
    ALLOWED_UNKNOWN,
}
MOLIYALASHTIRISH_OPTIONS = {
    "To'liq moliyalash",
    "Qisman moliyalash",
    "O'z-o'zini moliyalash",
    "Bepul ishtirok",
    "Stipendiya",
    ALLOWED_UNKNOWN,
}
FORMAT_OPTIONS = {"Offlayn", "Onlayn", "Gibrid (Onlayn & Offlayn)", ALLOWED_UNKNOWN}
DAVOMIYLIK_OPTIONS = {
    "Juda qisqa (1-5 kun)",
    "Qisqa (1-3 hafta)",
    "O'rta (1-2 oy)",
    "Kengaytirilgan (3-9 oy)",
    "Uzoq (1-2 yil)",
    "Juda uzoq (3+ yil)",
    ALLOWED_UNKNOWN,
}
ARIZA_TOLOVI_OPTIONS = {"Bor", "Yo'q", ALLOWED_UNKNOWN}


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


@dataclass
class SpellcheckResult:
    changed: bool
    corrected_post: str
    notes: list[str]


@dataclass
class PublishDraftResult:
    data: dict[str, Any]
    used_sources: list[str]


def get_required_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"{key} env o'zgaruvchisi topilmadi")
    return value


def extract_first_url(text: str) -> str:
    match = re.search(r"https?://\S+", text)
    return match.group(0).rstrip(").,;!?\"]") if match else "Noma'lum"


def extract_all_urls(text: str) -> list[str]:
    urls = re.findall(r"https?://\S+", text or "")
    cleaned = [u.rstrip(").,;!?\"]") for u in urls]
    return list(dict.fromkeys(cleaned))


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


def clean_search_result_url(href: str) -> str:
    href = href.strip()
    if href.startswith("//"):
        href = f"https:{href}"
    if href.startswith("/l/?"):
        query = parse_qs(urlparse(href).query)
        if "uddg" in query and query["uddg"]:
            return unquote(query["uddg"][0])
    return href


def search_program_sources(program_name: str, max_results: int = 6) -> list[str]:
    query = quote_plus(program_name)
    search_url = f"https://duckduckgo.com/html/?q={query}"
    resp = requests.get(
        search_url,
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    urls: list[str] = []
    for a in soup.select("a.result__a"):
        href = a.get("href", "").strip()
        if not href:
            continue
        cleaned = clean_search_result_url(href)
        if cleaned.startswith("http://") or cleaned.startswith("https://"):
            urls.append(cleaned)
        if len(urls) >= max_results:
            break
    return list(dict.fromkeys(urls))


def parse_monthly_schedule(raw: str | None) -> dict[int, list[str]]:
    schedule: dict[int, list[str]] = {}
    if not raw:
        return schedule

    lines = [ln.strip() for ln in re.split(r"[;\n]+", raw) if ln.strip()]
    for line in lines:
        match = re.match(r"^\s*(\d{1,2})\s*[:=-]\s*(.+)$", line)
        if not match:
            continue
        day = int(match.group(1))
        if day < 1 or day > 31:
            continue
        names = [n.strip() for n in match.group(2).split(",") if n.strip()]
        if names:
            schedule[day] = names
    return schedule


def parse_member_usernames(raw: str | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not raw:
        return mapping

    pairs = [p.strip() for p in re.split(r"[;\n,]+", raw) if p.strip()]
    for pair in pairs:
        if "=" not in pair:
            continue
        name, username = pair.split("=", 1)
        n = name.strip()
        u = username.strip()
        if not n or not u:
            continue
        if not u.startswith("@"):
            u = f"@{u}"
        mapping[n] = u
    return mapping


def build_duty_message(
    duty_date: datetime,
    duty_names: list[str],
    username_map: dict[str, str],
) -> str:
    mentions = [username_map.get(name, name) for name in duty_names]

    day_label = duty_date.strftime("%d-%m-%Y")
    lines = [
        "Bugungi navbatchilar:",
        f"Sana: {day_label}",
        ", ".join(duty_names),
        "",
    ]
    lines.extend(mentions or ["(username topilmadi)"])

    return "\n".join(lines)


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


def spellcheck_post(client: Anthropic, model: str, post_text: str) -> SpellcheckResult:
    response = client.messages.create(
        model=model,
        system=SPELLCHECK_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": post_text,
            }
        ],
        max_tokens=2200,
        temperature=0,
    )

    parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))

    parsed = safe_json_extract("\n".join(parts).strip())
    changed = bool(parsed.get("changed", False))
    corrected_post = str(parsed.get("corrected_post", "") or "").strip() or post_text
    notes_raw = parsed.get("notes", [])
    notes = [str(x).strip() for x in notes_raw] if isinstance(notes_raw, list) else []
    return SpellcheckResult(changed=changed, corrected_post=corrected_post, notes=notes[:3])


def normalize_enum(value: str, allowed: set[str]) -> str:
    value = (value or "").strip()
    return value if value in allowed else ALLOWED_UNKNOWN


def validate_publish_draft(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    normalized["imkoniyat_turi"] = normalize_enum(str(data.get("imkoniyat_turi", "")), IMKONIYAT_TURI_OPTIONS)
    normalized["daraja"] = normalize_enum(str(data.get("daraja", "")), DARAJA_OPTIONS)
    normalized["moliyalashtirish"] = normalize_enum(str(data.get("moliyalashtirish", "")), MOLIYALASHTIRISH_OPTIONS)
    normalized["format"] = normalize_enum(str(data.get("format", "")), FORMAT_OPTIONS)
    normalized["davomiylik"] = normalize_enum(str(data.get("davomiylik", "")), DAVOMIYLIK_OPTIONS)
    normalized["ariza_tolovi"] = normalize_enum(str(data.get("ariza_tolovi", "")), ARIZA_TOLOVI_OPTIONS)
    normalized["deadline_type"] = str(data.get("deadline_type", "Regular") or "Regular")
    normalized["sources"] = [str(x) for x in data.get("sources", [])][:12] if isinstance(data.get("sources"), list) else []
    normalized["confidence"] = str(data.get("confidence", "low") or "low")
    return normalized


def build_publish_preview(draft: dict[str, Any]) -> str:
    return (
        "Draft tayyor:\n\n"
        f"Title: {draft.get('title', '')}\n"
        f"Country: {draft.get('country', '')}\n"
        f"Imkoniyat turi: {draft.get('imkoniyat_turi', '')}\n"
        f"Daraja: {draft.get('daraja', '')}\n"
        f"Moliyalashtirish: {draft.get('moliyalashtirish', '')}\n"
        f"Format: {draft.get('format', '')}\n"
        f"Davomiylik: {draft.get('davomiylik', '')}\n"
        f"Ariza to'lovi: {draft.get('ariza_tolovi', '')}\n"
        f"Official link: {draft.get('official_link', '')}\n"
        f"Registration link: {draft.get('registration_link', '')}\n"
        f"Deadline: {draft.get('deadline', '')}\n"
        f"Opening date: {draft.get('opening_date', '')}\n"
        f"Confidence: {draft.get('confidence', '')}\n\n"
        f"Description:\n{draft.get('description', '')}\n\n"
        f"Eligibility:\n{draft.get('eligibility', '')}\n\n"
        f"Benefits:\n{draft.get('benefits', '')}\n\n"
        f"Application Process:\n{draft.get('application_process', '')}\n\n"
        f"Additional Information:\n{draft.get('additional_information', '')}\n\n"
        "Tasdiqlash: /approve\nBekor qilish: /cancelpublish"
    )


def generate_publish_draft(
    client: Anthropic,
    model: str,
    program_name: str,
    input_links: list[str],
    max_sources: int = 8,
) -> PublishDraftResult:
    source_candidates = list(dict.fromkeys(input_links + search_program_sources(program_name, max_results=max_sources)))
    source_candidates = source_candidates[:max_sources]

    collected_chunks: list[str] = []
    used_sources: list[str] = []
    for url in source_candidates:
        try:
            text = fetch_url_content(url, max_chars=4500)
        except Exception:
            continue
        if not text:
            continue
        used_sources.append(url)
        collected_chunks.append(f"[SOURCE] {url}\n{text}")
        if len(collected_chunks) >= max_sources:
            break

    if not collected_chunks:
        raise ValueError("Hech bir manbadan matn olib bo'lmadi")

    user_payload = (
        f"Program name: {program_name}\n"
        f"Seed links: {', '.join(input_links) if input_links else 'none'}\n\n"
        "Source texts:\n"
        + "\n\n".join(collected_chunks)
    )

    response = client.messages.create(
        model=model,
        system=CMS_DRAFT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_payload}],
        max_tokens=3500,
        temperature=0,
    )
    parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
    parsed = safe_json_extract("\n".join(parts).strip())
    parsed["sources"] = used_sources
    validated = validate_publish_draft(parsed)
    return PublishDraftResult(data=validated, used_sources=used_sources)


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


def is_reply_spellcheck_request(update: Update, bot_username: str | None) -> bool:
    if not update.message or not update.message.reply_to_message:
        return False
    text = (update.message.text or "").strip().lower()
    if not text:
        return False

    bot_tag = f"@{bot_username.lower()}" if bot_username else ""
    if text.startswith("/spell"):
        return True
    return bool(bot_tag and bot_tag in text)


def is_reply_bot_trigger(update: Update, bot_username: str | None) -> bool:
    if not update.message or not update.message.reply_to_message:
        return False
    text = (update.message.text or "").strip().lower()
    if not text:
        return False
    if text.startswith("/spell") or text.startswith("/grant"):
        return True
    bot_tag = f"@{bot_username.lower()}" if bot_username else ""
    return bool(bot_tag and bot_tag in text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Opportunity matnini yuboring. Bot uni tekshiradi va natijani private guruhga jo'natadi.\n\n"
        "Format erkin: URL + tavsif matni bo'lsa yetarli.\n"
        "Agar SOURCE_URLS berilgan bo'lsa, /scan komandasi bilan bot manbalarni o'zi skan qiladi.\n"
        "Website draft uchun: /publish"
    )


async def publish_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    context.user_data[PUBLISH_STATE_KEY] = {"step": "awaiting_name"}
    await update.message.reply_text(
        "Publishing wizard boshlandi.\n1/2 Program nomini yuboring:"
    )


async def cancel_publish_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if PUBLISH_STATE_KEY in context.user_data:
        context.user_data.pop(PUBLISH_STATE_KEY, None)
    if not update.message:
        return
    await update.message.reply_text("Publish wizard bekor qilindi.")


async def approve_publish_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    state = context.user_data.get(PUBLISH_STATE_KEY, {})
    draft = state.get("draft") if isinstance(state, dict) else None
    if not draft:
        await update.message.reply_text("Tasdiqlash uchun draft topilmadi. /publish dan boshlang.")
        return
    await update.message.reply_text(
        "Draft tasdiqlandi. Endi buni website publish API ga ulash qolgan (hozircha manual copy/paste rejim)."
    )
    context.user_data.pop(PUBLISH_STATE_KEY, None)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    scan_lock: asyncio.Lock = context.application.bot_data["scan_lock"]
    source_urls: list[str] = context.application.bot_data.get("source_urls", [])
    auto_scan_interval_minutes = int(context.application.bot_data.get("auto_scan_interval_minutes", 0))

    scan_state = "busy (scan ketmoqda)" if scan_lock.locked() else "idle"
    auto_scan_state = (
        f"o'chirilgan (oldingi sozlama: {auto_scan_interval_minutes} daqiqa)"
        if auto_scan_interval_minutes > 0
        else "o'chirilgan"
    )

    await update.message.reply_text(
        "Bot holati:\n"
        f"- Scan: {scan_state}\n"
        f"- Source soni: {len(source_urls)}\n"
        f"- Auto scan: {auto_scan_state}"
    )


async def duty_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    app = context.application
    tz: ZoneInfo = app.bot_data["tz"]
    monthly_schedule: dict[int, list[str]] = app.bot_data.get("monthly_schedule", {})
    member_usernames: dict[str, str] = app.bot_data.get("member_usernames", {})
    now = datetime.now(tz)
    duty_names = monthly_schedule.get(now.day, [])

    if not duty_names:
        await update.message.reply_text("Bugungi sana uchun navbatchi schedule topilmadi.")
        return

    msg = build_duty_message(
        duty_date=now,
        duty_names=duty_names,
        username_map=member_usernames,
    )
    await update.message.reply_text(msg)


async def daily_reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    tz: ZoneInfo = app.bot_data["tz"]
    target_chat_id: str | None = app.bot_data.get("target_chat_id")
    monthly_schedule: dict[int, list[str]] = app.bot_data.get("monthly_schedule", {})
    member_usernames: dict[str, str] = app.bot_data.get("member_usernames", {})

    if not target_chat_id:
        logger.warning("Daily reminder skip: TARGET_CHAT_ID yo'q")
        return

    now = datetime.now(tz)
    duty_names = monthly_schedule.get(now.day, [])
    if not duty_names:
        logger.info("Daily reminder: schedule yo'q (%s)", now.day)
        return

    text = build_duty_message(
        duty_date=now,
        duty_names=duty_names,
        username_map=member_usernames,
    )
    await context.bot.send_message(chat_id=target_chat_id, text=text)


async def run_source_scan(context: ContextTypes.DEFAULT_TYPE, notify_chat_id: int | None = None) -> ScanStats:
    app = context.application
    client: Anthropic = app.bot_data["llm_client"]
    model: str = app.bot_data["llm_model"]
    tz: ZoneInfo = app.bot_data["tz"]
    source_urls: list[str] = app.bot_data.get("source_urls", [])
    target_chat_id: str | None = app.bot_data.get("target_chat_id")
    destination_chat_id = str(notify_chat_id) if notify_chat_id is not None else target_chat_id
    llm_timeout_seconds: int = app.bot_data.get("llm_timeout_seconds", 60)
    max_candidates_per_source: int = app.bot_data.get("max_candidates_per_source", 8)
    scan_max_evaluations: int = app.bot_data.get("scan_max_evaluations", 20)
    scan_lock: asyncio.Lock = app.bot_data["scan_lock"]

    if scan_lock.locked():
        if destination_chat_id:
            await context.bot.send_message(
                chat_id=destination_chat_id,
                text="Scan allaqachon ishlayapti. Tugashini kuting.",
            )
        return ScanStats(sources_total=len(source_urls))

    await scan_lock.acquire()
    try:
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
        evaluated_count = 0

        for source_url in source_urls:
            try:
                listing_resp = await asyncio.to_thread(
                    requests.get,
                    source_url,
                    timeout=20,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                listing_resp.raise_for_status()
                candidates = extract_candidate_links(
                    source_url,
                    listing_resp.text,
                    max_links=max_candidates_per_source,
                )
            except Exception as exc:
                logger.warning("Source o'qilmadi: %s | %s", source_url, exc)
                continue

            stats.candidates_total += len(candidates)

            for candidate_url in candidates:
                if evaluated_count >= scan_max_evaluations:
                    logger.info("Scan limit reached: %s", scan_max_evaluations)
                    save_seen_links(seen_links)
                    return stats

                if candidate_url in seen_links:
                    continue

                seen_links.add(candidate_url)
                stats.checked_total += 1
                evaluated_count += 1

                try:
                    raw_text = await asyncio.wait_for(
                        asyncio.to_thread(fetch_url_content, candidate_url),
                        timeout=30,
                    )
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            evaluate_opportunity,
                            client,
                            model,
                            today,
                            candidate_url,
                            raw_text,
                        ),
                        timeout=llm_timeout_seconds,
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
    finally:
        if scan_lock.locked():
            scan_lock.release()


async def scan_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "/scan o'chirilgan (token tejash uchun). Endi link xabarga reply + bot tag qilsangiz ishlaydi."
        )


async def auto_scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Auto-scan o'chirilgan")


async def analyze_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    chat_type = update.effective_chat.type if update.effective_chat else ""
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    app = context.application
    client: Anthropic = app.bot_data["llm_client"]
    model: str = app.bot_data["llm_model"]
    tz: ZoneInfo = app.bot_data["tz"]
    target_chat_id: str | None = app.bot_data.get("target_chat_id")
    llm_timeout_seconds: int = app.bot_data.get("llm_timeout_seconds", 60)
    bot_username = context.bot.username
    allowed_group_id: str | None = app.bot_data.get("allowed_group_id")

    # Restrict bot activity to a single configured group; private chat stays enabled.
    if chat_type != "private" and allowed_group_id and chat_id != allowed_group_id:
        return

    publish_state = context.user_data.get(PUBLISH_STATE_KEY)
    if publish_state:
        if chat_type != "private":
            await update.message.reply_text("Publish wizard faqat private chatda ishlaydi.")
            return

        step = str(publish_state.get("step", ""))
        incoming = update.message.text.strip()

        if step == "awaiting_name":
            context.user_data[PUBLISH_STATE_KEY] = {
                "step": "awaiting_links",
                "program_name": incoming,
            }
            await update.message.reply_text(
                "2/2 Mavjud linklarni yuboring (bir nechta bo'lsa bo'sh joy yoki yangi qator bilan).\n"
                "Agar link bo'lmasa: none"
            )
            return

        if step == "awaiting_links":
            program_name = str(publish_state.get("program_name", "")).strip()
            if not program_name:
                context.user_data.pop(PUBLISH_STATE_KEY, None)
                await update.message.reply_text("Program nomi yo'qolib qoldi. /publish ni qayta boshlang.")
                return

            input_links = [] if incoming.lower() == "none" else extract_all_urls(incoming)
            await update.message.reply_text("Ko'p manbadan tekshirib draft tayyorlayapman...")
            try:
                draft_result = await asyncio.wait_for(
                    asyncio.to_thread(
                        generate_publish_draft,
                        client,
                        model,
                        program_name,
                        input_links,
                    ),
                    timeout=max(90, llm_timeout_seconds),
                )
            except Exception as exc:
                context.user_data.pop(PUBLISH_STATE_KEY, None)
                await update.message.reply_text(f"Draft yaratishda xatolik: {exc}\nQayta urinib ko'ring: /publish")
                return

            context.user_data[PUBLISH_STATE_KEY] = {
                "step": "ready",
                "program_name": program_name,
                "draft": draft_result.data,
            }
            await update.message.reply_text(build_publish_preview(draft_result.data), disable_web_page_preview=True)
            await update.message.reply_text(
                "Manbalar:\n" + "\n".join(draft_result.used_sources[:12]),
                disable_web_page_preview=True,
            )
            return

        if step == "ready":
            await update.message.reply_text("Draft tayyor. Tasdiqlash: /approve yoki bekor qilish: /cancelpublish")
            return

    if chat_type != "private":
        if not is_reply_bot_trigger(update, bot_username):
            return

        replied = update.message.reply_to_message
        original_text = (replied.text or replied.caption or "").strip() if replied else ""
        if not original_text:
            await update.message.reply_text("Iltimos, matnli xabarga reply qilib tekshiring.")
            return

        replied_url = extract_first_url(original_text)
        if replied_url != "Noma'lum":
            await update.message.reply_text("Reply qilingan link uchun grant post tayyorlanmoqda...")
            today = datetime.now(tz).date().isoformat()
            raw_text_for_ai = original_text
            try:
                fetched_text = await asyncio.wait_for(
                    asyncio.to_thread(fetch_url_content, replied_url),
                    timeout=30,
                )
                if fetched_text:
                    raw_text_for_ai = f"{original_text}\n\nQuyida URLdan olingan matn:\n{fetched_text}"
            except Exception as exc:
                logger.warning("Reply link fetch xatoligi: %s", exc)

            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        evaluate_opportunity,
                        client,
                        model,
                        today,
                        replied_url,
                        raw_text_for_ai,
                    ),
                    timeout=llm_timeout_seconds,
                )
            except Exception as exc:
                await update.message.reply_text(f"Grant post yaratishda xatolik: {exc}")
                return

            if result.eligible:
                await update.message.reply_text(
                    "Tayyor post:\n\n" + result.post,
                    disable_web_page_preview=True,
                )
            else:
                await update.message.reply_text(
                    f"Bu imkoniyat mos emas.\nSabab: {result.reason}"
                )
            return

        await update.message.reply_text("Imlo tekshiruvi boshlandi...")
        try:
            proof = await asyncio.wait_for(
                asyncio.to_thread(
                    spellcheck_post,
                    client,
                    model,
                    original_text,
                ),
                timeout=llm_timeout_seconds,
            )
        except Exception as exc:
            logger.warning("Reply spellcheck xatoligi: %s", exc)
            await update.message.reply_text(f"Imlo tekshiruvda xatolik: {exc}")
            return

        await update.message.reply_text(
            "Natija: "
            + ("xatolar tuzatildi" if proof.changed else "xato topilmadi")
            + (f"\nIzoh: {'; '.join(proof.notes)}" if proof.notes else "")
            + f"\n\n{proof.corrected_post}"
        )
        return

    text = update.message.text.strip()
    if not text:
        return

    await update.message.reply_text("Tahlil boshlandi...")

    source_url = extract_first_url(text)
    today = datetime.now(tz).date().isoformat()
    raw_text_for_ai = text

    if source_url != "Noma'lum":
        try:
            fetched_text = await asyncio.wait_for(
                asyncio.to_thread(fetch_url_content, source_url),
                timeout=30,
            )
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
        result = await asyncio.wait_for(
            asyncio.to_thread(
                evaluate_opportunity,
                client,
                model,
                today,
                source_url,
                raw_text_for_ai,
            ),
            timeout=llm_timeout_seconds,
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
    allowed_group_id = os.getenv("ALLOWED_GROUP_ID")
    source_urls = parse_source_urls(os.getenv("SOURCE_URLS"))
    auto_scan_interval_minutes = int(os.getenv("AUTO_SCAN_INTERVAL_MINUTES", "0"))
    llm_timeout_seconds = int(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
    max_candidates_per_source = int(os.getenv("MAX_CANDIDATES_PER_SOURCE", "8"))
    scan_max_evaluations = int(os.getenv("SCAN_MAX_EVALUATIONS", "20"))
    monthly_schedule = parse_monthly_schedule(os.getenv("MONTHLY_SCHEDULE"))
    member_usernames = parse_member_usernames(os.getenv("MEMBER_USERNAMES"))
    daily_reminder_hour = int(os.getenv("DAILY_REMINDER_HOUR", "9"))
    daily_reminder_minute = int(os.getenv("DAILY_REMINDER_MINUTE", "0"))

    tz = ZoneInfo(timezone_name)
    client = Anthropic(api_key=claude_api_key)

    application = Application.builder().token(telegram_bot_token).build()

    application.bot_data["llm_client"] = client
    application.bot_data["llm_model"] = claude_model
    application.bot_data["tz"] = tz
    application.bot_data["target_chat_id"] = target_chat_id
    application.bot_data["allowed_group_id"] = allowed_group_id
    application.bot_data["source_urls"] = source_urls
    application.bot_data["llm_timeout_seconds"] = llm_timeout_seconds
    application.bot_data["max_candidates_per_source"] = max_candidates_per_source
    application.bot_data["scan_max_evaluations"] = scan_max_evaluations
    application.bot_data["scan_lock"] = asyncio.Lock()
    application.bot_data["auto_scan_interval_minutes"] = auto_scan_interval_minutes
    application.bot_data["monthly_schedule"] = monthly_schedule
    application.bot_data["member_usernames"] = member_usernames
    application.bot_data["daily_reminder_hour"] = daily_reminder_hour
    application.bot_data["daily_reminder_minute"] = daily_reminder_minute

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("publish", publish_command))
    application.add_handler(CommandHandler("approve", approve_publish_command))
    application.add_handler(CommandHandler("cancelpublish", cancel_publish_command))
    application.add_handler(CommandHandler("scan", scan_sources))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("duty", duty_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analyze_message))

    if auto_scan_interval_minutes > 0:
        logger.info("AUTO_SCAN_INTERVAL_MINUTES=%s lekin auto-scan ataylab o'chirilgan", auto_scan_interval_minutes)

    if monthly_schedule:
        if application.job_queue is None:
            logger.warning("JobQueue yo'q. Daily reminder o'chirildi.")
        else:
            application.job_queue.run_daily(
                daily_reminder_job,
                time=dt_time(hour=daily_reminder_hour, minute=daily_reminder_minute, tzinfo=tz),
                name="daily-duty-reminder",
            )

    logger.info(
        "Bot ishga tushdi | source_urls=%s | auto_scan_interval_minutes=%s | schedule_days=%s",
        len(source_urls),
        auto_scan_interval_minutes,
        len(monthly_schedule),
    )
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
