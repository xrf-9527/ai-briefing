
import os
import datetime as dt
from typing import List, Dict, Any
import praw

from briefing.utils import clean_text, get_logger

CLIENT_ID = os.getenv("REDDIT_CLIENT_ID")
CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
USER_AGENT = os.getenv("REDDIT_USER_AGENT", "ai-briefing/1.0")
logger = get_logger(__name__)

def _client():
    if not (CLIENT_ID and CLIENT_SECRET and USER_AGENT):
        raise RuntimeError("Reddit credentials missing. Please set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT")
    return praw.Reddit(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        user_agent=USER_AGENT
    )

def fetch(source_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    subreddits = source_config.get("subreddits", [])
    sort_by = source_config.get("sort_by", "top")
    time_window = source_config.get("time_window", "day")
    limit = int(source_config.get("limit_per_subreddit", 20))

    reddit = _client()
    items: List[Dict[str, Any]] = []

    for sub in subreddits:
        sr = reddit.subreddit(sub)
        if sort_by == "new":
            posts = sr.new(limit=limit)
        elif sort_by == "hot":
            posts = sr.hot(limit=limit)
        elif sort_by == "rising":
            posts = sr.rising(limit=limit)
        else:
            posts = sr.top(time_filter=time_window, limit=limit)

        for p in posts:
            text = clean_text(f"{p.title}\n\n{p.selftext or ''}")
            created = dt.datetime.fromtimestamp(p.created_utc, tz=dt.timezone.utc)
            items.append({
                "id": p.id,
                "text": text,
                "url": f"https://www.reddit.com{p.permalink}",
                "author": str(p.author) if p.author else "Unknown",
                "timestamp": created.isoformat(),
                "metadata": {"source": "reddit", "subreddit": sub}
            })

    logger.info("reddit_adapter fetched_items=%d subs=%s", len(items), ",".join(subreddits))
    return items

