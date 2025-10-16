"""Pipeline stages: deduplication, clustering, reranking, packing.

Each stage exposes a small, pure function API and is gated by config flags
under `processing.*` in the runtime configuration.
"""

