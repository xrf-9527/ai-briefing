
#!/usr/bin/env python3
import argparse

from briefing.orchestrator import run_once


def main():
    parser = argparse.ArgumentParser(description="AI-Briefing CLI")
    parser.add_argument("--config", required=True, help="Path to YAML config")
    # existing toggles
    parser.add_argument("--multi-stage", dest="multi_stage", action="store_true", help="Enable multi-stage LLM pipeline")
    parser.add_argument("--single-stage", dest="multi_stage", action="store_false", help="Force legacy single-stage summarization")
    parser.add_argument("--agentic-section", dest="agentic_section", action="store_true", help="Force Agentic Focus section output when possible")
    parser.add_argument("--no-agentic-section", dest="agentic_section", action="store_false", help="Disable Agentic Focus section even if configured")
    parser.add_argument("--brief-lite", dest="brief_lite", action="store_true", help="Emit additional condensed brief if supported")
    parser.add_argument("--no-brief-lite", dest="brief_lite", action="store_false", help="Skip condensed brief generation")
    # new processing flags
    parser.add_argument("--dedup", dest="dedup_enabled", action="store_true", help="Enable two-stage dedup")
    parser.add_argument("--no-dedup", dest="dedup_enabled", action="store_false", help="Disable two-stage dedup")
    parser.add_argument("--dedup-threshold", dest="dedup_threshold", type=float, help="Semantic dedup cosine threshold (0-1)")
    parser.add_argument("--dedup-fp", dest="dedup_fp_enabled", action="store_true", help="Enable fingerprint dedup stage")
    parser.add_argument("--no-dedup-fp", dest="dedup_fp_enabled", action="store_false", help="Disable fingerprint dedup stage")
    parser.add_argument("--dedup-fp-bits", dest="dedup_fp_bits", type=int, help="SimHash bits (32-128)")
    parser.add_argument("--dedup-fp-bands", dest="dedup_fp_bands", type=int, help="LSH band count (1-16)")
    parser.add_argument("--dedup-fp-ham", dest="dedup_fp_ham", type=int, help="Hamming threshold within family")
    parser.add_argument("--cluster-algo", dest="cluster_algo", choices=["hdbscan", "kmeans"], help="Clustering algorithm")
    parser.add_argument("--cluster-min-size", dest="cluster_min_size", type=int, help="Min cluster size")
    parser.add_argument("--cluster-k", dest="cluster_k", type=int, help="K for kmeans")
    parser.add_argument("--attach-noise", dest="attach_noise", action="store_true", help="Attach noise points to nearest cluster")
    parser.add_argument("--no-attach-noise", dest="attach_noise", action="store_false", help="Keep -1 noise labels")
    parser.add_argument("--rerank-strategy", dest="rerank_strategy", choices=["none", "ce", "mmr", "ce+mmr"], help="Rerank strategy")
    parser.add_argument("--rerank-lambda", dest="rerank_lambda", type=float, help="MMR lambda (0-1)")
    parser.add_argument("--rerank-model", dest="rerank_model", type=str, help="CrossEncoder model name")
    parser.add_argument("--pack", dest="packer_enabled", action="store_true", help="Enable context packing")
    parser.add_argument("--no-pack", dest="packer_enabled", action="store_false", help="Disable context packing")
    parser.add_argument("--pack-budget", dest="packer_budget", type=int, help="Global token budget")
    parser.add_argument("--pack-min", dest="packer_min", type=int, help="Per-cluster min tokens")
    parser.add_argument("--pack-max", dest="packer_max", type=int, help="Per-cluster max tokens")
    parser.set_defaults(multi_stage=None, agentic_section=None, brief_lite=None, dedup_enabled=None, dedup_fp_enabled=None, attach_noise=None, packer_enabled=None)
    args = parser.parse_args()

    overrides = {
        "multi_stage": args.multi_stage,
        "agentic_section": args.agentic_section,
        "brief_lite": args.brief_lite,
        # new flags
        "dedup_enabled": args.dedup_enabled,
        "dedup_threshold": args.dedup_threshold,
        "dedup_fp_enabled": args.dedup_fp_enabled,
        "dedup_fp_bits": args.dedup_fp_bits,
        "dedup_fp_bands": args.dedup_fp_bands,
        "dedup_fp_ham": args.dedup_fp_ham,
        "cluster_algo": args.cluster_algo,
        "cluster_min_size": args.cluster_min_size,
        "cluster_k": args.cluster_k,
        "attach_noise": args.attach_noise,
        "rerank_strategy": args.rerank_strategy,
        "rerank_lambda": args.rerank_lambda,
        "rerank_model": args.rerank_model,
        "packer_enabled": args.packer_enabled,
        "packer_budget": args.packer_budget,
        "packer_min": args.packer_min,
        "packer_max": args.packer_max,
    }

    run_once(
        args.config,
        multi_stage=args.multi_stage,
        agentic_section=args.agentic_section,
        brief_lite=args.brief_lite,
        overrides=overrides,
    )


if __name__ == "__main__":
    main()
