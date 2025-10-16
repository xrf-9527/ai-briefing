from __future__ import annotations

from typing import List

import numpy as np
from sklearn.cluster import KMeans
from sklearn.metrics.pairwise import cosine_similarity
import hdbscan

from briefing.utils import get_logger

logger = get_logger(__name__)


def _cluster_hdbscan(embs: np.ndarray, min_cluster_size: int) -> np.ndarray:
    clusterer = hdbscan.HDBSCAN(min_cluster_size=min_cluster_size, metric="euclidean")
    return clusterer.fit_predict(embs)


def _attach_noise(embs: np.ndarray, labels: np.ndarray) -> np.ndarray:
    # Attach -1 points to nearest centroid (cosine similarity)
    if not np.any(labels >= 0):
        return labels
    uniq = sorted({int(x) for x in labels.tolist() if x >= 0})
    centroids = []
    for lb in uniq:
        idxs = np.where(labels == lb)[0]
        centroids.append(embs[idxs].mean(axis=0))
    centroids = np.stack(centroids, axis=0)

    noise = np.where(labels == -1)[0]
    if noise.size == 0:
        return labels

    sims = cosine_similarity(embs[noise], centroids)
    best = sims.argmax(axis=1)
    mapped = np.array([uniq[i] for i in best], dtype=int)
    labels2 = labels.copy()
    labels2[noise] = mapped
    return labels2


def cluster(
    embs: np.ndarray,
    *,
    algo: str = "hdbscan",
    min_cluster_size: int = 3,
    k: int = 20,
    attach_noise: bool = True,
) -> np.ndarray:
    if embs.size == 0:
        return np.zeros((0,), dtype=int)
    if algo == "kmeans":
        km = KMeans(n_clusters=max(2, int(k)), n_init="auto")
        labels = km.fit_predict(embs)
        logger.info("cluster.kmeans: k=%d", k)
        return labels
    else:
        labels = _cluster_hdbscan(embs, min_cluster_size=min_cluster_size)
        if attach_noise:
            labels = _attach_noise(embs, labels)
        uniq = set(labels.tolist())
        n_clusters = len([x for x in uniq if x >= 0])
        n_noise = (labels == -1).sum()
        logger.info(
            "cluster.hdbscan: clusters=%d noise=%d (min_size=%d attach_noise=%s)",
            n_clusters,
            int(n_noise),
            min_cluster_size,
            attach_noise,
        )
        return labels

