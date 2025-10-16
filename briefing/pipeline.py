
import os
import time
import json
import math
import datetime as dt
from collections import deque
import numpy as np
import requests
import fasttext
from typing import List, Dict, Any, Tuple, Optional
from sklearn.metrics.pairwise import cosine_similarity
import hdbscan
from sentence_transformers import CrossEncoder

from briefing.utils import now_utc, get_logger, parse_datetime_safe
from briefing.stages.dedup import dedup_fingerprint, dedup_semantic
from briefing.stages.clustering import cluster as stage_cluster
from briefing.stages.rerank import rerank_candidates

TEI_ORIGIN = os.getenv("TEI_ORIGIN", "http://tei:3000")
LID_MODEL_PATH = os.getenv("LID_MODEL_PATH", "/workspace/lid.176.bin")
logger = get_logger(__name__)


def _parse_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%s, using default %d", name, raw, default)
        return default


def _parse_env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid float for %s=%s, using default %.2f", name, raw, default)
        return default


EMBED_MAX_BATCH_TOKENS_DEFAULT = max(1, _parse_env_int("EMBED_MAX_BATCH_TOKENS", 8192))
EMBED_MAX_ITEM_CHARS_DEFAULT = max(0, _parse_env_int("EMBED_MAX_ITEM_CHARS", 6000))
EMBED_CHARS_PER_TOKEN_DEFAULT = max(0.1, _parse_env_float("EMBED_CHAR_PER_TOKEN", 4.0))

def _normalize_text_encoding(text: str) -> str:
    """Normalize text encoding to ensure valid UTF-8."""
    original_text = text
    
    # Ensure text is a string
    if not isinstance(text, str):
        logger.debug("Converting non-string input to string: %s", type(text).__name__)
        text = str(text)
    
    # Handle bytes input
    if isinstance(text, bytes):
        logger.debug("Converting bytes to UTF-8 string")
        text = text.decode('utf-8', errors='replace')
    
    # Ensure proper UTF-8 encoding
    text = text.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
    
    if text != original_text and isinstance(original_text, str):
        logger.debug("Text encoding normalized: %s characters changed", 
                    sum(1 for a, b in zip(original_text[:100], text[:100]) if a != b))
    
    return text

def _remove_invalid_escapes(text: str) -> str:
    """Remove incomplete hex/unicode escape sequences that cause JSON parsing errors."""
    import re
    
    original_text = text
    changes = 0
    
    # Replace incomplete \x sequences (not followed by exactly 2 hex digits)
    new_text = re.sub(r'\\x(?![0-9a-fA-F]{2})', ' ', text)
    if new_text != text:
        changes += len(re.findall(r'\\x(?![0-9a-fA-F]{2})', text))
        text = new_text
    
    # Replace incomplete \u sequences (not followed by exactly 4 hex digits) 
    new_text = re.sub(r'\\u(?![0-9a-fA-F]{4})', ' ', text)
    if new_text != text:
        changes += len(re.findall(r'\\u(?![0-9a-fA-F]{4})', text))
        text = new_text
    
    # Replace literal backslashes that might cause issues
    backslash_count = text.count('\\')
    text = text.replace('\\', ' ')
    if backslash_count > 0:
        changes += backslash_count
    
    if changes > 0:
        logger.debug("Removed %d invalid escape sequences from text", changes)
    
    return text

def _filter_control_chars(text: str) -> str:
    """Remove control characters except common whitespace."""
    # Keep only printable characters and common whitespace
    original_len = len(text)
    filtered_text = ''.join(char for char in text if ord(char) >= 32 or char in '\n\r\t')
    
    removed_count = original_len - len(filtered_text)
    if removed_count > 0:
        logger.debug("Filtered %d control characters from text", removed_count)
    
    return filtered_text

def _clean_text_for_embedding(text: str) -> str:
    """Clean text to prevent JSON parsing errors in TEI service."""
    text = _normalize_text_encoding(text)
    text = _remove_invalid_escapes(text)
    text = _filter_control_chars(text)
    return text.strip()

