"""
sources.py — fetching materials: arXiv, HF Daily Papers, Telegram channels, blogs.

No API keys needed: arXiv API, public HF API, t.me/s/ web preview, RSS/Atom feeds.
"""

import json, re, time, urllib.request, urllib.parse, xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ── What to follow ────────────────────────────────────────────────────────────

TG_CHANNELS = [
    # RU
    "seeallochnaya", "gonzo_ml", "abstractDL", "ai_newz", "lovedeathtransformers",
    # EN
    "interconnects", "gradientdissent", "ai_alignment_feed", "lmsys_org",
]

BLOG_FEEDS = [
    ("Interconnects",   "https://www.interconnects.ai/feed"),
    ("Ahead of AI",     "https://magazine.sebastianraschka.com/feed"),
    ("Import AI",       "https://jack-clark.net/feed/"),
    ("Lil'Log",         "https://lilianweng.github.io/index.xml"),
    ("Simon Willison",  "https://simonwillison.net/atom/entries/"),   # long posts only
    ("Eugene Yan",      "https://eugeneyan.com/rss/"),
    ("The Gradient",    "https://thegradient.pub/rss/"),
    ("Last Week in AI", "https://lastweekin.ai/feed"),
]

ARXIV_CATEGORIES = ["cs.AI", "cs.CL", "cs.LG", "cs.CR", "cs.NE", "stat.ML"]

# ── Helpers ───────────────────────────────────────────────────────────────────

def http_get(url: str, timeout=30) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "AIDigestBot/3.0 (personal research digest)",
        "Accept-Language": "ru,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def strip_html(s: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", s, flags=re.I)
    s = unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def _parse_dt(s: str):
    """ISO-8601 or RFC-822 date -> aware datetime (UTC), or None."""
    if not s:
        return None
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(s)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

# ── arXiv ─────────────────────────────────────────────────────────────────────

ATOM = {"atom": "http://www.w3.org/2005/Atom"}
ARXIV_API = "https://export.arxiv.org/api/query"


def _arxiv_entries(xml_data: bytes) -> list[dict]:
    root = ET.fromstring(xml_data)
    out = []
    for e in root.findall("atom:entry", ATOM):
        id_el = e.find("atom:id", ATOM)
        if id_el is None or "/abs/" not in (id_el.text or ""):
            continue  # API error entries have no /abs/ id
        arxiv_id = id_el.text.split("/abs/")[-1]
        title    = re.sub(r"\s+", " ", e.findtext("atom:title", "", ATOM)).strip()
        abstract = re.sub(r"\s+", " ", e.findtext("atom:summary", "", ATOM)).strip()
        out.append({
            "id": arxiv_id, "kind": "paper", "source": "arXiv",
            "title": title, "abstract": abstract[:2500],
            "url": f"https://arxiv.org/abs/{arxiv_id}",
            "published": _parse_dt(e.findtext("atom:published", "", ATOM)),
        })
    return out


def fetch_arxiv_window(since: datetime, until: datetime, max_total=4000, page=500) -> list[dict]:
    """All papers in ARXIV_CATEGORIES submitted in [since, until], newest first.

    Uses the submittedDate range query; if the API returns nothing for it,
    falls back to paging the newest papers and cutting by date.
    """
    cats = " OR ".join(f"cat:{c}" for c in ARXIV_CATEGORIES)
    fmt = "%Y%m%d%H%M"
    dated_q = f"({cats}) AND submittedDate:[{since.strftime(fmt)} TO {until.strftime(fmt)}]"

    def run(query: str, cut_by_date: bool) -> list[dict]:
        papers, start = [], 0
        while start < max_total:
            url = (f"{ARXIV_API}?search_query={urllib.parse.quote(query, safe=':')}"
                   f"&sortBy=submittedDate&sortOrder=descending&start={start}&max_results={page}")
            try:
                batch = _arxiv_entries(http_get(url, timeout=60))
            except Exception as e:
                print(f"arXiv error at start={start}: {e}", flush=True)
                break
            if cut_by_date:
                fresh = [p for p in batch if p["published"] and p["published"] >= since]
                papers += fresh
                if len(fresh) < len(batch):
                    break
            else:
                papers += batch
            if len(batch) < page:
                break
            start += page
            time.sleep(3)   # arXiv API etiquette
        return papers

    papers = run(dated_q, cut_by_date=False)
    if not papers:
        print("arXiv: date query returned nothing, falling back to newest-first paging", flush=True)
        papers = run(cats, cut_by_date=True)
    # de-duplicate (cross-listed papers can repeat between pages)
    uniq = {}
    for p in papers:
        uniq.setdefault(p["id"].split("v")[0], p)
    return list(uniq.values())


