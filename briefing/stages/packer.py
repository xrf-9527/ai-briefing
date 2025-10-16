from __future__ import annotations

import re
import typing as t
from dataclasses import dataclass, field

from briefing.utils import get_logger

logger = get_logger(__name__)


def _try_token_len(text: str) -> int:
    try:
        import tiktoken  # type: ignore
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        # heuristic: ~4 chars per token
        return max(1, int(len(text) / 4))


def _sent_split(text: str) -> t.List[str]:
    sents = re.split(r"(?<=[。！？.!?])\s+", (text or "").strip())
    return [s for s in sents if s]


@dataclass
class Excerpt:
    text: str
    urls: t.List[str] = field(default_factory=list)


@dataclass
class PackedTopic:
    topic_id: str
    label: str
    excerpts: t.List[Excerpt] = field(default_factory=list)


def pack_cluster(items: t.List[dict], min_tokens: int, max_tokens: int) -> t.List[Excerpt]:
    used = set()
    total = 0
    out: t.List[Excerpt] = []
    for it in items:
        sents = _sent_split(it.get("text", ""))[:8]
        urls = it.get("merged_urls") or (it.get("urls") or []) or ([it.get("url")] if it.get("url") else [])
        for s in sents:
            s_norm = s.strip()
            if not s_norm or s_norm in used:
                continue
            tl = _try_token_len(s_norm)
            if total + tl > max_tokens and total >= min_tokens:
                return out
            out.append(Excerpt(text=s_norm, urls=[str(u) for u in urls if u]))
            used.add(s_norm)
            total += tl
    return out


def pack(
    clusters: t.List[dict],
    *,
    token_budget: int,
    per_cluster_min: int,
    per_cluster_max: int,
    title: str,
    date_iso: str,
) -> dict:
    remaining = token_budget
    topics: t.List[PackedTopic] = []
    for c in clusters:
        cap = min(per_cluster_max, remaining) if remaining > per_cluster_min else remaining
        ex = pack_cluster(c.get("items", []), per_cluster_min, cap)
        topics.append(PackedTopic(topic_id=c.get("topic_id"), label=c.get("topic_label") or c.get("label", ""), excerpts=ex))
        used = sum(_try_token_len(e.text) for e in ex)
        remaining = max(0, remaining - used)
        if remaining <= 0:
            break
    return {
        "title": title,
        "date": date_iso,
        "topics": [
            {
                "topic_id": t.topic_id,
                "label": t.label,
                "excerpts": [{"text": e.text, "urls": e.urls} for e in t.excerpts],
            }
            for t in topics
        ],
    }

