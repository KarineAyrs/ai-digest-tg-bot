"""
prompts.py — prompts for every stage of the digest.

Personalisation:
  Copy system_prompt.example.txt → system_prompt.txt and fill in your profile.
  The file is gitignored and never committed.
  If system_prompt.txt is absent, a generic researcher profile is used.
"""

from pathlib import Path

LANGUAGE = "Russian"

_DEFAULT_PROFILE = """AI researcher and practitioner.

Priority topics:
1. New LLM/transformer architectures and training innovations
2. NLP breakthroughs — reasoning, long context, multimodal
3. Reinforcement learning for LLMs (RLHF, GRPO, PPO, reward modeling)
4. AI safety: agent security, prompt injection, jailbreaks, sandboxing
5. Surprising industry moves or benchmark-breaking results"""


def load_profile() -> str:
    """Load personal profile from system_prompt.txt, fall back to generic."""
    path = Path(__file__).resolve().parent / "system_prompt.txt"
    if path.exists():
        content = path.read_text().strip()
        if content:
            return content
    return _DEFAULT_PROFILE


def _header(task: str) -> str:
    return f"""You are an expert AI research assistant. {task}

Reader profile:
{load_profile()}
"""


_STYLE = f"""- Write all text fields in {LANGUAGE}. Keep established technical terms and
  method/model/benchmark names in English (e.g. RLHF, GRPO, prompt injection, MoE).
- Use only what the item text supports. If the text says nothing for a field, return "" for it,
  do not guess.
- Plain text only: no markdown, no asterisks, no emoji."""

_SCORE = """- score: significance for this reader. 5 = must read (breakthrough or directly on priority topics),
  4 = strong and relevant, 3 = solid, 2 = minor, 1 = marginal."""


# ── Papers ────────────────────────────────────────────────────────────────────

def build_titles_prompt(titles_text: str, n_items: int, n_select: int) -> str:
    """Stage 1 for papers: cheap triage by titles only."""
    return _header("You are triaging this week's arXiv papers by title.") + f"""
From the {n_items} numbered titles below, pick up to {n_select} papers most worth reading for this reader:
novel methods and strong results on the priority topics. Skip incremental work, narrow
applications, surveys without new insight, and duplicates of the same idea.

Return ONLY a JSON array of item numbers, most promising first, e.g. [12, 3, 57].

Titles:
{titles_text}"""


def build_papers_prompt(items_text: str, n_items: int, n_select: int, context: str) -> str:
    """Stage 2 for papers: pick the best by abstracts and write a short review."""
    return _header(f"You are curating a {context}.") + f"""
From the {n_items} numbered papers below, select exactly {min(n_select, n_items)} most important
and non-trivial ones. Skip incremental work and duplicate topics.

Return ONLY a JSON array, no code fences and no text before or after:
[{{"n": <item number>, "score": <1-5>, "title": "...", "ideas": "...", "why": "...", "artifacts": "..."}}, ...]

Rules:
{_SCORE}
- title: up to 90 characters, says what was done or found.
- ideas (Ключевые идеи): what exactly was done and the key idea of the method, what is done
  differently and why it works. 1–3 sentences, up to 350 characters.
- why (Почему это важно): why it matters for the field and for this reader, plus the key caveat:
  conceptual only, narrow setup, weak baselines. 1–2 sentences, up to 300 characters.
  Words, not a list of numbers: at most one key number, and only if it is the main point.
- artifacts (Артефакты): code, weights, datasets mentioned in the text, with URLs copied exactly.
  "" if none. Never invent links.
{_STYLE}

Papers:
{items_text}"""


def build_deep_prompt(title: str, fulltext: str) -> str:
    """Stage 3 for papers: full-text review of one paper (Sonnet)."""
    return _header("You are writing a short, precise review of one paper from its full text.") + f"""
Return ONLY a JSON object, no code fences and no text before or after:
{{"ideas": "...", "why": "...", "artifacts": "..."}}

Rules:
- ideas (Ключевые идеи): the problem, the method and what exactly is new compared to prior work;
  the mechanism, not marketing. 2–4 sentences, up to 600 characters.
- why (Почему это важно): significance for the field and for this reader, plus the main limitations:
  scale, narrow setup, weak or missing baselines, no code, claims not backed by experiments.
  Up to 400 characters. Words, not a list of numbers: at most one key number, only if it is the main point.
- artifacts (Артефакты): code, weights, datasets, demos with URLs copied exactly from the text.
  "" if none are mentioned. Never invent links.
{_STYLE}

Paper title: {title}

Full text:
{fulltext}

Reminder: the paper is in English, but write "ideas" and "why" in {LANGUAGE}."""


# ── Telegram channels ─────────────────────────────────────────────────────────

def build_tg_prompt(posts_text: str, n_posts: int, n_select: int) -> str:
    return _header("You are curating a weekly digest of AI Telegram channels.") + f"""
Below are {n_posts} numbered posts. The same news is often covered by several channels:
group such posts into ONE item. Select up to {n_select} most important items for this reader.
Skip ads, memes, job posts, event announcements and self-promotion.

Return ONLY a JSON array, no code fences and no text before or after:
[{{"n": [<post numbers about this item, most informative first>], "score": <1-5>, "title": "...", "what": "...", "why": "...", "origin": "..."}}, ...]

Rules:
{_SCORE}
- title: up to 90 characters, says what happened.
- what (Что произошло): the facts in 1–3 sentences, up to 350 characters.
- why (Почему это важно): why it matters for this reader, plus a caveat if it is a rumour,
  a retelling or an unconfirmed claim. Up to 300 characters.
- origin (Первоисточник): one URL of the original source (paper, company blog, repo, tweet),
  copied exactly from the "Links" of the grouped posts. "" if there is none.
{_STYLE}

Posts:
{posts_text}"""


# ── Blogs ─────────────────────────────────────────────────────────────────────

def build_blogs_prompt(items_text: str, n_items: int, n_select: int) -> str:
    return _header("You are curating a digest of posts by influential AI authors.") + f"""
From the {n_items} numbered posts below, select up to {min(n_select, n_items)} worth the reader's attention.

Return ONLY a JSON array, no code fences and no text before or after:
[{{"n": <item number>, "score": <1-5>, "title": "...", "thesis": "...", "arguments": "..."}}, ...]

Rules:
{_SCORE}
- title: up to 90 characters, the main point of the post (not just the original title).
- thesis (Главная мысль): the author's main claim or insight, 1–2 sentences, up to 300 characters.
- arguments (Аргументы и данные): what the claim rests on: data, experiments, examples,
  experience. Up to 350 characters.
{_STYLE}

Posts:
{items_text}"""
