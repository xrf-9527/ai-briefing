
import os
import argparse
import time
import yaml
import uuid
import json
import requests
from pathlib import Path
from typing import Dict, Any, List, Optional

from briefing.sources import twitter_list_adapter, rss_adapter, reddit_adapter, hackernews_adapter
from briefing.pipeline import run_processing_pipeline
from briefing.summarizer import generate_summary
from briefing.pipeline_multistep import compute_metrics, run_multistage_pipeline
from briefing.publisher import maybe_publish_telegram, maybe_briefing_archive
from briefing.rendering.markdown import render_md
from briefing.utils import write_output, validate_config, wait_for_service, get_logger, now_utc
from briefing.stages.packer import pack as pack_context

logger = get_logger(__name__)

def _wait_infra(source_type=None):
    """Wait for infrastructure services to be ready."""
    tei = os.getenv("TEI_ORIGIN", "http://tei:3000") + "/health"
    
    # Only check RSSHub if actually needed
    if source_type == "twitter_list":
        rsshub = os.getenv("RSSHUB_ORIGIN", "http://rsshub:1200") + "/healthz"
        wait_for_service(rsshub)
    
    # TEI is critical
    wait_for_service(tei)
def _fetch_items(source_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch items from configured source."""
    t = source_cfg["type"]
    if t == "twitter_list":
        return twitter_list_adapter.fetch(source_cfg)
    elif t == "rss":
        return rss_adapter.fetch(source_cfg)
    elif t == "reddit":
        return reddit_adapter.fetch(source_cfg)
    elif t == "hackernews":
        return hackernews_adapter.fetch(source_cfg)
    else:
        raise ValueError(f"Unknown source type: {t}")

def _apply_overrides(cfg: Dict[str, Any], overrides: Optional[Dict[str, Any]]) -> None:
    if not overrides:
        return

    processing = cfg.setdefault("processing", {})
    if overrides.get("multi_stage") is not None:
        processing["multi_stage"] = overrides["multi_stage"]
    if overrides.get("agentic_section") is not None:
        processing["agentic_section"] = overrides["agentic_section"]
    if overrides.get("brief_lite") is not None:
        processing["brief_lite"] = overrides["brief_lite"]

    # Dedup
    if (
        overrides.get("dedup_enabled") is not None
        or overrides.get("dedup_threshold") is not None
        or overrides.get("dedup_fp_enabled") is not None
        or overrides.get("dedup_fp_bits") is not None
        or overrides.get("dedup_fp_bands") is not None
        or overrides.get("dedup_fp_ham") is not None
    ):
        dd = processing.setdefault("dedup", {})
        if overrides.get("dedup_enabled") is not None:
            dd["enabled"] = overrides["dedup_enabled"]
        sem = dd.setdefault("semantic", {})
        if overrides.get("dedup_threshold") is not None:
            sem["enabled"] = True
            sem["threshold"] = float(overrides["dedup_threshold"])  # type: ignore[arg-type]
        fp = dd.setdefault("fingerprint", {})
        if overrides.get("dedup_fp_enabled") is not None:
            fp["enabled"] = overrides["dedup_fp_enabled"]
        if overrides.get("dedup_fp_bits") is not None:
            fp["bits"] = int(overrides["dedup_fp_bits"])  # type: ignore[arg-type]
        if overrides.get("dedup_fp_bands") is not None:
            fp["bands"] = int(overrides["dedup_fp_bands"])  # type: ignore[arg-type]
        if overrides.get("dedup_fp_ham") is not None:
            fp["ham_thresh"] = int(overrides["dedup_fp_ham"])  # type: ignore[arg-type]

    # Clustering
    if (
        overrides.get("cluster_algo") is not None
        or overrides.get("cluster_min_size") is not None
        or overrides.get("cluster_k") is not None
        or overrides.get("attach_noise") is not None
    ):
        cl = processing.setdefault("clustering", {})
        if overrides.get("cluster_algo") is not None:
            cl["algo"] = overrides["cluster_algo"]
        if overrides.get("cluster_min_size") is not None:
            v = int(overrides["cluster_min_size"])  # type: ignore[arg-type]
            cl["min_cluster_size"] = v
            processing["min_cluster_size"] = v
        if overrides.get("cluster_k") is not None:
            cl["k"] = int(overrides["cluster_k"])  # type: ignore[arg-type]
        if overrides.get("attach_noise") is not None:
            cl["attach_noise"] = overrides["attach_noise"]

    # Rerank
    if (
        overrides.get("rerank_strategy") is not None
        or overrides.get("rerank_lambda") is not None
        or overrides.get("rerank_model") is not None
    ):
        rr = processing.setdefault("rerank", {})
        if overrides.get("rerank_strategy") is not None:
            rr["strategy"] = overrides["rerank_strategy"]
        if overrides.get("rerank_lambda") is not None:
            rr["lambda"] = float(overrides["rerank_lambda"])  # type: ignore[arg-type]
        if overrides.get("rerank_model") is not None:
            rr["model"] = overrides["rerank_model"]

    # Packer
    if (
        overrides.get("packer_enabled") is not None
        or overrides.get("packer_budget") is not None
        or overrides.get("packer_min") is not None
        or overrides.get("packer_max") is not None
    ):
        pk = processing.setdefault("packer", {})
        if overrides.get("packer_enabled") is not None:
            pk["enabled"] = overrides["packer_enabled"]
        if overrides.get("packer_budget") is not None:
            pk["budget"] = int(overrides["packer_budget"])  # type: ignore[arg-type]
        if overrides.get("packer_min") is not None:
            pk["per_cluster_min"] = int(overrides["packer_min"])  # type: ignore[arg-type]
        if overrides.get("packer_max") is not None:
            pk["per_cluster_max"] = int(overrides["packer_max"])  # type: ignore[arg-type]


def _execute_pipeline(cfg: Dict[str, Any], run_id: str, overrides: Optional[Dict[str, Optional[bool]]] = None) -> None:
    """Execute the core briefing pipeline with given configuration."""
    briefing_id = cfg["briefing_id"]
    source_type = cfg["source"]["type"]
    logger.info("config loaded briefing_id=%s title=%s source=%s", briefing_id, cfg["briefing_title"], source_type)

    _apply_overrides(cfg, overrides)

    _wait_infra(source_type)

    t0 = time.monotonic()
    raw_items = _fetch_items(cfg["source"])
    logger.info("fetched items=%d took_ms=%d", len(raw_items), int((time.monotonic()-t0)*1000))

    t1 = time.monotonic()
    bundles = run_processing_pipeline(raw_items, cfg["processing"])
    logger.info("processed bundles=%d took_ms=%d", len(bundles), int((time.monotonic()-t1)*1000))

    use_multi_stage = bool(cfg.get("processing", {}).get("multi_stage"))

    # Optional: context packer artifact for downstream prompts
    try:
        pack_cfg = cfg.get("processing", {}).get("packer") or {}
        if pack_cfg.get("enabled"):
            token_budget = int(pack_cfg.get("budget", 6000))
            per_min = int(pack_cfg.get("per_cluster_min", 300))
            per_max = int(pack_cfg.get("per_cluster_max", 1200))
            packed = pack_context(
                bundles,
                token_budget=token_budget,
                per_cluster_min=per_min,
                per_cluster_max=per_max,
                title=cfg.get("briefing_title", "Daily Engineering Briefing"),
                date_iso=now_utc().isoformat().replace("+00:00", "Z"),
            )
            artifact_root = Path(cfg["output"]["dir"]) / "packed.json"
            artifact_root.parent.mkdir(parents=True, exist_ok=True)
            artifact_root.write_text(json.dumps(packed, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("packed context written: %s topics=%d", artifact_root, len(packed.get("topics", [])))
    except Exception as e:
        logger.warning("context packer failed: %s", e)

    if use_multi_stage:
        t2 = time.monotonic()
        briefing_obj, state = run_multistage_pipeline(
            bundles,
            cfg,
            briefing_id=briefing_id,
            output_root=Path(cfg["output"]["dir"]),
        )
        js = briefing_obj.model_dump(mode="json")
        md = render_md(js, cfg.get("rendering", {}))
        metrics = compute_metrics(state, briefing_obj, cfg)
        logger.info(
            "multi-stage summarize took_ms=%d metrics=%s",
            int((time.monotonic()-t2) * 1000),
            metrics,
        )
        if state.artifact_root:
            metrics_path = state.artifact_root / "metrics.json"
            metrics_path.parent.mkdir(parents=True, exist_ok=True)
            metrics_path.write_text(
                json.dumps(metrics, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    else:
        t2 = time.monotonic()
        md, js = generate_summary(bundles, cfg)
        logger.info("summarized took_ms=%d", int((time.monotonic()-t2)*1000))

    if md is None or js is None:
        logger.info("empty briefing -> skip output & publish")
        return

    out_dir = cfg["output"]["dir"]
    generated_files = write_output(md, js, cfg["output"])
    logger.info("output written dir=%s", out_dir)

    try:
        maybe_publish_telegram(md, cfg["output"])
    except Exception as e:
        logger.error("telegram publish failed: %s", e)

    try:
        maybe_briefing_archive(generated_files, cfg["output"], briefing_id, run_id)
    except Exception as e:
        logger.error("github backup failed: %s", e)

    logger.info("OK: briefing generated and published.")

def main():
    """Main entry point for CLI usage."""
    parser = argparse.ArgumentParser(description="Run a briefing generation task.")
    parser.add_argument('--config', type=str, required=True, help="Path to the briefing config YAML file.")
    parser.add_argument('--multi-stage', dest='multi_stage', action='store_true', help="Enable multi-stage LLM pipeline")
    parser.add_argument('--single-stage', dest='multi_stage', action='store_false', help="Use legacy single-stage summarizer")
    parser.add_argument('--agentic-section', dest='agentic_section', action='store_true', help="Force Agentic Focus section")
    parser.add_argument('--no-agentic-section', dest='agentic_section', action='store_false', help="Disable Agentic Focus section")
    parser.add_argument('--brief-lite', dest='brief_lite', action='store_true', help="Emit condensed brief if available")
    parser.add_argument('--no-brief-lite', dest='brief_lite', action='store_false', help="Skip condensed brief")
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

    run_id = uuid.uuid4().hex[:8]
    logger.info("=== run start id=%s ===", run_id)

    try:
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        validate_config(cfg)
        overrides = {
            "multi_stage": args.multi_stage,
            "agentic_section": args.agentic_section,
            "brief_lite": args.brief_lite,
            # new
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
        _execute_pipeline(cfg, run_id, overrides)
        
    except Exception as e:
        logger.error("Pipeline execution failed: %s", e)
        raise
    finally:
        logger.info("=== run end id=%s ===", run_id)

if __name__ == "__main__":
    main()



def run_once(
    config_path: str,
    *,
    multi_stage: Optional[bool] = None,
    agentic_section: Optional[bool] = None,
    brief_lite: Optional[bool] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> None:
    """Execute pipeline once with given config file path."""
    run_id = uuid.uuid4().hex[:8]
    logger.info("=== run start id=%s ===", run_id)
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        validate_config(cfg)
        base_overrides = {
            "multi_stage": multi_stage,
            "agentic_section": agentic_section,
            "brief_lite": brief_lite,
        }
        if overrides:
            base_overrides.update({k: v for k, v in overrides.items() if v is not None})
        _execute_pipeline(cfg, run_id, base_overrides)
        
    except Exception as e:
        logger.error("Pipeline execution failed: %s", e)
        raise
    finally:
        logger.info("=== run end id=%s ===", run_id)
