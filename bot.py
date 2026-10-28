#!/usr/bin/env python3
"""
AI Digest Telegram Bot — polling mode, runs on VPS.

Three separate digests, each on its own schedule (Europe/Moscow):
  Mon 10:00 — Telegram channels   (/tg)
  Wed 10:00 — arXiv papers        (/papers)
  Fri 10:00 — blogs of AI authors (/blogs)

A digest always comes to the owner as a preview; it goes to the channel only
via the button under it or /publish. Nothing is published automatically.

Commands:
  /tg [N], /papers [N], /blogs [N] — one section for the last N days
  /digest [N]                      — all three sections
  /arxiv <query>                   — search arXiv
  /publish [section] [1 3 5] [force]
  /status, /help

Personalisation: system_prompt.txt (profile), keywords.txt (paper prefilter).
"""

import os, sys, json, re, time, html, hashlib, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# All paths are relative to this file, so the bot works from any cwd (incl. systemd).
BASE_DIR = Path(__file__).resolve().parent


def _load_env():
    """Load BASE_DIR/.env. Fail loudly instead of silently skipping it."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return  # vars may come from the environment itself
    try:
        from dotenv import load_dotenv
    except ImportError:
        sys.exit(f"ERROR: found {env_path}, but python-dotenv is not installed "
                 f"for this interpreter ({sys.executable}).\n  Fix: pip install -r requirements.txt")
    load_dotenv(env_path)


_load_env()
_missing = [k for k in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
            if not os.environ.get(k)]
if _missing:
    sys.exit(f"ERROR: missing env vars: {', '.join(_missing)}\n"
             f"  Fill them in {BASE_DIR / '.env'} (see .env.example)")

# imported after .env is loaded: they read model names from the environment
import digest  # noqa: E402

TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID    = str(os.environ["TELEGRAM_CHAT_ID"])
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()   # @channel or -100…

# ── Sections and schedule ─────────────────────────────────────────────────────

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Moscow")
except Exception:                                   # no tzdata on the system
    TZ = timezone(timedelta(hours=3))

SCHEDULE_HOUR = 10
CATCH_UP_UNTIL = 13      # if the bot was down at 10:00, run when back, but only until 13:00
SECTIONS = {
    #  key        title                  default window  weekday (Mon=0)
    "tg":     {"title": "📡 Telegram-каналы", "days": 7,  "weekday": 0},
    "papers": {"title": "📚 Статьи arXiv",    "days": 7,  "weekday": 2},
    "blogs":  {"title": "✍️ Блоги авторов",   "days": 30, "weekday": 4},
}
ALIASES = {"tg": "tg", "telegram": "tg", "каналы": "tg",
           "papers": "papers", "arxiv": "papers", "статьи": "papers",
           "blogs": "blogs", "блоги": "blogs", "search": "search", "поиск": "search",
           "pool": "pool", "подборка": "pool"}
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MAX_DAYS = 60

# ── State: last runs, seen ids, last digest per section ──────────────────────

STATE_FILE = BASE_DIR / "state.json"


def load_state() -> dict:
    s = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    for key in ("last_run", "seen", "digests", "scheduled"):
        s.setdefault(key, {})
    for key in ("pool", "published"):           # pool: items picked for the next post
        s.setdefault(key, [])
    return s


def save_state(s: dict):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False))
    tmp.replace(STATE_FILE)                         # atomic: no half-written state on crash

# ── Telegram API ──────────────────────────────────────────────────────────────

def tg_api(method: str, **kwargs) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
        data=json.dumps(kwargs).encode(), headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def tg_send(chat_id: str, text: str, html_mode: bool = True, reply_markup: dict = None):
    """Short message. html_mode=True expects already-escaped Telegram HTML."""
    kwargs = {"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": True}
    if html_mode:
        kwargs["parse_mode"] = "HTML"
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    tg_api("sendMessage", **kwargs)
    time.sleep(0.3)


def pack_messages(header: str, blocks: list[str], limit: int = 3800) -> list[str]:
    """Pack blocks into messages without ever cutting a block (and its HTML) in half."""
    messages, cur = [], header
    for b in blocks:
        piece = ("\n\n\n" if cur else "") + b      # header may be empty (pool without intro)
        if len(cur) + len(piece) > limit:
            messages.append(cur)
            cur = b
        else:
            cur += piece
    messages.append(cur)
    return messages


def tg_send_blocks(chat_id: str, header: str, blocks: list[str], reply_markup: dict = None):
    messages = pack_messages(header, blocks)
    for i, m in enumerate(messages):
        tg_send(chat_id, m, reply_markup=reply_markup if i == len(messages) - 1 else None)


def tg_get_updates(offset=0) -> list:
    try:
        r = tg_api("getUpdates", offset=offset, timeout=30,
                   allowed_updates=["message", "callback_query"])
        return r.get("result", [])
    except Exception:
        return []

# ── Running a section ─────────────────────────────────────────────────────────

def item_key(block: str) -> str:
    """An item is identified by its own link (the first href in the block)."""
    m = re.search(r'href="([^"]+)"', block)
    return m.group(1) if m else block[:200]


def item_title(block: str) -> str:
    m = re.search(r"<a [^>]*>(.*?)</a>", block)
    return m.group(1) if m else "без названия"


def short_hash(key: str) -> str:
    return hashlib.md5(key.encode()).hexdigest()[:10]


def digest_keyboard(state: dict, section: str) -> dict:
    """Under a preview: one toggle per item (add to / remove from the pool), and the whole digest."""
    d = state["digests"][section]
    in_pool = {p["key"] for p in state["pool"]}
    published = set(state["published"])
    buttons = []
    for i, b in enumerate(d["blocks"], 1):
        k = item_key(b)
        label = f"📢 {i}" if k in published else (f"✓ {i}" if k in in_pool else f"＋ {i}")
        buttons.append({"text": label, "callback_data": f"t:{section}:{d['id']}:{i}"})
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    if TELEGRAM_CHANNEL_ID:
        rows.append([{"text": "📢 Весь дайджест в канал", "callback_data": f"publish:{section}:{d['id']}"}])
    return {"inline_keyboard": rows}


def deliver(state: dict, section: str, header: str, blocks: list[str]):
    """Preview to the owner + remember it for the pool and /publish."""
    state["digests"][section] = {"id": int(time.time()), "header": header, "blocks": blocks}
    state["last_section"] = section
    save_state(state)
    tg_send_blocks(TELEGRAM_CHAT_ID, header, blocks, reply_markup=digest_keyboard(state, section))


def run_section(section: str, days: int = None, scheduled: bool = False):
    state = load_state()
    cfg = SECTIONS[section]
    now = datetime.now(timezone.utc)
    last = state["last_run"].get(section)

    if days:
        since = now - timedelta(days=days)
    elif scheduled and last:
        since = max(datetime.fromisoformat(last), now - timedelta(days=MAX_DAYS))
    else:
        since = now - timedelta(days=cfg["days"])

    # Scheduled runs show only what hasn't been sent yet. Manual runs show the whole
    # period (a summary "for N days"), but still record it, so the schedule won't repeat it.
    seen = set(state["seen"].get(section, [])) if scheduled else set()
    progress = lambda t: tg_send(TELEGRAM_CHAT_ID, t)
    if section == "tg":
        result = digest.tg_digest(since, seen, progress)
    elif section == "papers":
        result = digest.papers_digest(since, now, seen, progress)
    else:
        result = digest.blogs_digest(since, seen, progress)

    state = load_state()                          # re-read: the run took minutes
    old = state["seen"].get(section, [])
    state["seen"][section] = (old + sorted(result["seen"] - set(old)))[-5000:]
    if scheduled:
        state["last_run"][section] = now.isoformat()
    save_state(state)

    if not result["blocks"]:
        if not scheduled:                         # scheduled: stay silent when there is nothing new
            tg_send(TELEGRAM_CHAT_ID, f"📭 {cfg['title']}: новых материалов нет.")
        return
    period = f"{since.astimezone(TZ):%d.%m} – {now.astimezone(TZ):%d.%m}"
    deliver(state, section, f"<b>{cfg['title']}</b> · {period}", result["blocks"])

# ── Publishing ────────────────────────────────────────────────────────────────

MAX_INTRO = 3000
_TAGS = {"bold": ("<b>", "</b>"), "italic": ("<i>", "</i>"), "underline": ("<u>", "</u>"),
         "strikethrough": ("<s>", "</s>"), "spoiler": ("<tg-spoiler>", "</tg-spoiler>"),
         "code": ("<code>", "</code>"), "pre": ("<pre>", "</pre>"),
         "blockquote": ("<blockquote>", "</blockquote>"),
         "expandable_blockquote": ("<blockquote expandable>", "</blockquote>")}


def entities_to_html(text: str, entities: list, start: int) -> str:
    """text[start:] -> Telegram HTML, keeping the formatting you typed (bold, links, quotes…).

    Telegram gives entity offsets in UTF-16 code units, so we walk the text in those units.
    """
    def u16(s: str) -> int:
        return len(s.encode("utf-16-le")) // 2
    base = u16(text[:start])
    opens, closes = {}, {}
    for e in entities or []:
        a, b = e["offset"] - base, e["offset"] + e["length"] - base
        if a < 0:
            continue                                   # entity in the command part
        if e["type"] == "text_link":
            tag = (f'<a href="{html.escape(e["url"], quote=True)}">', "</a>")
        elif e["type"] in _TAGS:
            tag = _TAGS[e["type"]]
        else:
            continue                                   # mentions, hashtags, plain urls: as text
        opens.setdefault(a, []).append((b, tag[0]))
        closes.setdefault(b, []).append((a, tag[1]))
    out, pos = [], 0
    for ch in text[start:] + "\0":
        for _, t in sorted(closes.get(pos, []), key=lambda x: -x[0]):   # inner closes first
            out.append(t)
        for _, t in sorted(opens.get(pos, []), key=lambda x: -x[0]):    # outer opens first
            out.append(t)
        if ch != "\0":
            out.append(html.escape(ch))
        pos += u16(ch)
    return "".join(out).strip()


def parse_publish(text: str, entities: list) -> tuple[list[str], str]:
    """'/publish papers 1 3 Текст…' -> (['papers', '1', '3'], intro_html).

    Section, item numbers and 'force' come first; everything after them (the rest of the
    line and all following lines) is an optional intro posted before the digest.
    """
    first_line_end = text.find("\n") if "\n" in text else len(text)
    args, intro_at = [], None
    for i, m in enumerate(re.finditer(r"\S+", text[:first_line_end])):
        tok = m.group(0)
        if i == 0:
            continue                                   # /publish itself
        if tok.lower() in ALIASES or tok.isdigit() or tok.lower() == "force":
            args.append(tok)
        else:
            intro_at = m.start()
            break
    if intro_at is None:
        intro_at = first_line_end
    while intro_at < len(text) and text[intro_at].isspace():
        intro_at += 1
    intro = entities_to_html(text, entities, intro_at) if intro_at < len(text) else ""
    return args, intro


SECTION_ORDER = ["tg", "papers", "blogs", "search"]
SECTION_TITLES = {**{k: v["title"] for k, v in SECTIONS.items()}, "search": "🔍 Поиск по arXiv"}


def _renumber(block: str, i: int) -> str:
    return re.sub(r"^<b>\d+\. ", f"<b>{i}. ", block, count=1)


def publish_to_channel(state: dict, header: str, blocks: list[str]):
    """Post to the channel, mark the items as published and drop them from the pool."""
    tg_send_blocks(TELEGRAM_CHANNEL_ID, header, blocks)
    keys = {item_key(b) for b in blocks}
    state["published"] = (state["published"] + sorted(keys - set(state["published"])))[-3000:]
    state["pool"] = [p for p in state["pool"] if p["key"] not in keys]
    save_state(state)


def _check_channel() -> bool:
    if not TELEGRAM_CHANNEL_ID:
        tg_send(TELEGRAM_CHAT_ID, "Канал не настроен: добавьте <code>TELEGRAM_CHANNEL_ID</code> в .env")
    return bool(TELEGRAM_CHANNEL_ID)


def cmd_publish(args: list[str], intro: str = ""):
    """/publish [section] [numbers] [force] [intro] — one section; /publish pool — the pool."""
    if not _check_channel():
        return
    if len(intro) > MAX_INTRO:
        tg_send(TELEGRAM_CHAT_ID, f"Вступление слишком длинное: {len(intro)} символов, максимум {MAX_INTRO}.")
        return
    named = [ALIASES[a.lower()] for a in args if a.lower() in ALIASES]
    if "pool" in named:
        publish_pool(intro)
        return

    state = load_state()
    section = (named[0] if named else None) or state.get("last_section")
    d = state["digests"].get(section) if section else None
    if not d:
        tg_send(TELEGRAM_CHAT_ID, "Нечего публиковать: сначала /tg, /papers, /blogs или /arxiv")
        return
    nums = [int(a) for a in args if a.isdigit()] or list(range(1, len(d["blocks"]) + 1))
    bad = [x for x in nums if not 1 <= x <= len(d["blocks"])]
    if bad:
        tg_send(TELEGRAM_CHAT_ID, f"Нет пунктов {bad}. В дайджесте пунктов: {len(d['blocks'])}")
        return

    published = set(state["published"])
    force = "force" in [a.lower() for a in args]
    chosen = [(x, d["blocks"][x - 1]) for x in nums]
    skipped = [x for x, b in chosen if item_key(b) in published and not force]
    blocks = [b for x, b in chosen if x not in skipped]
    if not blocks:
        tg_send(TELEGRAM_CHAT_ID, f"Пункты {skipped} уже опубликованы. Повторно: "
                                  f"<code>/publish {section} {' '.join(map(str, nums))} force</code>")
        return
    blocks = [_renumber(b, i) for i, b in enumerate(blocks, 1)]
    header = f"{intro}\n\n{d['header']}" if intro else d["header"]   # intro on top of the post
    publish_to_channel(state, header, blocks)
    note = f" Пропущены уже опубликованные: {skipped}." if skipped else ""
    tg_send(TELEGRAM_CHAT_ID, f"✅ Опубликовано в канал: {len(blocks)} пункт(ов).{note}")


def publish_pool(intro: str = "") -> bool:
    """The pool as one post: grouped by section, numbered through, intro on top."""
    if not _check_channel():
        return False
    state = load_state()
    pool = [p for p in state["pool"] if p["key"] not in set(state["published"])]
    if not pool:
        tg_send(TELEGRAM_CHAT_ID, "Подборка пуста. Добавляйте пункты кнопками ＋ под превью.")
        return False
    blocks, i = [], 0
    for section in SECTION_ORDER:
        items = [p for p in pool if p["section"] == section]
        for j, p in enumerate(items):
            i += 1
            b = _renumber(p["block"], i)
            blocks.append(f"<b>{SECTION_TITLES[section]}</b>\n\n{b}" if j == 0 else b)
    publish_to_channel(state, intro, blocks)
    tg_send(TELEGRAM_CHAT_ID, f"✅ Подборка опубликована: {len(blocks)} пункт(ов). Подборка очищена.")
    return True


def pool_view(state: dict) -> tuple[str, dict]:
    pool = state["pool"]
    if not pool:
        return "Подборка пуста. Добавляйте пункты кнопками ＋ под превью.", {"inline_keyboard": []}
    lines, buttons, i = [f"<b>Подборка</b> · {len(pool)} пункт(ов)"], [], 0
    for section in SECTION_ORDER:
        items = [p for p in pool if p["section"] == section]
        if items:
            lines.append(f"\n<b>{SECTION_TITLES[section]}</b>")
        for p in items:
            i += 1
            lines.append(f"{i}. {item_title(p['block'])}")
            buttons.append({"text": f"✖ {i}", "callback_data": f"pr:{short_hash(p['key'])}"})
    rows = [buttons[k:k + 4] for k in range(0, len(buttons), 4)]
    rows.append([{"text": "📢 Опубликовать подборку", "callback_data": "pp"},
                 {"text": "🗑 Очистить", "callback_data": "pc"}])
    lines.append("\n✖ — убрать пункт. Со вступлением: <code>/publish pool</code> и текст с новой строки.")
    return "\n".join(lines), {"inline_keyboard": rows}


def cmd_pool():
    text, kb = pool_view(load_state())
    tg_send(TELEGRAM_CHAT_ID, text, reply_markup=kb)

# ── Commands ──────────────────────────────────────────────────────────────────

def _parse_days(arg: str | None):
    """'14' or '14d' -> 14 (max 60); None -> None; invalid -> False."""
    if arg is None:
        return None
    m = re.match(r"(\d+)d?$", arg.lower())
    if not m or int(m.group(1)) < 1:
        return False
    return min(int(m.group(1)), MAX_DAYS)


def cmd_status():
    state = load_state()
    now = datetime.now(TZ)
    lines = ["<b>Расписание</b> (время московское)"]
    for key, cfg in SECTIONS.items():
        delta = (cfg["weekday"] - now.weekday()) % 7
        nxt = (now + timedelta(days=delta)).replace(hour=SCHEDULE_HOUR, minute=0)
        if delta == 0 and (now.hour >= CATCH_UP_UNTIL
                           or state["scheduled"].get(key) == now.date().isoformat()):
            nxt += timedelta(days=7)
        last = state["last_run"].get(key)
        last_s = datetime.fromisoformat(last).astimezone(TZ).strftime("%d.%m %H:%M") if last else "—"
        lines.append(f"{cfg['title']}: следующий {WEEKDAYS[nxt.weekday()]} {nxt:%d.%m %H:%M}, "
                     f"прошлый {last_s}")
    lines.append(f"\nКанал: {html.escape(TELEGRAM_CHANNEL_ID) or 'не настроен'}")
    lines.append(f"В подборке: {len(state['pool'])} пункт(ов) — /pool")
    lines.append(f"Модели: {digest.MODEL} / {digest.DEEP_MODEL}")
    tg_send(TELEGRAM_CHAT_ID, "\n".join(lines))


def cmd_help():
    tg_send(TELEGRAM_CHAT_ID, (
        "🤖 <b>AI Digest Bot</b>\n\n"
        "По расписанию в 10:00 МСК: пн — каналы, ср — статьи, пт — блоги.\n"
        "Приходит превью, в канал — только по кнопке или /publish.\n\n"
        "/tg [дни] — Telegram-каналы (по умолчанию 7)\n"
        "/papers [дни] — статьи arXiv (7)\n"
        "/blogs [дни] — блоги авторов (30)\n"
        "/digest [дни] — все три раздела\n"
        "/arxiv &lt;запрос&gt; — поиск по arXiv\n"
        "\n<b>Публикация</b>\n"
        "＋ под превью — добавить пункт в подборку (✓ — уже в ней, 📢 — опубликован)\n"
        "/pool — подборка: убрать пункты, опубликовать одним постом\n"
        "/publish pool [текст] — подборка со вступлением\n"
        "/publish [раздел] [номера] [текст] — сразу в канал, например\n"
        "    <code>/publish papers 3</code>\n"
        "текст вступления можно с новой строки и с форматированием\n\n"
        "/status — расписание и прошлые запуски\n"
        "/help — эта справка"
    ))


def handle_message(msg: dict):
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if chat_id != TELEGRAM_CHAT_ID:
        print(f"Ignored message from {chat_id}", flush=True)
        return
    print(f"Command: {text!r}", flush=True)

    parts = text.split()
    cmd = parts[0].split("@")[0].lower() if parts else ""
    arg = parts[1] if len(parts) > 1 else None

    if cmd in ("/tg", "/papers", "/blogs", "/digest"):
        days = _parse_days(arg)
        if days is False:
            tg_send(TELEGRAM_CHAT_ID, f"Укажите число дней: <code>{cmd} 14</code> (от 1 до {MAX_DAYS})")
            return
        for section in (SECTIONS if cmd == "/digest" else [cmd[1:]]):
            run_section(section, days=days)
    elif cmd == "/arxiv":
        keyword = text[len(parts[0]):].strip()
        if not keyword:
            tg_send(TELEGRAM_CHAT_ID, "Укажите запрос: <code>/arxiv prompt injection</code>")
            return
        tg_send(TELEGRAM_CHAT_ID, f"🔍 Ищу в arXiv: <code>{html.escape(keyword)}</code>…")
        result = digest.arxiv_search_digest(keyword, lambda t: tg_send(TELEGRAM_CHAT_ID, t))
        if not result["blocks"]:
            tg_send(TELEGRAM_CHAT_ID, "Ничего не найдено.")
            return
        deliver(load_state(), "search", f"<b>🔍 arXiv: {html.escape(keyword)}</b>", result["blocks"])
    elif cmd == "/publish":
        args, intro = parse_publish(text, msg.get("entities"))
        cmd_publish(args, intro)
    elif cmd == "/pool":
        cmd_pool()
    elif cmd == "/status":
        cmd_status()
    elif cmd in ("/help", "/start"):
        cmd_help()
    else:
        tg_send(TELEGRAM_CHAT_ID, "Не знаю такой команды. Список: /help")


def _edit_markup(msg: dict, markup: dict):
    try:
        tg_api("editMessageReplyMarkup", chat_id=msg["chat"]["id"],
               message_id=msg["message_id"], reply_markup=markup)
    except Exception:
        pass                                           # "message is not modified" etc.


def _edit_text(msg: dict, text: str, markup: dict):
    try:
        tg_api("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
               text=text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup)
    except Exception:
        pass


def handle_callback(cq: dict):
    """Buttons under previews and under /pool."""
    def answer(text: str = ""):
        tg_api("answerCallbackQuery", callback_query_id=cq["id"], text=text)

    if str(cq.get("from", {}).get("id", "")) != TELEGRAM_CHAT_ID:
        answer()
        return
    data, msg = cq.get("data") or "", cq.get("message", {})
    state = load_state()

    if data.startswith("t:") or data.startswith("publish:"):
        parts = data.split(":")
        section, digest_id = parts[1], int(parts[2]) if len(parts) > 2 else None
        d = state["digests"].get(section)
        if not d or (digest_id and d.get("id") != digest_id):
            answer("Это превью устарело: раздел уже перезапускали")
            return
        if data.startswith("publish:"):
            answer("Публикую…")
            cmd_publish([section])
            _edit_markup(msg, digest_keyboard(load_state(), section))
            return
        block = d["blocks"][int(parts[3]) - 1]
        key = item_key(block)
        if key in state["published"]:
            answer("Этот пункт уже опубликован")
            return
        if any(p["key"] == key for p in state["pool"]):
            state["pool"] = [p for p in state["pool"] if p["key"] != key]
            text = f"Убрано из подборки ({len(state['pool'])})"
        else:
            state["pool"].append({"key": key, "section": section, "block": block})
            text = f"Добавлено в подборку ({len(state['pool'])})"
        save_state(state)
        answer(text)
        _edit_markup(msg, digest_keyboard(state, section))

    elif data.startswith("pr:"):                       # remove one item from the pool
        h = data[3:]
        state["pool"] = [p for p in state["pool"] if short_hash(p["key"]) != h]
        save_state(state)
        answer("Убрано")
        _edit_text(msg, *pool_view(state))
    elif data == "pc":
        state["pool"] = []
        save_state(state)
        answer("Подборка очищена")
        _edit_text(msg, *pool_view(state))
    elif data == "pp":
        answer("Публикую…")
        if publish_pool():
            _edit_text(msg, "✅ Подборка опубликована.", {"inline_keyboard": []})
    else:
        answer()


def handle(update: dict):
    if "callback_query" in update:
        handle_callback(update["callback_query"])
    elif "message" in update:
        handle_message(update["message"])

# ── Scheduler ─────────────────────────────────────────────────────────────────

def due_sections(now_local: datetime, state: dict) -> list[str]:
    """Sections whose day is today, within [SCHEDULE_HOUR, CATCH_UP_UNTIL), not yet run today.

    If the bot was down at 10:00, the run happens as soon as it is back, until 13:00.
    Deploying later in the day doesn't fire a surprise digest.
    """
    today = now_local.date().isoformat()
    return [key for key, cfg in SECTIONS.items()
            if cfg["weekday"] == now_local.weekday()
            and SCHEDULE_HOUR <= now_local.hour < CATCH_UP_UNTIL
            and state["scheduled"].get(key) != today]


def run_scheduled():
    state = load_state()
    for section in due_sections(datetime.now(TZ), state):
        state["scheduled"][section] = datetime.now(TZ).date().isoformat()
        save_state(state)              # mark first: an error must not retry every 30 seconds
        print(f"Scheduled run: {section}", flush=True)
        try:
            run_section(section, scheduled=True)
        except Exception as e:
            report_error(e, f"плановый запуск «{SECTIONS[section]['title']}»")
        state = load_state()

# ── Main loop ─────────────────────────────────────────────────────────────────

def report_error(e: Exception, where: str = ""):
    print(f"Error {where}: {type(e).__name__}: {e}", flush=True)
    try:  # tell the owner instead of failing silently
        tg_send(TELEGRAM_CHAT_ID, f"⚠️ {where + ': ' if where else ''}{type(e).__name__}: {e}",
                html_mode=False)
    except Exception:
        pass


def main():
    print(f"Bot started. Models: {digest.MODEL} / {digest.DEEP_MODEL}. "
          f"Owner: {TELEGRAM_CHAT_ID}. Channel: {TELEGRAM_CHANNEL_ID or '—'}", flush=True)
    offset = 0
    while True:
        for update in tg_get_updates(offset):     # long poll, returns within ~30 s
            offset = update["update_id"] + 1
            try:
                handle(update)
            except Exception as e:
                report_error(e)
        try:
            run_scheduled()
        except Exception as e:
            report_error(e, "планировщик")
        time.sleep(1)


if __name__ == "__main__":
    main()