def _embed_texts(
    texts: List[str],
    *,
    max_batch_tokens: int,
    max_item_chars: int,
    chars_per_token: float,
) -> np.ndarray:
    st = time.monotonic()
    max_batch_tokens = max(1, max_batch_tokens)
    chars_per_token = max(0.1, chars_per_token)

    # Clean all texts before processing
    cleaned_texts = [_clean_text_for_embedding(text) for text in texts]
    cleaned_count = sum(1 for orig, clean in zip(texts, cleaned_texts) if orig != clean)
    if cleaned_count > 0:
        logger.info("Cleaned %d texts for embedding processing", cleaned_count)

    max_single_chars = max(1, int(max_batch_tokens * chars_per_token))
    effective_char_limit = max_single_chars
    if max_item_chars > 0:
        effective_char_limit = min(max_item_chars, max_single_chars)

    processed_texts: List[Tuple[int, str, bool]] = []
    truncated_count = 0
    for idx, text in enumerate(cleaned_texts):
        truncated = text
        if len(truncated) > effective_char_limit:
            truncated = truncated[:effective_char_limit]
            truncated_count += 1
        processed_texts.append((idx, truncated, False))

    if truncated_count > 0:
        logger.info(
            "Truncated %d texts for embedding processing (char_limit=%d)",
            truncated_count,
            effective_char_limit,
        )

    def approx_tokens(payload: str) -> int:
        return max(1, int(math.ceil(len(payload) / chars_per_token)))

    # queue entries: (original_index, truncated_text, force_single)
    queue: deque[Tuple[int, str, bool]] = deque(processed_texts)
    all_embs: List[Optional[np.ndarray]] = [None] * len(texts)
    batches_sent = 0

    def enqueue_front(items: List[Tuple[int, str, bool]]) -> None:
        for item in reversed(items):
            queue.appendleft(item)

    while queue:
        batch: List[Tuple[int, str, bool]] = []
        current_tokens = 0

        while queue:
            idx, text, force_single = queue[0]
            tokens = approx_tokens(text)

            if tokens > max_batch_tokens:
                queue.popleft()
                allowed_chars = max(1, int(max_batch_tokens * chars_per_token))
                if len(text) <= allowed_chars:
                    # already within hard bound yet tokens still exceed limit -> shrink aggressively
                    new_length = max(1, len(text) // 2)
                else:
                    new_length = allowed_chars

                if new_length == len(text):
                    raise RuntimeError(
                        f"Text {idx} cannot be reduced below TEI batch limit (len={len(text)})"
                    )

                logger.warning(
                    "Text %d exceeded token limit, trimming to %d chars (was %d)",
                    idx,
                    new_length,
                    len(text),
                )
                queue.appendleft((idx, text[:new_length], True))
                continue

            if batch and (current_tokens + tokens > max_batch_tokens or force_single):
                break

            queue.popleft()
            batch.append((idx, text, force_single))
            current_tokens += tokens
            if force_single:
                break

        if not batch:
            # No batch assembled; likely due to very small max_batch_tokens
            continue

        payload = [text for _, text, _ in batch]
        batch_token_estimate = sum(approx_tokens(text) for text in payload)
        logger.debug(
            "Embedding batch size=%d approx_tokens=%d", len(batch), batch_token_estimate
        )

        max_retries = 3
        sent = False

        for attempt in range(max_retries):
            try:
                resp = requests.post(
                    f"{TEI_ORIGIN}/embeddings", json={"input": payload}, timeout=60
                )
            except requests.exceptions.RequestException as exc:
                if attempt == max_retries - 1:
                    logger.error("TEI embedding failed after %d attempts: %s", max_retries, exc)
                    raise
                logger.warning("TEI embedding attempt %d failed, retrying: %s", attempt + 1, exc)
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 413:
                logger.warning(
                    "TEI embedding 413 for batch size=%d approx_tokens=%d, reducing batch",
                    len(batch),
                    batch_token_estimate,
                )
                if len(batch) > 1:
                    mid = max(1, len(batch) // 2)
                    second_half = [(idx, text, True) for idx, text, _ in batch[mid:]]
                    first_half = [(idx, text, True) for idx, text, _ in batch[:mid]]
                    enqueue_front(second_half)
                    enqueue_front(first_half)
                else:
                    idx, text, _ = batch[0]
                    new_length = max(1, int(len(text) * 0.7))
                    if new_length == len(text):
                        new_length = max(1, len(text) - 1)
                    if new_length <= 0:
                        raise RuntimeError(f"Unable to shrink text {idx} below TEI limit")
                    logger.warning(
                        "Further trimming text %d to %d chars after 413 (was %d)",
                        idx,
                        new_length,
                        len(text),
                    )
                    queue.appendleft((idx, text[:new_length], True))
                time.sleep(1)
                sent = False
                break

            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError as exc:
                if attempt == max_retries - 1:
                    logger.error("TEI embedding failed after %d attempts: %s", max_retries, exc)
                    raise
                logger.warning("TEI embedding attempt %d failed, retrying: %s", attempt + 1, exc)
                time.sleep(2 ** attempt)
                continue

            data = resp.json()
            if "data" in data:
                embs = [d["embedding"] for d in data["data"]]
            else:
                embs = data["embeddings"]

            for (original_idx, _, _), emb in zip(batch, embs):
                all_embs[original_idx] = emb

            batches_sent += 1
            sent = True
            break

        if not sent:
            # batch was re-queued due to 413; continue outer loop
            continue

    missing = [idx for idx, emb in enumerate(all_embs) if emb is None]
    if missing:
        raise RuntimeError(f"Missing embeddings for indices: {missing}")

    arr = np.array(all_embs, dtype=np.float32)
    logger.info(
        "embed_texts count=%d batches=%d took_ms=%d",
        len(texts),
        batches_sent,
        int((time.monotonic() - st) * 1000),
    )
    return arr

def _near_duplicate_mask(embs: np.ndarray, threshold: float) -> List[bool]:
    n = embs.shape[0]
    keep = [True] * n
    duplicates_found = 0
    sims = cosine_similarity(embs)
    
    for i in range(n):
        if not keep[i]:
            continue
        for j in range(i + 1, n):
            if keep[j] and sims[i, j] >= threshold:
                keep[j] = False
                duplicates_found += 1
                logger.debug("Duplicate detected: item %d similar to %d (similarity=%.3f)", j, i, sims[i, j])
    
    logger.info("Near-duplicate detection: %d duplicates found out of %d items (threshold=%.2f)", 
                duplicates_found, n, threshold)
    return keep

def _cluster(embs: np.ndarray, min_cluster_size: int) -> np.ndarray:
    # Kept for backward-compat and tests; prefer stage_cluster
    clusterer = hdbscan.HDBSCAN(min_cluster_size=min_cluster_size, metric='euclidean')
    labels = clusterer.fit_predict(embs)
    unique_labels = set(labels)
    n_clusters = len(unique_labels) - (1 if -1 in unique_labels else 0)
    n_noise = list(labels).count(-1)
    logger.info("Clustering complete: %d clusters found, %d noise points (min_size=%d)", n_clusters, n_noise, min_cluster_size)
    return labels

def _cluster_centrality(embs: np.ndarray, idxs: List[int]) -> Tuple[int, np.ndarray]:
    sub = embs[idxs]
    sims = cosine_similarity(sub, sub)
    scores = sims.mean(axis=1)
    best_local = int(np.argmax(scores))
    return idxs[best_local], sub[best_local]

def _top_k_by_centroid(embs: np.ndarray, idxs: List[int], k: int = 50) -> List[int]:
    centroid = embs[idxs].mean(axis=0, keepdims=True)
    sims = cosine_similarity(embs[idxs], centroid).reshape(-1)
    order = np.argsort(-sims)
    pick = [idxs[i] for i in order[: min(k, len(idxs))]]
    return pick

def _rerank(bge_model: str, query: str, candidates: List[str]) -> List[int]:
    # Legacy CE rerank, retained for compatibility where strategy is implicitly CE
    st = time.monotonic()
    ce = CrossEncoder(bge_model)
    clean_candidates = [_clean_text_for_embedding(c) for c in candidates]
    pairs = [[_clean_text_for_embedding(query), c] for c in clean_candidates]
    scores = ce.predict(pairs)
    order = np.argsort(-scores)
    logger.info("rerank candidates=%d took_ms=%d", len(candidates), int((time.monotonic()-st)*1000))
    return order.tolist()

def run_processing_pipeline(raw_items: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not raw_items:
        return []

    horizon = now_utc().timestamp() - cfg["time_window_hours"] * 3600
    filtered = []
    items_too_old = 0
    items_invalid_ts = 0
    
    for it in raw_items:
        try:
            ts = it["timestamp"]
            if isinstance(ts, str):
                parsed_dt = parse_datetime_safe(ts)
                if parsed_dt is None:
                    raise ValueError("invalid timestamp string")
                t = parsed_dt.timestamp()
            elif isinstance(ts, dt.datetime):
                parsed_dt = ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)
                t = parsed_dt.timestamp()
            elif isinstance(ts, (int, float)):
                t = float(ts)
            else:
                raise TypeError(f"unsupported timestamp type: {type(ts)}")
        except Exception as e:
            logger.warning("Failed to parse timestamp for item %s: %s", it.get("id"), str(e))
            items_invalid_ts += 1
            continue
        
        if t >= horizon:
            filtered.append(it)
        else:
            items_too_old += 1
            logger.debug("Item %s filtered: too old (age=%.1f hours)", 
                        it.get("id"), (now_utc().timestamp() - t) / 3600)
    
    logger.info(
        "Time filter: kept %d items, filtered %d old items, dropped %d invalid timestamps (window=%d hours)",
        len(filtered), items_too_old, items_invalid_ts, cfg["time_window_hours"]
    )

    if not filtered:
        logger.info("pipeline: no items after time_window filter")
        return []

    # Optional: fingerprint dedup prior to embedding (controlled by processing.dedup.fingerprint)
    dedup_cfg = cfg.get("dedup") or {}
    fp_cfg = (dedup_cfg.get("fingerprint") or {}) if dedup_cfg.get("enabled") else {}
    if dedup_cfg.get("enabled") and (fp_cfg.get("enabled", True)):
        try:
            filtered = dedup_fingerprint(
                filtered,
                bits=int(fp_cfg.get("bits", 64)),
                bands=int(fp_cfg.get("bands", 8)),
                ham_thresh=int(fp_cfg.get("ham_thresh", 3)),
            )
        except Exception as e:
            logger.warning("fingerprint dedup failed, continuing without it: %s", e)

    # lid = fasttext.load_model(LID_MODEL_PATH)
    texts = [it["text"] for it in filtered]
    # for tx in texts:
    #     lid.predict(tx.replace("\n", " ")[:1000])  # 标注语言（当前未做强过滤）

    embedding_cfg = cfg.get("embedding", {})
    max_batch_tokens = int(embedding_cfg.get("max_batch_tokens", EMBED_MAX_BATCH_TOKENS_DEFAULT))
    max_item_chars = int(embedding_cfg.get("max_item_chars", EMBED_MAX_ITEM_CHARS_DEFAULT))
    chars_per_token = float(embedding_cfg.get("chars_per_token", EMBED_CHARS_PER_TOKEN_DEFAULT))

    embs = _embed_texts(
        texts,
        max_batch_tokens=max_batch_tokens,
        max_item_chars=max_item_chars,
        chars_per_token=chars_per_token,
    )

    # Semantic deduplication (controlled by processing.dedup.semantic). Fallback to legacy mask.
    use_legacy_near_dup = True
    embs2 = embs
    filtered2 = filtered
    if dedup_cfg:
        use_legacy_near_dup = False
        sem_cfg = dedup_cfg.get("semantic") or {}
        if dedup_cfg.get("enabled") and sem_cfg.get("enabled", True):
            thr = float(sem_cfg.get("threshold", cfg.get("sim_near_dup", 0.92)))
            try:
                embs2, filtered2 = dedup_semantic(embs, filtered, threshold=thr, merge_sources=bool(sem_cfg.get("merge_sources", True)))
            except Exception as e:
                logger.warning("semantic dedup failed, falling back to legacy: %s", e)
                use_legacy_near_dup = True
        else:
            # dedup section provided but semantic disabled -> keep all
            embs2 = embs
            filtered2 = filtered

    if use_legacy_near_dup:
        mask = _near_duplicate_mask(embs, cfg.get("sim_near_dup", 0.92))
        filtered2 = [x for x, m in zip(filtered, mask) if m]
        embs2 = embs[mask]

    if len(filtered2) == 0:
        logger.info("pipeline: all items removed by near-dup filter")
        return []

    # Clustering (algo switchable under processing.clustering)
    cl_cfg = cfg.get("clustering") or {}
    algo = cl_cfg.get("algo", "hdbscan")
    min_cs = int(cl_cfg.get("min_cluster_size", cfg.get("min_cluster_size", 3)))
    k = int(cl_cfg.get("k", 20))
    attach_noise = bool(cl_cfg.get("attach_noise", True))
    try:
        labels = stage_cluster(embs2, algo=algo, min_cluster_size=min_cs, k=k, attach_noise=attach_noise)
    except Exception as e:
        logger.warning("stage clustering failed (algo=%s), using legacy hdbscan: %s", algo, e)
        labels = _cluster(embs2, cfg.get("min_cluster_size", 3))
    clusters: Dict[int, List[int]] = {}
    for i, lb in enumerate(labels):
        clusters.setdefault(lb, []).append(i)

    bundles: List[Dict[str, Any]] = []
    initial_topk = int(cfg.get("initial_topk", 1000))
    max_candidates = int(cfg.get("max_candidates_per_cluster", 300))
    # Reranking strategy
    rerank_cfg = cfg.get("rerank") or {}
    strategy = rerank_cfg.get("strategy", "ce")
    bge_model = rerank_cfg.get("model") or cfg["reranker_model"]
    mmr_lambda = float(rerank_cfg.get("lambda", 0.4))

    for lb, idxs in clusters.items():
        pick = _top_k_by_centroid(embs2, idxs, k=min(initial_topk, len(idxs)))
        pick = pick[:max_candidates]
        best_idx, best_vec = _cluster_centrality(embs2, idxs)
        query_text = filtered2[best_idx]["text"]
        cand_texts = [filtered2[i]["text"] for i in pick]
        if strategy in ("none", "ce", "mmr", "ce+mmr"):
            try:
                order = rerank_candidates(
                    query_text=query_text,
                    candidate_texts=cand_texts,
                    strategy=strategy,
                    model_name=bge_model,
                    cand_embs=embs2[pick],
                    query_vec=best_vec,
                    mmr_lambda=mmr_lambda,
                )
            except Exception as e:
                logger.warning("rerank(strategy=%s) failed, falling back to CE: %s", strategy, e)
                order = _rerank(bge_model, query_text, cand_texts)
        else:
            order = _rerank(bge_model, query_text, cand_texts)
        ordered_items = [filtered2[pick[i]] for i in order]

        bundles.append({
            "topic_id": f"cluster-{lb}",
            "topic_label": None,
            "items": ordered_items
        })

    bundles.sort(key=lambda b: len(b["items"]), reverse=True)
    logger.info("pipeline: bundles=%d", len(bundles))
    return bundles
