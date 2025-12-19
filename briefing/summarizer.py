import os
import json
import copy
import datetime as dt
from typing import List, Dict, Any, Optional, Tuple, Set

from briefing.utils import get_logger
from briefing.llm.registry import call_with_schema
from briefing.rendering.markdown import render_md

logger = get_logger(__name__)

GEMINI_MAX_URL_ENUM_ITEMS = 20
GEMINI_MAX_URL_ENUM_CHARS = 2000

def _mk_prompt(bundles: List[Dict[str, Any]], cfg: dict) -> str:
    """Render prompt from YAML template."""
    summ = cfg.get("summarization", {})
    prompt_file = summ.get("prompt_file")
    if not prompt_file:
        raise ValueError("summarization.prompt_file required")
    
    from briefing.rendering.prompt_loader import render_prompt
    title = cfg.get("briefing_title", "AI 简报")
    return render_prompt(title, bundles, prompt_file)

def generate_summary(bundles: List[Dict[str, Any]], 
                    config: dict) -> Tuple[Optional[str], Optional[Dict]]:
    """Generate summary using structured outputs."""
    if not bundles:
        logger.info("No bundles to summarize")
        return None, None
    
    # Load base schema
    schema_path = os.path.join(os.path.dirname(__file__), "schemas", "briefing.schema.json")
    with open(schema_path) as f:
        base_schema = json.load(f)

    # Build allowlists: per-topic and global
    allowed_by_topic = _collect_allowed_urls_by_topic(bundles)
    allowed_urls = sorted({u for urls in allowed_by_topic.values() for u in urls})
    
    # Get config
    summ = config.get("summarization", {})
    provider = summ.get("llm_provider", "gemini").lower()
    
    if provider == "gemini":
        model = summ.get("gemini_model", "gemini-3-flash-preview")
    elif provider == "openai":
        model = summ.get("openai_model", "gpt-4o-2024-08-06")
    else:
        raise ValueError(f"Unknown provider: {provider}")
    
    # Inject dynamic schema constraints based on provider capabilities
    if provider == "openai":
        runtime_schema = _inject_per_topic_url_enums(base_schema, allowed_by_topic)
    else:
        # Gemini may reject large schemas; only inject URL enums when compact.
        if _should_inject_gemini_url_enum(allowed_urls):
            runtime_schema = _inject_global_url_enum(base_schema, allowed_urls)
        else:
            runtime_schema = base_schema
    
    # Call LLM with schema
    obj = call_with_schema(
        provider=provider,
        prompt=_mk_prompt(bundles, config),
        model=model,
        schema=runtime_schema,
        temperature=float(summ.get("temperature", 0.2)),
        timeout=int(summ.get("timeout", 600)),
        retries=int(summ.get("retries", 0)),
        options=summ.get("provider_options", {}).get(provider)
    )
    
    # Check if empty
    if not obj.get("topics"):
        logger.info("Empty topics")
        return None, None
    
    # Validate and correct URLs as a final safety net (per-topic)
    if allowed_urls:
        obj = _validate_urls(obj, allowed_by_topic, set(allowed_urls))

    # Set metadata
    obj["title"] = config.get("briefing_title", obj.get("title", "AI Briefing"))
    obj["date"] = obj.get("date", dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z'))
    
    # Extract rendering config from main config
    rendering_config = config.get("rendering", {})
    
    # Render
    md = render_md(obj, rendering_config)
    logger.info("Generated %d topics", len(obj["topics"]))
    
    return md, obj


def _collect_allowed_urls_by_topic(bundles: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Collect source URLs per topic_id from input bundles."""
    out: Dict[str, Set[str]] = {}
    for bundle in bundles or []:
        tid = str(bundle.get("topic_id") or "").strip() or ""
        if not tid:
            continue
        urls: Set[str] = out.setdefault(tid, set())
        for item in (bundle.get("items") or []):
            url = str(item.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                urls.add(url)
    # sort for determinism
    return {k: sorted(v) for k, v in out.items()}


def _should_inject_gemini_url_enum(allowed_urls: List[str]) -> bool:
    """Avoid oversized enums that Gemini may reject for schema complexity."""
    if not allowed_urls:
        return False
    if len(allowed_urls) > GEMINI_MAX_URL_ENUM_ITEMS:
        return False
    total_chars = sum(len(url) for url in allowed_urls)
    return total_chars <= GEMINI_MAX_URL_ENUM_CHARS


def _inject_per_topic_url_enums(schema: dict, allowed_by_topic: Dict[str, List[str]]) -> dict:
    """Inject conditional enums: if topic_id==X then bullets[].url ∈ urls(X).

    Note: Some providers may ignore conditional constructs; post-validation
    still enforces per-topic constraints.
    """
    if not allowed_by_topic:
        return schema

    s = copy.deepcopy(schema)
    try:
        topic_item = s["properties"]["topics"]["items"]
        all_of = topic_item.setdefault("allOf", [])
        for topic_id, urls in allowed_by_topic.items():
            if not urls:
                continue
            all_of.append({
                "if": {
                    "properties": {
                        "topic_id": {"const": topic_id}
                    },
                    "required": ["topic_id"]
                },
                "then": {
                    "properties": {
                        "bullets": {
                            "items": {
                                "properties": {
                                    "url": {"enum": urls}
                                }
                            }
                        }
                    }
                }
            })
        return s
    except Exception:
        return schema


def _inject_global_url_enum(schema: dict, allowed_urls: List[str]) -> dict:
    """Inject a global enum for bullets[].url.

    Safer for providers that might not support conditional schemas.
    """
    if not allowed_urls:
        return schema
    s = copy.deepcopy(schema)
    try:
        url_node = (
            s["properties"]["topics"]["items"]["properties"]["bullets"]["items"]["properties"]["url"]
        )
        url_node["enum"] = allowed_urls
        return s
    except Exception:
        return schema


def _validate_urls(obj: dict, allowed_by_topic: Dict[str, List[str]], global_allowed: Set[str]) -> dict:
    """Ensure every bullet.url stays within the per-topic allowlist.

    Attempts safe correction within the topic's allowed set; falls back to
    global set only for case-insensitive or micro mutations.
    """
    if not isinstance(obj, dict) or not global_allowed:
        return obj

    def _closest(u: str, allowed: Set[str]) -> Optional[str]:
        if not u:
            return None
        if u in allowed:
            return u
        low = u.lower()
        for cand in allowed:
            if cand.lower() == low:
                return cand
        # Try underscore/hyphen swap patterns
        for candidate in (u.replace("_", "-"), u.replace("-", "_")):
            if candidate in allowed:
                return candidate
            low_c = candidate.lower()
            for cand in allowed:
                if cand.lower() == low_c:
                    return cand
        # Fuzzy match (strict)
        try:
            from difflib import get_close_matches
            match = get_close_matches(u, list(allowed), n=1, cutoff=0.98)
            if match:
                return match[0]
        except Exception:
            pass
        return None

    topics = obj.get("topics") or []
    for topic in topics:
        bullets = topic.get("bullets") or []
        tid = str(topic.get("topic_id") or "").strip()
        topic_allowed_list = allowed_by_topic.get(tid) or []
        topic_allowed = set(topic_allowed_list) if topic_allowed_list else global_allowed
        for bullet in bullets:
            url = str(bullet.get("url") or "").strip()
            if url and url not in topic_allowed:
                fixed = _closest(url, topic_allowed)
                if fixed and fixed != url:
                    logger.warning("URL corrected: %s -> %s", url, fixed)
                    bullet["url"] = fixed
    return obj
