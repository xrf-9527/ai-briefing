from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from typing import Dict, List, Tuple

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

from briefing.utils import get_logger

logger = get_logger(__name__)


# ---------- Fingerprint (SimHash + banded LSH) ----------

def _canonicalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[“”]", '"', text)
    text = re.sub(r"[‘’]", "'", text)
    return text


def _simhash(text: str, bits: int = 64) -> int:
    v = [0] * bits
    for token in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1
    fp = 0
    for i, val in enumerate(v):
        if val >= 0:
            fp |= 1 << i
    return fp


def _band_buckets(simhashes: List[int], bits: int, bands: int) -> Dict[Tuple[int, int], List[int]]:
    r = max(1, bits // max(1, bands))
    buckets: Dict[Tuple[int, int], List[int]] = {}
    for idx, sh in enumerate(simhashes):
        for b in range(bands):
            key = (b, (sh >> (b * r)) & ((1 << r) - 1))
            buckets.setdefault(key, []).append(idx)
    return buckets


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _select_representative(items: List[dict], idxs: List[int]) -> int:
    # Heuristic: composite score by length and recency if present
    def score_item(i: int) -> float:
        it = items[i]
        txt = (it.get("text") or "").strip()
        length = len(txt)
        ts = it.get("timestamp")
        ts_score = 0.0
        try:
            ts_score = float(ts) if isinstance(ts, (int, float)) else 0.0
        except Exception:
            ts_score = 0.0
        return 0.7 * math.tanh(length / 800.0) + 0.3 * (ts_score / (ts_score + 1.0))

    best = max(idxs, key=score_item)
    return best


def dedup_fingerprint(items: List[dict], *, bits: int = 64, bands: int = 8, ham_thresh: int = 3) -> List[dict]:
    if not items:
        return items
    texts = [_canonicalize(x.get("text", "")) for x in items]
    shs = [_simhash(t, bits=bits) for t in texts]
    buckets = _band_buckets(shs, bits=bits, bands=bands)

    seen = set()
    keep: List[int] = []
    merged_sources: Dict[int, List[str]] = {}

    for _, cand in buckets.items():
        # Within bucket, form families by exact hamming threshold
        family = []  # list of index lists
        for i in cand:
            placed = False
            for grp in family:
                if any(_hamming(shs[i], shs[j]) <= ham_thresh for j in grp):
                    grp.append(i)
                    placed = True
                    break
            if not placed:
                family.append([i])

        for grp in family:
            rep = _select_representative(items, grp)
            if rep not in seen:
                seen.add(rep)
                keep.append(rep)
                merged_sources[rep] = []
            # Merge sources into rep
            urls = []
            for idx in grp:
                it = items[idx]
                u = it.get("url")
                if u:
                    urls.append(str(u))
            merged_sources[rep].extend(urls)

    # Deduplicate keep list and sort by original order
    keep_sorted = sorted(set(keep))
    out: List[dict] = []
    for i in keep_sorted:
        it = dict(items[i])
        if merged_sources.get(i):
            # include rep url as well
            rep_url = it.get("url")
            urls = list(dict.fromkeys(([rep_url] if rep_url else []) + merged_sources[i]))
            it["merged_urls"] = urls
        out.append(it)

    logger.info("dedup.fingerprint: kept=%d from=%d", len(out), len(items))
    return out


# ---------- Semantic dedup (cosine) ----------

def dedup_semantic(
    embs: np.ndarray,
    items: List[dict],
    *,
    threshold: float = 0.92,
    merge_sources: bool = True,
) -> Tuple[np.ndarray, List[dict]]:
    if embs.shape[0] != len(items):
        raise ValueError("embs/items length mismatch for semantic dedup")

    n = embs.shape[0]
    sims = cosine_similarity(embs)
    keep_mask = np.ones(n, dtype=bool)
    rep_for = list(range(n))

    for i in range(n):
        if not keep_mask[i]:
            continue
        for j in range(i + 1, n):
            if keep_mask[j] and sims[i, j] >= threshold:
                keep_mask[j] = False
                rep_for[j] = i

    filtered_items: List[dict] = []
    idx_map = {}
    for old_idx, keep in enumerate(keep_mask.tolist()):
        if keep:
            idx_map[old_idx] = len(filtered_items)
            it = dict(items[old_idx])
            if merge_sources:
                it["merged_urls"] = list(dict.fromkeys([it.get("url")] if it.get("url") else []))
            filtered_items.append(it)

    if merge_sources:
        # Attach duplicate URLs to representatives
        for j in range(n):
            if not keep_mask[j]:
                rep = rep_for[j]
                new_idx = idx_map[rep]
                url = items[j].get("url")
                if url:
                    lst = filtered_items[new_idx].setdefault("merged_urls", [])
                    if str(url) not in lst:
                        lst.append(str(url))

    embs2 = embs[keep_mask]
    logger.info("dedup.semantic: kept=%d from=%d (thr=%.2f)", int(keep_mask.sum()), n, threshold)
    return embs2, filtered_items