def fetch_arxiv_search(keyword: str, max_results=30) -> list[dict]:
    q = f"all:{keyword}"
    url = (f"{ARXIV_API}?search_query={urllib.parse.quote(q, safe=':')}"
           f"&sortBy=submittedDate&sortOrder=descending&max_results={max_results}")
    try:
        return _arxiv_entries(http_get(url, timeout=60))
    except Exception as e:
        print(f"arXiv search error: {e}", flush=True)
        return []

# ── HF Daily Papers (community upvotes as a signal) ───────────────────────────

def fetch_hf_daily(since: datetime, until: datetime) -> dict:
    """{arxiv_id_without_version: {"title", "abstract", "upvotes"}} for days in the window."""
    out = {}
    day = since.date()
    while day <= until.date():
        try:
            data = json.loads(http_get(f"https://huggingface.co/api/daily_papers?date={day}"))
        except Exception as e:
            print(f"HF daily papers {day}: {e}", flush=True)
            data = []
        for row in data if isinstance(data, list) else []:
            paper = row.get("paper") or {}
            pid = str(paper.get("id") or "").split("v")[0]
            if not pid:
                continue
            out[pid] = {
                "title": paper.get("title") or row.get("title") or "",
                "abstract": (paper.get("summary") or row.get("summary") or "")[:2500],
                "upvotes": int(paper.get("upvotes") or 0),
            }
        day += timedelta(days=1)
        time.sleep(0.5)
    return out

# ── Keyword prefilter (free, before any LLM) ─────────────────────────────────

DEFAULT_KEYWORDS = """
# post-training / RL
RLHF, RLAIF, DPO, GRPO, PPO, reward model, reward hacking, preference optimization,
post-training, reinforcement learning, RLVR, verifiable reward, policy optimization
# architectures / training
mixture of experts, MoE, state space model, Mamba, linear attention, long context,
transformer architecture, scaling law, pretraining, distillation, test-time compute, reasoning model
# safety / security
alignment, AI safety, jailbreak, prompt injection, red teaming, red-teaming, backdoor,
data poisoning, sandbox, agent security, guardrail, interpretability, deception, sycophancy,
unlearning, adversarial attack, tool use, LLM agent, agentic
"""


def load_keywords() -> list[str]:
    path = BASE_DIR / "keywords.txt"
    text = path.read_text() if path.exists() else DEFAULT_KEYWORDS
    words = []
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        words += [w.strip().lower() for w in line.split(",") if w.strip()]
    return words


def prefilter_papers(papers: list[dict], hf: dict, limit=400) -> list[dict]:
    """Score by keyword hits in title (x3) and abstract, plus HF upvotes. Keep top `limit`."""
    keywords = load_keywords()
    scored = []
    for p in papers:
        title, abstract = p["title"].lower(), p["abstract"].lower()
        hits = sum(3 * (k in title) + (k in abstract) for k in keywords)
        upvotes = hf.get(p["id"].split("v")[0], {}).get("upvotes", 0)
        p["hf_upvotes"] = upvotes
        score = hits + min(upvotes, 50) / 5
        if score > 0:
            scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:limit]]

# ── arXiv full text (HTML only, no PDF) ───────────────────────────────────────

MAX_FULLTEXT_CHARS = 60_000   # ≈15K tokens: the body without references


class _ArxivHTMLText(HTMLParser):
    """Plain text from arxiv.org/html: skips scripts, nav, bibliography; formulas as LaTeX."""
    VOID  = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
             "source", "track", "wbr"}
    SKIP  = {"script", "style", "nav", "header", "footer", "button", "svg"}
    SKIP_CLASSES = ("ltx_bibliography", "ltx_page_footer", "ltx_page_header", "ltx_dates")
    BLOCK = {"p", "div", "section", "li", "h1", "h2", "h3", "h4", "h5", "h6",
             "tr", "figcaption", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            if tag == "br" and not self.skip:
                self.out.append("\n")
            return
        a = dict(attrs)
        if self.skip:
            self.skip += 1
        elif tag == "math":
            self.out.append(f" ${a.get('alttext', '')}$ ")
            self.skip = 1
        elif tag in self.SKIP or any(c in (a.get("class") or "") for c in self.SKIP_CLASSES):
            self.skip = 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)

    def text(self) -> str:
        t = re.sub(r"[ \t]+", " ", "".join(self.out))
        return re.sub(r"\n\s*\n+", "\n\n", t).strip()


