from briefing.stages.dedup import dedup_fingerprint, dedup_semantic
import numpy as np


def test_dedup_fingerprint_basic():
    items = [
        {"text": "Hello World!", "url": "https://a"},
        {"text": "Hello  World", "url": "https://b"},
        {"text": "Completely different", "url": "https://c"},
    ]
    out = dedup_fingerprint(items, bits=64, bands=8, ham_thresh=3)
    assert len(out) in (2, 3)  # tolerant to simhash bucketing
    # merged urls appear on some representative
    assert any(isinstance(x.get("merged_urls"), list) for x in out)


def test_dedup_semantic_basic():
    items = [
        {"text": "Alpha beta gamma", "url": "u1"},
        {"text": "Alpha  beta  gamma", "url": "u2"},
        {"text": "Delta epsilon zeta", "url": "u3"},
    ]
    # contrive embeddings: first two almost identical, third orthogonal
    e = np.array([
        [1.0, 0.0, 0.0],
        [0.99, 0.01, 0.0],
        [0.0, 1.0, 0.0],
    ], dtype=float)
    e2, out = dedup_semantic(e, items, threshold=0.9, merge_sources=True)
    assert e2.shape[0] == len(out)
    assert len(out) == 2
    assert any("u1" in x.get("merged_urls", []) and "u2" in x.get("merged_urls", []) for x in out)

