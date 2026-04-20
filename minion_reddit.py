#!/usr/bin/env python3
"""
minion_reddit.py — Posts helpful Reddit comments for the 3 latest published articles.
Runs Mon/Wed/Fri at 10:00 UTC via GitHub Actions.

Reddit OAuth requires CLIENT_ID + CLIENT_SECRET even for password flow.
Get them free in 30 seconds: reddit.com/prefs/apps → "create another app"
→ type "script" → any name/redirect URL → copy client_id (under app name) + secret.
"""

import json
import os
import re
import sys
import time
import random
from datetime import date, datetime
from pathlib import Path

import anthropic
import praw
import praw.exceptions
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
MODEL = "claude-sonnet-4-6"
DATA_DIR = Path("data")
POSTS_EN = Path("posts/en")
PUBLISHED_FILE = DATA_DIR / "published.json"
REDDIT_LOG_FILE = DATA_DIR / "reddit_log.json"
BLOG_BASE_URL = "https://saikolabo.github.io/Upline-AI"

# Ordered keyword → subreddit mapping (first match wins)
KEYWORD_SUBREDDIT: list[tuple[str, str]] = [
    ("fps",           "pcgaming"),
    ("gaming",        "pcgaming"),
    ("nvidia",        "pcgaming"),
    ("noise",         "Twitch"),
    ("stream",        "Twitch"),
    ("highlight",     "Twitch"),
    ("transcription", "Twitch"),
    ("twitch",        "Twitch"),
    ("krisp",         "Twitch"),
    ("ai",            "artificial"),
]

# French-origin slug prefixes — used to skip FR articles (Reddit is EN)
FR_PREFIXES = ("ia-", "comment-", "outil-", "supprimer-", "ameliorer-",
               "creer-", "utiliser-", "generateur-")

REDDIT_SYSTEM = """\
You are an experienced gamer and streamer who genuinely helps people in gaming communities.
Write a Reddit comment that:
- Opens by directly addressing a real pain point users in that subreddit face
- Shares actionable, specific advice as if from personal experience
- Is 100-150 words, casual and conversational — never sounds like an ad
- Ends with the article URL on its own line, preceded by "More detail here:"
- Never uses phrases like "check this out", "great article", or "I found this resource"
"""


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M')}] {msg}", flush=True)


def pick_subreddit(slug: str) -> str:
    for keyword, sub in KEYWORD_SUBREDDIT:
        if keyword in slug.lower():
            return sub
    return "Twitch"


def load_reddit_log() -> dict:
    if REDDIT_LOG_FILE.exists():
        return json.loads(REDDIT_LOG_FILE.read_text(encoding="utf-8"))
    return {"posts": []}


