
import time
import datetime as dt
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any
from briefing.utils import clean_text, get_logger, normalize_http_url

logger = get_logger(__name__)

BASE = "https://hacker-news.firebaseio.com/v0"

def _story_ids(story_type: str) -> List[int]:
    if story_type == "new":
        path = "newstories"
    elif story_type == "best":
        path = "beststories"
    else:
        path = "topstories"
    url = f"{BASE}/{path}.json"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.json() or []

def _get_item(item_id: int) -> dict:
    url = f"{BASE}/item/{item_id}.json"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.json() or {}

def _parse_story(sid: int, js: dict) -> Dict[str, Any] | None:
    """Parse a single HN story JSON into a standardized item dict, or None."""
    if not js or js.get("type") != "story":
        return None
    title = js.get("title") or ""
    text = js.get("text") or ""
    raw_url = js.get("url") or f"https://news.ycombinator.com/item?id={sid}"
    url = normalize_http_url(raw_url)
    if not url:
        logger.warning("hackernews_adapter: drop item %s due to invalid url", sid)
        return None

    author = js.get("by") or "Unknown"
    created = js.get("time", int(time.time()))
    ts = dt.datetime.fromtimestamp(created, tz=dt.timezone.utc)

    content = clean_text(f"{title}\n\n{text}")
    if not content:
        logger.warning("hackernews_adapter: drop item %s due to empty content after cleaning", sid)
        return None

    return {
        "id": str(sid),
        "text": content,
        "url": url,
        "author": author,
        "timestamp": ts.isoformat(),
        "metadata": {"source": "hackernews", "score": js.get("score")}
    }


def fetch(source_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    story_type = source_config.get("hn_story_type", "top")
    limit = int(source_config.get("hn_limit", 50))
    ids = _story_ids(story_type)[:limit]

    items: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_get_item, sid): sid for sid in ids}
        for future in as_completed(futures):
            sid = futures[future]
            try:
                js = future.result()
            except Exception as exc:
                logger.warning("hackernews_adapter: failed to fetch item %s: %s", sid, exc)
                continue
            parsed = _parse_story(sid, js)
            if parsed:
                items.append(parsed)

    # Preserve original ordering by story id position
    id_order = {str(sid): idx for idx, sid in enumerate(ids)}
    items.sort(key=lambda it: id_order.get(it["id"], len(ids)))

    logger.info("hackernews_adapter fetched_items=%d type=%s", len(items), story_type)
    return items

