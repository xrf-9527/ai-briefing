from briefing.stages.packer import pack


def test_pack_creates_topics():
    clusters = [
        {
            "topic_id": "cluster-0",
            "topic_label": "Test",
            "items": [
                {"text": "Sentence one. Sentence two.", "merged_urls": ["u1"]},
                {"text": "Another sentence. More.", "merged_urls": ["u2"]},
            ],
        }
    ]
    out = pack(
        clusters,
        token_budget=200,
        per_cluster_min=10,
        per_cluster_max=50,
        title="T",
        date_iso="2025-01-01T00:00:00Z",
    )
    assert "topics" in out and len(out["topics"]) == 1
    assert out["topics"][0]["excerpts"]