def save_reddit_log(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    REDDIT_LOG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def already_posted(log_data: dict, slug: str, subreddit: str) -> bool:
    return any(
        p["slug"] == slug and p["subreddit"] == subreddit
        for p in log_data.get("posts", [])
    )


def parse_frontmatter(md_path: Path) -> dict:
    text = md_path.read_text(encoding="utf-8")
    match = re.search(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}
    fm: dict[str, str] = {}
    for line in match.group(1).split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip().strip("\"'")
    return fm


def find_en_article(slug: str) -> tuple[str, str] | None:
    """Return (title, url) for the EN article matching the slug, or None."""
    if not POSTS_EN.exists():
        return None
    for md_file in POSTS_EN.glob(f"*{slug}*.md"):
        fm = parse_frontmatter(md_file)
        title = fm.get("title", slug)
        article_slug = fm.get("slug", slug)
        return title, f"{BLOG_BASE_URL}/{article_slug}/"
    return None


def get_last_en_slugs(n: int = 3) -> list[str]:
    """Return the n most recent EN slugs from published.json."""
    if not PUBLISHED_FILE.exists():
        return []
    data = json.loads(PUBLISHED_FILE.read_text(encoding="utf-8"))
    slugs = data.get("slugs", [])
    en_slugs = [
        s for s in slugs
        if not any(c in s for c in "éèàùûîôêç")
        and not s.startswith(FR_PREFIXES)
    ]
    return en_slugs[-n:]


def generate_comment(
    title: str, url: str, subreddit: str, client: anthropic.Anthropic
) -> str:
    prompt = f"""Write a Reddit comment for r/{subreddit}.

Article topic: "{title}"
Article URL (place at end): {url}

The comment must feel like genuine peer advice, not a blog promotion.
Open with a specific, relatable problem this subreddit users face about this topic."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=350,
        system=[{"type": "text", "text": REDDIT_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def try_comment_on_existing(
    reddit: praw.Reddit, subreddit_name: str, slug: str, comment_text: str
) -> str | None:
    """Search for a relevant post and reply to it. Returns permalink or None."""
    query = slug.replace("-", " ")
    try:
        results = list(
            reddit.subreddit(subreddit_name).search(
                query, sort="new", time_filter="month", limit=10
            )
        )
        if results:
            comment = results[0].reply(comment_text)
            return f"https://reddit.com{comment.permalink}"
    except Exception as exc:
        log(f"  Comment on existing post failed: {exc}")
    return None


def try_submit_post(
    reddit: praw.Reddit, subreddit_name: str, title: str, text: str
) -> str | None:
    """Submit a new self-text post as fallback. Returns permalink or None."""
    try:
        submission = reddit.subreddit(subreddit_name).submit(
            title=title, selftext=text, send_replies=False
        )
        return f"https://reddit.com{submission.permalink}"
    except Exception as exc:
        log(f"  Submit new post failed: {exc}")
    return None


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        log("DRY-RUN mode — nothing will be posted to Reddit")

    en_slugs = get_last_en_slugs(3)
    if not en_slugs:
        log("No EN slugs found in published.json — nothing to post")
        sys.exit(0)

    log(f"Selected articles: {en_slugs}")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    reddit = None
    if not dry_run:
        reddit = praw.Reddit(
            client_id=os.environ["REDDIT_CLIENT_ID"],
            client_secret=os.environ["REDDIT_CLIENT_SECRET"],
            username=os.environ["REDDIT_USERNAME"],
            password=os.environ["REDDIT_PASSWORD"],
            user_agent=f"UplineAI:v1.0 (by u/{os.environ['REDDIT_USERNAME']})",
        )

    log_data = load_reddit_log()

    for slug in en_slugs:
        subreddit = pick_subreddit(slug)

        if already_posted(log_data, slug, subreddit):
            log(f"Skip (already posted): {slug} → r/{subreddit}")
            continue

        article = find_en_article(slug)
        if not article:
            log(f"Skip (article file not found): {slug}")
            continue

        title, url = article
        log(f"Generating comment: {slug} → r/{subreddit}")

        try:
            comment_text = generate_comment(title, url, subreddit, client)
        except Exception as exc:
            log(f"  Claude error: {exc}")
            continue

        if dry_run:
            log(f"\n{'─'*50}")
            log(f"  Subreddit : r/{subreddit}")
            log(f"  Article   : {title}")
            log(f"  URL       : {url}")
            log(f"  Comment   :\n{comment_text}")
            log(f"{'─'*50}\n")
            continue

        # First try to comment on an existing relevant post; fall back to new post
        permalink = try_comment_on_existing(reddit, subreddit, slug, comment_text)
        if not permalink:
            log("  No relevant post found — submitting a new post...")
            permalink = try_submit_post(reddit, subreddit, title, comment_text)

        if permalink:
            log(f"  Posted: {permalink}")
            log_data["posts"].append({
                "slug": slug,
                "subreddit": subreddit,
                "permalink": permalink,
                "posted_at": datetime.utcnow().isoformat() + "Z",
            })
            save_reddit_log(log_data)
        else:
            log(f"  ERROR: could not post to r/{subreddit}")

        # Respect Reddit rate limits between posts
        time.sleep(random.uniform(45, 90))

    log("Reddit run complete")


if __name__ == "__main__":
    main()
