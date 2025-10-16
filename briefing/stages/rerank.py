from __future__ import annotations

from typing import List, Optional

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from sentence_transformers import CrossEncoder

from briefing.utils import get_logger

logger = get_logger(__name__)


def _rerank_ce(model_name: str, query: str, candidates: List[str]) -> List[int]:
    ce = CrossEncoder(model_name)
    pairs = [[query, c] for c in candidates]
    scores = ce.predict(pairs)
    order = np.argsort(-scores)
    return order.tolist()


def _mmr_select(
    cand_embs: np.ndarray,
    *,
    query_vec: Optional[np.ndarray] = None,
    lam: float = 0.4,
    topk: Optional[int] = None,
) -> List[int]:
    n = cand_embs.shape[0]
    if n == 0:
        return []
    if topk is None:
        topk = n
    q = query_vec
    if q is None:
        q = cand_embs.mean(axis=0)
    q = q.reshape(1, -1)
    sim_q = cosine_similarity(cand_embs, q).reshape(-1)
    sim_mat = cosine_similarity(cand_embs)

    selected: List[int] = []
    remaining = set(range(n))

    # start with best by query similarity
    first = int(np.argmax(sim_q))
    selected.append(first)
    remaining.remove(first)

    while remaining and len(selected) < topk:
        best_i = None
        best_score = -1e9
        for i in list(remaining):
            div = 0.0
            if selected:
                div = max(sim_mat[i, s] for s in selected)
            score = lam * sim_q[i] - (1.0 - lam) * div
            if score > best_score:
                best_score = score
                best_i = i
        selected.append(best_i)  # type: ignore[arg-type]
        remaining.remove(best_i)  # type: ignore[arg-type]
    return selected + [i for i in remaining]


def rerank_candidates(
    *,
    query_text: str,
    candidate_texts: List[str],
    strategy: str,
    model_name: Optional[str] = None,
    cand_embs: Optional[np.ndarray] = None,
    query_vec: Optional[np.ndarray] = None,
    mmr_lambda: float = 0.4,
) -> List[int]:
    if strategy == "none":
        return list(range(len(candidate_texts)))

    if strategy == "ce":
        if not model_name:
            raise ValueError("CE rerank requires model_name")
        return _rerank_ce(model_name, query_text, candidate_texts)

    if strategy == "mmr":
        if cand_embs is None:
            raise ValueError("MMR requires candidate embeddings")
        return _mmr_select(cand_embs, query_vec=query_vec, lam=float(mmr_lambda), topk=len(candidate_texts))

    if strategy == "ce+mmr":
        if not model_name:
            raise ValueError("CE rerank requires model_name")
        ce_order = _rerank_ce(model_name, query_text, candidate_texts)
        if cand_embs is None:
            return ce_order
        # re-order using MMR on embeddings in CE-prioritized order
        embs_ordered = cand_embs[ce_order]
        mmr_order_local = _mmr_select(embs_ordered, query_vec=query_vec, lam=float(mmr_lambda), topk=len(candidate_texts))
        return [ce_order[i] for i in mmr_order_local]

    raise ValueError(f"Unknown rerank strategy: {strategy}")

