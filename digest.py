"""
digest.py — LLM calls and the three digest pipelines (papers, Telegram, blogs).

Every pipeline returns {"blocks": [Telegram-HTML per item], "seen": {ids to remember}}.
The model only returns item numbers and text fields; URLs and sources always come from
our own data and everything is HTML-escaped, so the model can't break formatting or links.
"""

import html, json, os, re, time, urllib.error, urllib.request
from datetime import datetime

from prompts import (build_titles_prompt, build_papers_prompt, build_deep_prompt,
                     build_tg_prompt, build_blogs_prompt)
from sources import (TG_CHANNELS, BLOG_FEEDS, fetch_arxiv_window, fetch_arxiv_search,
                     fetch_hf_daily, prefilter_papers, fetch_arxiv_fulltext,
                     fetch_tg_channel, fetch_feed)

MODEL      = os.environ.get("DIGEST_MODEL", "claude-haiku-5-5")    # triage and short reviews
DEEP_MODEL = os.environ.get("DEEP_MODEL", "claude-sonnet-5-5")     # full-text arXiv reviews

N_PAPERS, N_TG, N_BLOGS = 6, 6, 4        # items per digest
TITLE_STAGE_KEEP = 30                     # papers that pass the title triage

# ── LLM ──────────────────────────────────────────────────────────────────────