def fetch_arxiv_fulltext(arxiv_id: str) -> str | None:
    """Full text from the arXiv HTML version. None if there is no HTML (we don't parse PDFs)."""
    try:
        raw = http_get(f"https://arxiv.org/html/{arxiv_id}", timeout=40)
    except Exception as e:
        print(f"No HTML for {arxiv_id}: {e}", flush=True)
        return None
    parser = _ArxivHTMLText()
    parser.feed(raw.decode("utf-8", errors="ignore"))
    text = parser.text()
    if len(text) < 3000:   # error page / stub, not a paper
        return None
    return text[:MAX_FULLTEXT_CHARS]

# ── Telegram channels via t.me/s (with paging back in time) ───────────────────

_TG_MSG_SPLIT = re.compile(r'(?=<div class="tgme_widget_message_wrap)')
_TG_POST  = re.compile(r'data-post="([^"]+)"')
_TG_TEXT  = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TG_TIME  = re.compile(r'<time[^>]*datetime="([^"]+)"')
_TG_HREF  = re.compile(r'href="(https?://[^"]+)"')


def _parse_tg_page(html_text: str, channel: str) -> list[dict]:
    posts = []
    for chunk in _TG_MSG_SPLIT.split(html_text):
        m_post = _TG_POST.search(chunk)
        if not m_post:
            continue
        post_id = m_post.group(1)                        # "channel/123"
        m_text = _TG_TEXT.search(chunk)
        text_html = m_text.group(1) if m_text else ""
        links = [unescape(u) for u in _TG_HREF.findall(text_html)
                 if "t.me/" not in u or "/s/" not in u]
        posts.append({
            "num": int(post_id.rsplit("/", 1)[-1]) if post_id.rsplit("/", 1)[-1].isdigit() else 0,
            "id": f"tg_{post_id.replace('/', '_')}",
            "kind": "tg", "source": f"@{channel}", "channel": channel,
            "text": strip_html(text_html),
            "links": list(dict.fromkeys(links)),          # unique, keep order
            "url": f"https://t.me/{post_id}",
            "published": _parse_dt((_TG_TIME.search(chunk) or [None, None])[1]),
        })
    return posts


def fetch_tg_channel(channel: str, since: datetime, max_pages=15) -> list[dict]:
    """Posts of a public channel newer than `since`, paging back with ?before=<id>."""
    out, before = [], None
    for _ in range(max_pages):
        url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
        try:
            page = _parse_tg_page(http_get(url).decode("utf-8", errors="ignore"), channel)
        except Exception as e:
            print(f"TG @{channel}: {e}", flush=True)
            break
        if not page:
            break
        out += [p for p in page if p["published"] and p["published"] >= since]
        oldest = min(page, key=lambda p: p["num"])
        if (oldest["published"] and oldest["published"] < since) or oldest["num"] <= 1:
            break
        before = oldest["num"]
        time.sleep(1)
    uniq = {p["id"]: p for p in out if len(p["text"]) >= 40}
    return sorted(uniq.values(), key=lambda p: p["published"], reverse=True)

# ── Blogs (RSS and Atom) ──────────────────────────────────────────────────────

CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"
MAX_BLOG_CHARS = 6000


def fetch_feed(name: str, url: str, since: datetime) -> list[dict]:
    try:
        root = ET.fromstring(http_get(url))
    except Exception as e:
        print(f"Feed {name}: {e}", flush=True)
        return []
    posts = []
    # RSS 2.0
    for item in root.iter("item"):
        body = item.findtext(CONTENT_NS) or item.findtext("description") or ""
        posts.append((item.findtext("title"), (item.findtext("link") or "").strip(),
                      body, item.findtext("pubDate")))
    # Atom
    for e in root.iter(f"{{{ATOM['atom']}}}entry"):
        link = ""
        for l in e.findall("atom:link", ATOM):
            if l.get("rel") in (None, "alternate"):
                link = l.get("href", ""); break
        body = e.findtext("atom:content", "", ATOM) or e.findtext("atom:summary", "", ATOM)
        posts.append((e.findtext("atom:title", "", ATOM), link, body,
                      e.findtext("atom:published", "", ATOM) or e.findtext("atom:updated", "", ATOM)))
    out = []
    for title, link, body, date in posts:
        published = _parse_dt(date)
        if not link or not published or published < since:
            continue
        text = strip_html(body)
        out.append({
            "id": "blog_" + re.sub(r"[^a-z0-9]", "_", link.lower())[-80:],
            "kind": "blog", "source": name,
            "title": strip_html(title or ""), "abstract": text[:MAX_BLOG_CHARS],
            "words": len(text.split()), "url": link, "published": published,
        })
    return out
