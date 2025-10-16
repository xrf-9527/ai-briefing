
import os
import re
import json
import html2text
import requests
import datetime as dt
import logging
from logging.handlers import TimedRotatingFileHandler
from jsonschema import validate, Draft202012Validator
from jsonschema.exceptions import ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from email.utils import parsedate_to_datetime
from typing import Optional, Any
from pydantic import TypeAdapter, HttpUrl

# ---------- Time helpers ----------

def now_utc():
    return dt.datetime.now(dt.timezone.utc)

def parse_datetime_safe(raw: str) -> Optional[dt.datetime]:
    """Best-effort parsing for timestamps from upstream feeds.

    Returns a timezone-aware UTC datetime on success, otherwise ``None``.
    """

    if not raw:
        return None

    raw = raw.strip()

    # Fast path: ISO 8601 (with optional trailing Z and fractional seconds)
    iso_candidate = raw
    if raw.endswith("Z"):
        iso_candidate = raw[:-1] + "+00:00"

    try:
        dt_obj = dt.datetime.fromisoformat(iso_candidate)
        if dt_obj.tzinfo is None:
            dt_obj = dt_obj.replace(tzinfo=dt.timezone.utc)
        return dt_obj.astimezone(dt.timezone.utc)
    except ValueError:
        pass

    # RFC 2822 / email style timestamps
    try:
        dt_obj = parsedate_to_datetime(raw)
        if dt_obj:
            if dt_obj.tzinfo is None:
                dt_obj = dt_obj.replace(tzinfo=dt.timezone.utc)
            return dt_obj.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        pass

    # Legacy fallbacks for custom formats (with timezone)
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            dt_obj = dt.datetime.strptime(raw, fmt)
            return dt_obj.astimezone(dt.timezone.utc)
        except Exception:
            continue

    return None

# ---------- Text helpers ----------

def clean_text(html_or_text: str) -> str:
    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = True
    h.body_width = 0
    text = h.handle(html_or_text or "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# ---------- URL helpers ----------

_HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)

def normalize_http_url(value: Any) -> Optional[str]:
    """Try to normalize a value into a valid http(s) URL string.

    - Trims whitespace
    - Adds scheme when missing (defaults to https://)
    - Supports protocol-relative form (//example.com)
    Returns normalized string on success; otherwise None.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.startswith("//"):
        s = "https:" + s
    # If no scheme present, assume https
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", s):
        s = "https://" + s
    try:
        _HTTP_URL_ADAPTER.validate_python(s)
        return s
    except Exception:
        return None

# ---------- Config validation ----------

def load_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def validate_config(cfg: dict):
    here = os.path.dirname(os.path.abspath(__file__))
    schema_path = os.path.join(here, "schemas", "config.schema.json")
    schema = json.loads(load_file(schema_path))
    try:
        validate(instance=cfg, schema=schema, cls=Draft202012Validator)
    except ValidationError as e:
        raise ValueError(f"Config validation error: {e.message} at {list(e.path)}") from e

# ---------- Output writer ----------

def write_output(human_md: str, json_obj: dict, out_cfg: dict):
    out_dir = out_cfg["dir"]
    formats = out_cfg["formats"]
    os.makedirs(out_dir, exist_ok=True)
    now_local = dt.datetime.now().astimezone()
    ts = now_local.strftime("%Y%m%dT%H%M%S%z")
    base = os.path.join(out_dir, f"briefing_{ts}")
    
    generated_files = []

    if "md" in formats:
        md_path = base + ".md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(human_md)
        generated_files.append(md_path)

    if "json" in formats:
        json_path = base + ".json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_obj, f, ensure_ascii=False, indent=2)
        generated_files.append(json_path)

    if "html" in formats:
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{json_obj.get('title','Briefing')}</title></head>
<body><pre>
{human_md}
</pre></body></html>"""
        html_path = base + ".html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        generated_files.append(html_path)
    
    return generated_files

# ---------- Logging ----------

_LOGGER_INITIALIZED = False

class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": dt.datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)

def _build_logger():
    global _LOGGER_INITIALIZED
    if _LOGGER_INITIALIZED:
        return

    level = os.getenv("LOG_LEVEL", "INFO").upper()
    # Default to a local, writable logs directory
    log_dir = os.getenv("LOG_DIR", "logs")
    json_mode = os.getenv("LOG_JSON", "false").lower() == "true"

    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, level, logging.INFO))

    ch = logging.StreamHandler()
    ch.setLevel(logger.level)

    fh = TimedRotatingFileHandler(os.path.join(log_dir, "ai-briefing.log"), when="D", backupCount=7, encoding="utf-8")
    fh.setLevel(logger.level)

    if json_mode:
        fmt = JsonFormatter()
    else:
        fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    ch.setFormatter(fmt)
    fh.setFormatter(fmt)
    logger.addHandler(ch)
    logger.addHandler(fh)

    _LOGGER_INITIALIZED = True

def get_logger(name: str = None) -> logging.Logger:
    _build_logger()
    return logging.getLogger(name if name else __name__)

# ---------- Secret redaction ----------

def redact_secrets(s: str) -> str:
    """Redact sensitive information from strings for safe logging."""
    if not s:
        return s
    
    # Simple approach: redact known env vars
    env_keys = ["GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "GITHUB_TOKEN", 
                "REDDIT_CLIENT_SECRET", "TWITTER_PASSWORD"]
    
    redacted = s
    for k in env_keys:
        v = os.getenv(k)
        if v and len(v) > 3:
            redacted = redacted.replace(v, "***")
    
    # Simple regex patterns for common cases
    import re
    pattern_flags = re.IGNORECASE
    redacted = re.sub(r"x-access-token:[^@]+@", "x-access-token:***@", redacted, flags=pattern_flags)
    redacted = re.sub(r"ghp_[A-Za-z0-9]{20,}", "ghp_***", redacted, flags=pattern_flags)
    redacted = re.sub(r"sk-[A-Za-z0-9-]{10,}", "sk-***", redacted, flags=pattern_flags)
    redacted = re.sub(r"(api_key=)([^\s&]+)", r"\1***", redacted, flags=pattern_flags)
    redacted = re.sub(r"(password=)([^\s&]+)", r"\1***", redacted, flags=pattern_flags)
    redacted = re.sub(r"(token=)([^\s&]+)", r"\1***", redacted, flags=pattern_flags)
    redacted = re.sub(r"(bearer\s+)[A-Za-z0-9._-]+", r"\1***", redacted, flags=pattern_flags)

    return redacted

# ---------- Service health wait ----------

@retry(stop=stop_after_attempt(10), wait=wait_exponential(multiplier=1, min=1, max=10),
       retry=retry_if_exception_type((requests.RequestException, AssertionError)))
def wait_for_service(url: str, expect_status: int = 200, timeout: float = 5.0):
    r = requests.get(url, timeout=timeout)
    assert r.status_code == expect_status
    return True
