"""Multi-stage LLM pipeline for daily briefing generation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml
from jinja2 import Environment
from statistics import mean

from briefing.llm.registry import call_with_schema
from briefing.models import (
    Bullet,
    BulletDraft,
    Briefing,
    ClusterBundle,
    ClusterFacts,
    ClusterSelection,
    DroppedFact,
    FactScores,
    ScoredFact,
    Topic,
    TopicDraft,
)
from briefing.utils import get_logger, parse_datetime_safe, normalize_http_url
from pydantic import ValidationError

logger = get_logger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

with open(SCHEMA_DIR / "briefing.schema.json", "r", encoding="utf-8") as _fh:
    FINAL_BRIEFING_SCHEMA = json.load(_fh)


ENV = Environment(autoescape=False, trim_blocks=True, lstrip_blocks=True)


@dataclass
class LLMSettings:
    provider: str
    model: str
    temperature: float
    timeout: int
    retries: int
    options: Optional[Dict[str, Any]] = None


DEFAULT_PROMPTS = {
    "stage1": PROMPT_DIR / "stage1_extract_facts.yaml",
    "stage2": PROMPT_DIR / "stage2_score_select.yaml",
    "stage3": PROMPT_DIR / "stage3_compose_bullets.yaml",
    "stage4": PROMPT_DIR / "stage4_format_qc.yaml",
}


DEFAULT_SCORING_WEIGHTS: Dict[str, float] = {
    "actionability": 3.0,
    "novelty": 2.0,
    "impact": 2.0,
    "reusability": 2.0,
    "reliability": 1.0,
    "agentic_bonus": 1.0,
}


@dataclass
class PipelineState:
    bundles: Dict[str, ClusterBundle]
    facts: Dict[str, ClusterFacts]
    selections: Dict[str, ClusterSelection]
    topics: Dict[str, TopicDraft]
    artifact_root: Optional[Path] = None


def _get_with_fallback(mapping: Dict[str, Any], key: str) -> Optional[Any]:
    if key in mapping:
        return mapping[key]
    if key.startswith("cluster-") and key[len("cluster-"):] in mapping:
        return mapping[key[len("cluster-"):]]
    return None


def _safe_dir_name(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in value)
    return safe or "cluster"


def _sanitize_topic_id(raw: str) -> str:
    if not raw:
        return "cluster-0"
    cleaned = raw
    while cleaned.startswith("cluster-"):
        cleaned = cleaned[len("cluster-"):]
    return f"cluster-{cleaned or '0'}"


def _sanitize_bundle_like(raw_bundle: Any) -> Dict[str, Any]:
    """Filter/repair invalid items in a raw bundle dict before model validation.

    This prevents an entire cluster from being dropped due to a single bad URL.
    - Repairs item and canonical link URLs when possible; otherwise drops them.
    - Leaves other fields untouched for downstream validation.
    """
    if isinstance(raw_bundle, ClusterBundle):
        # Already validated
        return raw_bundle.model_dump(mode="python")

    data = dict(raw_bundle) if isinstance(raw_bundle, dict) else {}
    # Normalize items
    items = data.get("items") or []
    sanitized_items = []
    dropped = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        url = normalize_http_url(it.get("url"))
        if not url:
            dropped += 1
            continue
        # Keep original fields; just patch normalized URL
        new_it = dict(it)
        new_it["url"] = url
        sanitized_items.append(new_it)

    if dropped:
        cid = data.get("cluster_id") or data.get("topic_id") or "?"
        logger.info("sanitize: cluster %s dropped %d items with invalid URLs", cid, dropped)

    data["items"] = sanitized_items

    # Normalize canonical_links if present
    if "canonical_links" in data and isinstance(data["canonical_links"], list):
        fixed_links: list[str] = []
        for u in data["canonical_links"]:
            nu = normalize_http_url(u)
            if nu:
                fixed_links.append(nu)
        data["canonical_links"] = fixed_links

    return data


def _get_scoring_weights(config: dict) -> Dict[str, float]:
    weights = DEFAULT_SCORING_WEIGHTS.copy()
    custom = config.get("processing", {}).get("scoring_weights", {}) or {}
    for key, value in custom.items():
        if key in weights and value is not None:
            try:
                weights[key] = float(value)
            except (TypeError, ValueError):
                continue
    return weights


def _max_bullets(config: dict) -> int:
    try:
        value = int(config.get("processing", {}).get("max_bullets_per_topic", 4))
    except (TypeError, ValueError):
        value = 4
    return max(1, min(4, value))


STAGE1_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "ClusterFacts",
    "type": "object",
    "properties": {
        "cluster_id": {"type": "string"},
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact_id": {"type": "string"},
                    "text": {"type": "string"},
                    "url": {"type": "string", "format": "uri"},
                },
                "required": ["fact_id", "text", "url"],
            },
            "default": [],
        },
        "rejected": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "fact_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["reason"],
            },
            "default": [],
        },
    },
    "required": ["cluster_id"],
}


STAGE2_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "ClusterSelection",
    "type": "object",
    "properties": {
        "cluster_id": {"type": "string"},
        "picked": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact_id": {"type": "string"},
                    "text": {"type": "string"},
                    "url": {"type": "string", "format": "uri"},
                    "scores": {
                        "type": "object",
                        "properties": {
                            "actionability": {"type": "integer", "minimum": 0, "maximum": 3},
                            "novelty": {"type": "integer", "minimum": 0, "maximum": 2},
                            "impact": {"type": "integer", "minimum": 0, "maximum": 2},
                            "reusability": {"type": "integer", "minimum": 0, "maximum": 2},
                            "reliability": {"type": "integer", "minimum": 0, "maximum": 1},
                            "agentic_bonus": {"type": "integer", "minimum": 0, "maximum": 1, "default": 0},
                        },
                        "required": [
                            "actionability",
                            "novelty",
                            "impact",
                            "reusability",
                            "reliability",
                        ],
                    },
                    "strategic_flag": {"type": "boolean", "default": False},
                    "rationale": {"type": "string"},
                },
                "required": [
                    "fact_id",
                    "text",
                    "url",
                    "scores",
                    "strategic_flag",
                    "rationale",
                ],
            },
            "default": [],
        },
        "dropped": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["fact_id", "reason"],
            },
            "default": [],
        },
        "notes": {"type": "string"},
    },
    "required": ["cluster_id"],
}


STAGE3_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "TopicDraft",
    "type": "object",
    "properties": {
        "topic_id": {"type": "string"},
        "headline": {"type": "string"},
        "bullets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "url": {"type": "string", "format": "uri"},
                    "fact_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                },
                "required": ["text", "url", "fact_ids"],
            },
            "default": [],
            "maxItems": 4,
        },
        "annotations": {
            "type": "object",
            "properties": {
                "agentic": {"type": "boolean"},
                "strategic": {"type": "boolean"}
            },
            "additionalProperties": False,
            "default": {},
        },
        "notes": {"type": "string"},
    },
    "required": ["topic_id", "headline", "bullets"],
}


def _inject_url_enum(schema: Dict[str, Any], allowed_urls: list[str]) -> Dict[str, Any]:
    """Return a copy of schema with bullets/picked url restricted to enum.

    Works for stage2 (picked[].url) and stage3 (bullets[].url). If allowed_urls
    is empty, returns the original schema.
    """
    if not allowed_urls:
        return schema
    import copy as _copy
    s = _copy.deepcopy(schema)
    # Try stage2 shape first
    try:
        node = s["properties"]["picked"]["items"]["properties"]["url"]
        node["enum"] = allowed_urls
        return s
    except Exception:
        pass
    # Try stage3 shape
    try:
        node = s["properties"]["bullets"]["items"]["properties"]["url"]
        node["enum"] = allowed_urls
        return s
    except Exception:
        return schema


def _closest_url(u: str, allowed: list[str]) -> Optional[str]:
    if not u or not allowed:
        return None
    if u in allowed:
        return u
    low = u.lower()
    for cand in allowed:
        if cand.lower() == low:
            return cand
    for candidate in (u.replace("_", "-"), u.replace("-", "_")):
        if candidate in allowed:
            return candidate
        low_c = candidate.lower()
        for cand in allowed:
            if cand.lower() == low_c:
                return cand
    try:
        from difflib import get_close_matches
        match = get_close_matches(u, allowed, n=1, cutoff=0.98)
        if match:
            return match[0]
    except Exception:
        pass
    return None


def _render_template(path: Path, **context: Any) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    sys_template = ENV.from_string(data.get("system", ""))
    task_template = ENV.from_string(data.get("task", ""))
    rendered = f"{sys_template.render(**context)}\n\n{task_template.render(**context)}".strip()
    return rendered + "\n"


def _load_prompt_path(config: dict, stage_key: str) -> Path:
    multistage_cfg = config.get("multistage", {})
    stage_cfg = multistage_cfg.get(stage_key, {})
    prompt_path = (
        stage_cfg.get("prompt_file")
        or stage_cfg.get("prompt")
        or multistage_cfg.get("prompt_file")
        or multistage_cfg.get("prompt")
    )
    if prompt_path:
        return Path(prompt_path)
    return DEFAULT_PROMPTS[stage_key]


def _resolve_llm_settings(config: dict, stage_key: str) -> LLMSettings:
    summarization_cfg = config.get("summarization", {})
    multistage_cfg = config.get("multistage", {})
    stage_cfg = multistage_cfg.get(stage_key, {})

    def _lookup(key: str, default: Any = None) -> Any:
        for scope in (stage_cfg, multistage_cfg, summarization_cfg):
            if key in scope and scope[key] is not None:
                return scope[key]
        return default

    provider = str(_lookup("llm_provider", "gemini")).lower()

    if provider == "gemini":
        model = _lookup("gemini_model", _lookup("model", "gemini-3-flash-preview"))
    elif provider == "openai":
        model = _lookup("openai_model", _lookup("model", "gpt-4o-2024-08-06"))
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")

    temperature = float(_lookup("temperature", 0.2))
    timeout = int(_lookup("timeout", 600))
    retries = int(_lookup("retries", 0))

    provider_options = _lookup("provider_options", {}) or {}
    options = provider_options.get(provider)

    return LLMSettings(
        provider=provider,
        model=model,
        temperature=temperature,
        timeout=timeout,
        retries=retries,
        options=options,
    )


def run_stage1_extract(bundle: ClusterBundle, config: dict, *, briefing_title: str, artifact_dir: Optional[Path] = None) -> ClusterFacts:
    """Run Stage 1 prompt to extract verifiable facts from a bundle."""

    prompt_path = _load_prompt_path(config, "stage1")
    llm_settings = _resolve_llm_settings(config, "stage1")

    cluster_json = json.dumps(
        bundle.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
    )

    prompt = _render_template(
        prompt_path,
        cluster_json=cluster_json,
        cluster_id=bundle.cluster_id,
        briefing_title=briefing_title,
    )

    logger.info(
        "Stage1 extract: cluster=%s provider=%s model=%s",
        bundle.cluster_id,
        llm_settings.provider,
        llm_settings.model,
    )

    result = call_with_schema(
        provider=llm_settings.provider,
        prompt=prompt,
        model=llm_settings.model,
        schema=STAGE1_SCHEMA,
        temperature=llm_settings.temperature,
        timeout=llm_settings.timeout,
        retries=llm_settings.retries,
        options=llm_settings.options,
    )

    result.setdefault("cluster_id", bundle.cluster_id)
    result.setdefault("facts", [])
    result.setdefault("rejected", [])

    cluster_facts = ClusterFacts.model_validate(result)

    if artifact_dir:
        artifact_path = Path(artifact_dir) / f"{bundle.cluster_id}_stage1.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(cluster_facts.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    logger.info(
        "Stage1 extract complete: cluster=%s facts=%d rejected=%d",
        cluster_facts.cluster_id,
        len(cluster_facts.facts),
        len(cluster_facts.rejected),
    )

    return cluster_facts


def run_stage2_score(
    cluster_facts: ClusterFacts,
    config: dict,
    *,
    briefing_title: str,
    artifact_dir: Optional[Path] = None,
) -> ClusterSelection:
    """Run Stage 2 prompt to score and select facts."""

    prompt_path = _load_prompt_path(config, "stage2")
    llm_settings = _resolve_llm_settings(config, "stage2")

    cluster_facts_json = json.dumps(
        cluster_facts.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
    )

    prompt = _render_template(
        prompt_path,
        cluster_facts_json=cluster_facts_json,
        cluster_id=cluster_facts.cluster_id,
        briefing_title=briefing_title,
    )

    logger.info(
        "Stage2 score: cluster=%s provider=%s model=%s",
        cluster_facts.cluster_id,
        llm_settings.provider,
        llm_settings.model,
    )

    # Restrict urls to facts from Stage 1
    allowed_urls = [str(f.url) for f in cluster_facts.facts]
    s2_schema = _inject_url_enum(STAGE2_SCHEMA, allowed_urls)

    raw = call_with_schema(
        provider=llm_settings.provider,
        prompt=prompt,
        model=llm_settings.model,
        schema=s2_schema,
        temperature=llm_settings.temperature,
        timeout=llm_settings.timeout,
        retries=llm_settings.retries,
        options=llm_settings.options,
    )

    raw.setdefault("cluster_id", cluster_facts.cluster_id)
    raw.setdefault("picked", [])
    raw.setdefault("dropped", [])

    for fact in raw["picked"]:
        scores = fact.setdefault("scores", {})
        scores.setdefault("agentic_bonus", 0)
        fact.setdefault("strategic_flag", False)

    # Post-fix picked URLs to canonical ones by fact_id when available
    # This prevents mutated URLs from slipping through
    fact_by_id = {f.fact_id: str(f.url) for f in cluster_facts.facts}
    for picked in raw.get("picked", []) or []:
        fid = picked.get("fact_id")
        canonical = fact_by_id.get(fid)
        if canonical:
            if picked.get("url") != canonical:
                logger.warning(
                    "Stage2: URL corrected by fact_id: %s -> %s", picked.get("url"), canonical
                )
                picked["url"] = canonical

    cluster_selection = ClusterSelection.model_validate(raw)

    if artifact_dir:
        artifact_path = Path(artifact_dir) / f"{cluster_selection.cluster_id}_stage2.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(cluster_selection.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    logger.info(
        "Stage2 score complete: cluster=%s picked=%d dropped=%d",
        cluster_selection.cluster_id,
        len(cluster_selection.picked),
        len(cluster_selection.dropped),
    )

    return cluster_selection


def run_stage3_compose(
    cluster_selection: ClusterSelection,
    config: dict,
    *,
    briefing_title: str,
    artifact_dir: Optional[Path] = None,
) -> TopicDraft:
    """Run Stage 3 prompt to compose actionable bullets."""

    prompt_path = _load_prompt_path(config, "stage3")
    llm_settings = _resolve_llm_settings(config, "stage3")

    cluster_selection_json = json.dumps(
        cluster_selection.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
    )

    prompt = _render_template(
        prompt_path,
        cluster_selection_json=cluster_selection_json,
        cluster_id=cluster_selection.cluster_id,
        briefing_title=briefing_title,
    )

    logger.info(
        "Stage3 compose: cluster=%s provider=%s model=%s",
        cluster_selection.cluster_id,
        llm_settings.provider,
        llm_settings.model,
    )

    # Restrict bullet URLs to picked fact URLs
    picked_urls = [str(f.url) for f in cluster_selection.picked]
    s3_schema = _inject_url_enum(STAGE3_SCHEMA, picked_urls)

    raw = call_with_schema(
        provider=llm_settings.provider,
        prompt=prompt,
        model=llm_settings.model,
        schema=s3_schema,
        temperature=llm_settings.temperature,
        timeout=llm_settings.timeout,
        retries=llm_settings.retries,
        options=llm_settings.options,
    )

    raw.setdefault("topic_id", f"cluster-{cluster_selection.cluster_id}")
    raw.setdefault("bullets", [])
    raw.setdefault("annotations", {})

    for bullet in raw["bullets"]:
        bullet.setdefault("fact_ids", [])

    # Post-validate bullet URLs against picked facts; align by fact_ids when possible
    by_id = {f.fact_id: str(f.url) for f in cluster_selection.picked}
    allowed = [str(f.url) for f in cluster_selection.picked]
    for bullet in raw.get("bullets", []) or []:
        # Prefer mapping via fact_ids
        ids = bullet.get("fact_ids") or []
        mapped = None
        for fid in ids:
            if fid in by_id:
                mapped = by_id[fid]
                break
        current = bullet.get("url")
        target = mapped or _closest_url(current, allowed)
        if target and current != target:
            logger.warning("Stage3: URL corrected: %s -> %s", current, target)
            bullet["url"] = target

    topic_draft = TopicDraft.model_validate(raw)

    max_bullet_count = _max_bullets(config)
    if len(topic_draft.bullets) > max_bullet_count:
        topic_draft = topic_draft.model_copy(update={"bullets": topic_draft.bullets[:max_bullet_count]})

    if artifact_dir:
        artifact_path = Path(artifact_dir) / f"{cluster_selection.cluster_id}_stage3.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(topic_draft.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    logger.info(
        "Stage3 compose complete: cluster=%s bullets=%d",
        cluster_selection.cluster_id,
        len(topic_draft.bullets),
    )

    return topic_draft


def run_stage4_finalize(
    topics: list[TopicDraft],
    selections: Dict[str, ClusterSelection],
    bundles: Dict[str, ClusterBundle],
    config: dict,
    *,
    briefing_title: str,
    briefing_date: Optional[datetime] = None,
    artifact_dir: Optional[Path] = None,
) -> Briefing:
    """Deterministically order topics, apply annotations, and emit final briefing."""

    weights = _get_scoring_weights(config)
    max_bullets = _max_bullets(config)
    agentic_section_enabled = bool(config.get("processing", {}).get("agentic_section", True))

    def score_selection(selection: Optional[ClusterSelection]) -> tuple[float, float, int]:
        if not selection or not selection.picked:
            return 0.0, 0.0, 0
        weighted_totals: List[float] = []
        actionability_scores: List[int] = []
        for fact in selection.picked:
            weighted_totals.append(
                fact.scores.actionability * weights["actionability"]
                + fact.scores.novelty * weights["novelty"]
                + fact.scores.impact * weights["impact"]
                + fact.scores.reusability * weights["reusability"]
                + fact.scores.reliability * weights["reliability"]
                + fact.scores.agentic_bonus * weights["agentic_bonus"]
            )
            actionability_scores.append(fact.scores.actionability)
        return (
            max(weighted_totals) if weighted_totals else 0.0,
            max(actionability_scores) if actionability_scores else 0.0,
            len(selection.picked),
        )

    def primary_source(bundle: Optional[ClusterBundle]) -> Optional[str]:
        if not bundle:
            return None
        for item in bundle.items:
            if item.source:
                return item.source
        return None

    enriched: List[Dict[str, Any]] = []
    for topic_draft in topics:
        selection = _get_with_fallback(selections, topic_draft.topic_id)
        bundle = _get_with_fallback(bundles, topic_draft.topic_id)

        score, actionability_score, picked_count = score_selection(selection)
        normalized_id = _sanitize_topic_id(
            selection.cluster_id if selection else topic_draft.topic_id
        )

        sources: List[str] = []
        if bundle:
            seen_sources = set()
            for item in bundle.items:
                src = item.source or "unknown"
                if src not in seen_sources:
                    sources.append(src)
                    seen_sources.add(src)

        enriched.append(
            {
                "topic": topic_draft,
                "score": score,
                "actionability": actionability_score,
                "picked_count": picked_count,
                "has_agentic": topic_draft.annotations.get("agentic", False),
                "has_strategic": topic_draft.annotations.get("strategic", False),
                "topic_id": normalized_id,
                "primary_source": primary_source(bundle),
                "sources": sources,
            }
        )

    enriched.sort(
        key=lambda entry: (
            -entry["score"],
            -entry["actionability"],
            -entry["picked_count"],
        )
    )

    agentic_entries: List[Dict[str, Any]] = []
    general_entries: List[Dict[str, Any]] = []
    for entry in enriched:
        if agentic_section_enabled and entry["has_agentic"]:
            agentic_entries.append(entry)
        else:
            general_entries.append(entry)

    if agentic_section_enabled and agentic_entries and not general_entries:
        if len(agentic_entries) == 1:
            general_entries = agentic_entries
            agentic_entries = []
        else:
            general_entries.append(agentic_entries.pop(0))

    def reorder_for_diversity(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        pending = entries.copy()
        ordered: List[Dict[str, Any]] = []
        while pending:
            if len(ordered) < 3:
                used_sources = {
                    item["primary_source"] for item in ordered if item["primary_source"]
                }
                chosen_idx = None
                for idx, candidate in enumerate(pending):
                    src = candidate["primary_source"]
                    if not src or src not in used_sources:
                        chosen_idx = idx
                        break
                if chosen_idx is None:
                    chosen_idx = 0
            else:
                chosen_idx = 0
            ordered.append(pending.pop(chosen_idx))
        return ordered

    general_entries = reorder_for_diversity(general_entries)

    final_topics: List[Topic] = []

    if agentic_section_enabled and agentic_entries:
        agentic_bullets: List[Bullet] = []
        for entry in agentic_entries:
            for bullet in entry["topic"].bullets:
                if len(agentic_bullets) >= max_bullets:
                    break
                agentic_bullets.append(Bullet(text=bullet.text, url=str(bullet.url)))
            if len(agentic_bullets) >= max_bullets:
                break
        final_topics.append(
            Topic(
                topic_id="agentic-focus",
                headline="Agentic Focus",
                bullets=agentic_bullets,
            )
        )

    for entry in general_entries:
        topic_draft = entry["topic"]
        headline = topic_draft.headline.strip()
        if entry["has_strategic"] and not headline.startswith("【Strategic/Risk】"):
            headline = f"【Strategic/Risk】{headline}"

        bullets: List[Bullet] = []
        for bullet in topic_draft.bullets[:max_bullets]:
            bullets.append(Bullet(text=bullet.text, url=str(bullet.url)))

        final_topics.append(
            Topic(
                topic_id=entry["topic_id"],
                headline=headline,
                bullets=bullets,
            )
        )

    briefing_payload = {
        "title": briefing_title,
        "date": (briefing_date or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
        "topics": [topic.model_dump(mode="json") for topic in final_topics],
    }

    # Validate against final schema via Briefing model
    briefing = Briefing.model_validate(briefing_payload)

    if artifact_dir:
        artifact_path = Path(artifact_dir) / "stage4_briefing.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(briefing.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    logger.info("Stage4 finalize complete: topics=%d", len(briefing.topics))

    return briefing


def run_multistage_pipeline(
    bundles: list[Any],
    config: dict,
    *,
    briefing_id: Optional[str] = None,
    output_root: Optional[Path] = None,
) -> Tuple[Briefing, PipelineState]:
    """Execute the full multi-stage pipeline across all clusters."""

    briefing_title = config.get("briefing_title", "AI 简报")

    briefing_date_val = config.get("briefing_date")
    briefing_date: Optional[datetime]
    if isinstance(briefing_date_val, datetime):
        briefing_date = briefing_date_val
    elif isinstance(briefing_date_val, str):
        briefing_date = parse_datetime_safe(briefing_date_val)
    else:
        briefing_date = None

    artifact_root: Optional[Path] = None
    if output_root:
        base_path = Path(output_root)
        if briefing_id:
            base_path = base_path / briefing_id
        artifact_root = base_path / "stages"
        artifact_root.mkdir(parents=True, exist_ok=True)

    bundle_map: Dict[str, ClusterBundle] = {}
    facts_map: Dict[str, ClusterFacts] = {}
    selections_map: Dict[str, ClusterSelection] = {}
    topics_map: Dict[str, TopicDraft] = {}
    ordered_ids: list[str] = []

    for raw_bundle in bundles:
        try:
            candidate = raw_bundle
            if not isinstance(candidate, ClusterBundle):
                candidate = _sanitize_bundle_like(candidate)
            bundle = candidate if isinstance(candidate, ClusterBundle) else ClusterBundle.model_validate(candidate)
        except ValidationError as exc:
            logger.error("Invalid cluster bundle skipped: %s", exc)
            continue

        bundle_map[bundle.cluster_id] = bundle
        ordered_ids.append(bundle.cluster_id)
        cluster_dir = None
        if artifact_root:
            cluster_dir = artifact_root / _safe_dir_name(bundle.cluster_id)

        try:
            facts = run_stage1_extract(
                bundle,
                config,
                briefing_title=briefing_title,
                artifact_dir=cluster_dir,
            )
            facts_map[bundle.cluster_id] = facts

            selection = run_stage2_score(
                facts,
                config,
                briefing_title=briefing_title,
                artifact_dir=cluster_dir,
            )
            selections_map[bundle.cluster_id] = selection

            if not selection.picked:
                logger.info("Cluster %s skipped after scoring (no high-value facts)", bundle.cluster_id)
                continue

            topic = run_stage3_compose(
                selection,
                config,
                briefing_title=briefing_title,
                artifact_dir=cluster_dir,
            )

            if not topic.bullets:
                logger.info("Cluster %s skipped after composition (empty bullets)", bundle.cluster_id)
                continue

            topics_map[bundle.cluster_id] = topic
        except Exception as exc:  # noqa: BLE001
            logger.exception("Cluster %s failed in multi-stage pipeline", bundle.cluster_id)
            continue

    ordered_topics = [topics_map[cid] for cid in ordered_ids if cid in topics_map]

    briefing = run_stage4_finalize(
        ordered_topics,
        selections_map,
        bundle_map,
        config,
        briefing_title=briefing_title,
        briefing_date=briefing_date,
        artifact_dir=artifact_root,
    )

    state = PipelineState(
        bundles=bundle_map,
        facts=facts_map,
        selections=selections_map,
        topics=topics_map,
        artifact_root=artifact_root,
    )

    return briefing, state


def compute_metrics(state: PipelineState, briefing: Briefing, config: Optional[dict] = None) -> Dict[str, Any]:
    """Summarize core quality metrics from pipeline state."""

    config_dict = config or {}
    weights = _get_scoring_weights(config_dict)

    total_facts = sum(len(facts.facts) for facts in state.facts.values())
    picked_facts = sum(len(selection.picked) for selection in state.selections.values())
    dropped_facts = sum(len(selection.dropped) for selection in state.selections.values())

    actionability_scores = [fact.scores.actionability for selection in state.selections.values() for fact in selection.picked]
    avg_actionability = mean(actionability_scores) if actionability_scores else 0.0

    weighted_totals = [
        fact.scores.actionability * weights["actionability"]
        + fact.scores.novelty * weights["novelty"]
        + fact.scores.impact * weights["impact"]
        + fact.scores.reusability * weights["reusability"]
        + fact.scores.reliability * weights["reliability"]
        + fact.scores.agentic_bonus * weights["agentic_bonus"]
        for selection in state.selections.values()
        for fact in selection.picked
    ]

    agentic_topics = sum(1 for topic in state.topics.values() if topic.annotations.get("agentic"))
    strategic_topics = sum(1 for topic in state.topics.values() if topic.annotations.get("strategic"))

    return {
        "clusters_total": len(state.bundles),
        "topics_final": len(briefing.topics),
        "facts_total": total_facts,
        "facts_picked": picked_facts,
        "facts_dropped": dropped_facts,
        "kept_ratio": (picked_facts / total_facts) if total_facts else 0.0,
        "avg_actionability": avg_actionability,
        "avg_weighted_score": mean(weighted_totals) if weighted_totals else 0.0,
        "agentic_topics": agentic_topics,
        "strategic_topics": strategic_topics,
        "json_repair_rate": 0.0,
    }