def llm_call(prompt: str, model: str = None, max_tokens: int = 8000) -> str:
    body = json.dumps({
        "model": model or MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    for attempt in range(4):
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            # 429 = rate limit, 529/5xx = overloaded: wait and retry
            if e.code in (429, 500, 502, 503, 529) and attempt < 3:
                wait = min(int(e.headers.get("retry-after") or 20), 90)
                print(f"LLM {e.code}, retry in {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise
    # The response may contain several blocks (e.g. "thinking" before "text").
    texts = [b["text"] for b in resp.get("content", []) if b.get("type") == "text"]
    if not texts:
        types = [b.get("type") for b in resp.get("content", [])]
        raise RuntimeError(f"No text in LLM response: stop_reason={resp.get('stop_reason')}, blocks={types}")
    return "\n".join(texts).strip()


def parse_json(raw: str, opener: str, closer: str):
    """Extract a JSON array/object from model output (tolerates code fences)."""
    start, end = raw.find(opener), raw.rfind(closer)
    if start == -1 or end <= start:
        raise ValueError("no JSON in LLM output")
    return json.loads(raw[start:end + 1])


def ru_ratio(text: str) -> float:
    """Share of Cyrillic among letters (technical terms in English are fine)."""
    cyr = len(re.findall(r"[а-яё]", text, re.I))
    lat = len(re.findall(r"[a-z]", text, re.I))
    return cyr / (cyr + lat) if cyr + lat else 1.0


def _strings(data) -> str:
    if isinstance(data, dict):
        return " ".join(_strings(v) for k, v in data.items() if k not in ("artifacts", "origin"))
    if isinstance(data, list):
        return " ".join(_strings(v) for v in data)
    return data if isinstance(data, str) else ""


def llm_json(prompt: str, opener: str, closer: str, **kw):
    """Call the model, parse JSON; if the text came out not in Russian, retry once."""
    raw = llm_call(prompt, **kw)
    data = parse_json(raw, opener, closer)
    if ru_ratio(_strings(data)) < 0.2:   # English ≈ 0; Russian with English terms ≈ 0.3–0.7
        print("LLM answered not in Russian, retrying once", flush=True)
        raw2 = llm_call(prompt + "\n\nIMPORTANT: your previous answer was in English. "
                                 "Write every text field in Russian (Cyrillic). Keep only "
                                 "established terms and names in English.", **kw)
        try:
            data2 = parse_json(raw2, opener, closer)
            if ru_ratio(_strings(data2)) > ru_ratio(_strings(data)):
                return data2, raw2
        except (ValueError, json.JSONDecodeError):
            pass
    return data, raw


def _pick(items: list, n) -> dict | None:
    try:
        return items[int(n) - 1] if int(n) >= 1 else None
    except (ValueError, TypeError, IndexError):
        return None


def _score(p: dict) -> int:
    try:
        return max(1, min(5, int(p.get("score", 0))))
    except (TypeError, ValueError):
        return 0

# ── Rendering ────────────────────────────────────────────────────────────────

URL_RE = re.compile(r"https?://[^\s,;)\"'<>]+")


def clean_links(text: str, allowed: str) -> str:
    """Drop every comma/semicolon-separated part that has a URL not present in `allowed`."""
    kept = []
    for part in re.split(r"[;,]\s*", text or ""):
        if all(u.rstrip(".") in allowed for u in URL_RE.findall(part)):
            kept.append(part.strip())
    return ", ".join(x for x in kept if x)


def render(k: int, title: str, url: str, meta_html: str, sections: list) -> str:
    """One item: title, source line, then labelled sections separated by blank lines.

    `meta_html` is already escaped; section values are plain text; empty sections are skipped.
    """
    parts = [f'<b>{k}. <a href="{html.escape(url, quote=True)}">{html.escape(title)}</a></b>\n{meta_html}']
    for label, value in sections:
        value = str(value or "").strip()
        if value:
            parts.append(f"{label} {html.escape(value)}")
    return "\n\n".join(parts)


def _empty() -> dict:
    return {"blocks": [], "seen": set()}

# ── Papers: prefilter -> Haiku by titles -> Haiku by abstracts -> Sonnet full text ──

def _paper_meta(p: dict, pick: dict) -> str:
    meta = html.escape(p["source"])
    if p.get("hf_upvotes"):
        meta += f" · HF ▲{p['hf_upvotes']}"
    if _score(pick):
        meta += f" · значимость {_score(pick)}/5"
    if pick.get("_fulltext"):
        meta += " · по полному тексту"
    return meta


def _review_papers(cands: list[dict], context: str, progress, n: int) -> list[str]:
    """Abstract-stage selection + full-text deep reviews; returns rendered blocks."""
    items_text = "\n\n".join(
        f"[{i+1}] Title: {p['title']}\nAbstract: {p['abstract']}" for i, p in enumerate(cands))
    try:
        data, raw = llm_json(build_papers_prompt(items_text, len(cands), n, context), "[", "]")
        picks = [d for d in data if isinstance(d, dict)]
    except (ValueError, json.JSONDecodeError):
        return ["Не удалось разобрать ответ модели."]

    chosen = [(pk, _pick(cands, pk.get("n"))) for pk in picks]
    chosen = [(pk, p) for pk, p in chosen if p][:n]
    chosen.sort(key=lambda x: _score(x[0]), reverse=True)

    if chosen and progress:
        progress(f"📄 Читаю полные тексты статей: {len(chosen)}…")
    for pk, p in chosen:
        arxiv_id = p["id"]
        fulltext = fetch_arxiv_fulltext(arxiv_id)
        if not fulltext:
            continue    # no HTML version: keep the abstract-based review, no PDF
        try:
            data, _ = llm_json(build_deep_prompt(p["title"], fulltext), "{", "}",
                               model=DEEP_MODEL, max_tokens=4000)
        except Exception as e:
            print(f"Deep review failed for {p['url']}: {type(e).__name__}: {e}", flush=True)
            continue
        pk.update({f: str(data.get(f) or "").strip() for f in ("ideas", "why", "artifacts")
                   if data.get(f)})
        pk["_fulltext"] = fulltext

    blocks = []
    for k, (pk, p) in enumerate(chosen, 1):
        allowed = " ".join([p["abstract"], p["url"], pk.get("_fulltext") or ""])
        blocks.append(render(k, pk.get("title") or p["title"], p["url"], _paper_meta(p, pk), [
            ("💡 <b>Ключевые идеи:</b>", pk.get("ideas")),
            ("🎯 <b>Почему это важно:</b>", pk.get("why")),
            ("📦 <b>Артефакты:</b>", clean_links(pk.get("artifacts"), allowed)),
        ]))
    return blocks


def papers_digest(since: datetime, until: datetime, seen: set, progress=None, n=N_PAPERS) -> dict:
    progress and progress("📥 Собираю статьи arXiv и HF Daily Papers…")
    papers = fetch_arxiv_window(since, until)
    hf = fetch_hf_daily(since, until)
    have = {p["id"].split("v")[0] for p in papers}
    for pid, h in hf.items():                      # HF papers outside our arXiv categories
        if pid not in have and h["title"]:
            papers.append({"id": pid, "kind": "paper", "source": "HF Daily Papers",
                           "title": h["title"], "abstract": h["abstract"],
                           "url": f"https://arxiv.org/abs/{pid}", "published": None})
    total = len(papers)
    papers = [p for p in papers if p["id"].split("v")[0] not in seen]
    cands = prefilter_papers(papers, hf)
    if not cands:
        return _empty()
    progress and progress(f"🔎 Статей за период: {total}, по вашим темам: {len(cands)}. Отбираю…")

    if len(cands) > TITLE_STAGE_KEEP:
        titles = "\n".join(
            f"[{i+1}] {p['title']}" + (f"  (HF ▲{p['hf_upvotes']})" if p.get("hf_upvotes") else "")
            for i, p in enumerate(cands))
        try:
            nums = parse_json(llm_call(build_titles_prompt(titles, len(cands), TITLE_STAGE_KEEP)), "[", "]")
            short = [p for p in (_pick(cands, x) for x in nums) if p]
        except (ValueError, json.JSONDecodeError):
            short = []
        short = list({p["id"]: p for p in short}.values())[:TITLE_STAGE_KEEP] or cands[:TITLE_STAGE_KEEP]
    else:
        short = cands

    days = max(1, round((until - since).total_seconds() / 86400))
    blocks = _review_papers(short, f"digest of arXiv papers for the last {days} days", progress, n)
    return {"blocks": blocks, "seen": {p["id"].split("v")[0] for p in cands}}


def arxiv_search_digest(keyword: str, progress=None, n=5) -> dict:
    papers = fetch_arxiv_search(keyword)
    if not papers:
        return _empty()
    return {"blocks": _review_papers(papers, f'arXiv search: "{keyword}"', progress, n), "seen": set()}

# ── Telegram channels: one item per story, duplicates across channels merged ──

def tg_digest(since: datetime, seen: set, progress=None, n=N_TG) -> dict:
    progress and progress("📥 Читаю Telegram-каналы…")
    posts = []
    for ch in TG_CHANNELS:
        posts += fetch_tg_channel(ch, since)
        time.sleep(1)
    posts = [p for p in posts if p["id"] not in seen]
    if not posts:
        return _empty()
    progress and progress(f"🔎 Постов за период: {len(posts)}. Отбираю и склеиваю дубли…")

    cap = max(250, min(800, 160_000 // len(posts)))   # keep the prompt within ~60K tokens
    posts_text = "\n\n".join(
        f"[{i+1}] {p['source']} · {p['published']:%d.%m}\n{p['text'][:cap]}"
        + (f"\nLinks: {' '.join(p['links'][:5])}" if p["links"] else "")
        for i, p in enumerate(posts))
    try:
        data, raw = llm_json(build_tg_prompt(posts_text, len(posts), n), "[", "]")
        picks = [d for d in data if isinstance(d, dict)]
    except (ValueError, json.JSONDecodeError):
        return {"blocks": ["Не удалось разобрать ответ модели."], "seen": {p["id"] for p in posts}}

    stories = []
    for pk in picks:
        nums = pk.get("n") if isinstance(pk.get("n"), list) else [pk.get("n")]
        group = list({p["id"]: p for p in (_pick(posts, x) for x in nums) if p}.values())
        if group:
            stories.append((pk, group))
    stories = sorted(stories[:n], key=lambda x: _score(x[0]), reverse=True)

    blocks = []
    for k, (pk, group) in enumerate(stories, 1):
        first_by_channel = {}
        for p in group:                               # one link per channel
            first_by_channel.setdefault(p["source"], p["url"])
        channels = " · ".join(f'<a href="{html.escape(u, quote=True)}">{html.escape(src)}</a>'
                              for src, u in first_by_channel.items())
        meta = channels + (f" · значимость {_score(pk)}/5" if _score(pk) else "")
        allowed = " ".join(" ".join(p["links"]) for p in group)
        origin = clean_links(pk.get("origin"), allowed) if allowed else ""
        blocks.append(render(k, pk.get("title") or group[0]["text"][:80], group[0]["url"], meta, [
            ("📰 <b>Что произошло:</b>", pk.get("what")),
            ("🎯 <b>Почему это важно:</b>", pk.get("why")),
            ("🔗 <b>Первоисточник:</b>", origin),
        ]))
    return {"blocks": blocks or [html.escape(raw)], "seen": {p["id"] for p in posts}}

# ── Blogs ─────────────────────────────────────────────────────────────────────

def blogs_digest(since: datetime, seen: set, progress=None, n=N_BLOGS) -> dict:
    progress and progress("📥 Читаю блоги…")
    posts = []
    for name, url in BLOG_FEEDS:
        posts += fetch_feed(name, url, since)
    posts = [p for p in posts if p["id"] not in seen]
    if not posts:
        return _empty()

    items_text = "\n\n".join(
        f"[{i+1}] Author/blog: {p['source']}\nTitle: {p['title']}\nText: {p['abstract']}"
        for i, p in enumerate(posts))
    try:
        data, raw = llm_json(build_blogs_prompt(items_text, len(posts), n), "[", "]")
        picks = [d for d in data if isinstance(d, dict)]
    except (ValueError, json.JSONDecodeError):
        return {"blocks": ["Не удалось разобрать ответ модели."], "seen": {p["id"] for p in posts}}

    chosen = [(pk, _pick(posts, pk.get("n"))) for pk in picks]
    chosen = sorted([(pk, p) for pk, p in chosen if p][:n], key=lambda x: _score(x[0]), reverse=True)
    blocks = []
    for k, (pk, p) in enumerate(chosen, 1):
        meta = html.escape(p["source"])
        if p["words"] > 300:   # full text in the feed: estimate reading time
            meta += f" · ~{max(1, round(p['words'] / 200))} мин чтения"
        if _score(pk):
            meta += f" · значимость {_score(pk)}/5"
        blocks.append(render(k, pk.get("title") or p["title"], p["url"], meta, [
            ("💡 <b>Главная мысль:</b>", pk.get("thesis")),
            ("📊 <b>Аргументы и данные:</b>", pk.get("arguments")),
        ]))
    return {"blocks": blocks or [html.escape(raw)], "seen": {p["id"] for p in posts}}
