"""Channel publishers for AI-Briefing outputs."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from html import escape as html_escape
from pathlib import Path
from typing import Iterable, Optional

import mistune

from briefing.net import retry_session
from briefing.utils import get_logger, redact_secrets

logger = get_logger(__name__)

ALLOWED_SCHEMES = ("http", "https", "mailto", "tg")
TELEGRAM_LIMIT = 4096


def _sanitize_url(url: str) -> str:
    if not url:
        return ""
    value = url.strip()
    if ":" in value:
        scheme = value.split(":", 1)[0].lower()
        if scheme not in ALLOWED_SCHEMES:
            return ""
    return value


class _TelegramHTMLRenderer(mistune.HTMLRenderer):
    def text(self, text: str) -> str:  # type: ignore[override]
        return html_escape(text)

    def emphasis(self, text: str) -> str:  # type: ignore[override]
        return f"<i>{text}</i>"

    def strong(self, text: str) -> str:  # type: ignore[override]
        return f"<b>{text}</b>"

    def link(self, text: str, url: str, title: Optional[str] = None) -> str:  # type: ignore[override]
        safe_url = _sanitize_url(url)
        if not safe_url:
            return text
        return f'<a href="{html_escape(safe_url, quote=True)}">{text or html_escape(safe_url)}</a>'

    def image(self, src: str, alt: str = "", title: Optional[str] = None) -> str:  # type: ignore[override]
        safe_url = _sanitize_url(src)
        if not safe_url:
            return ""
        label = alt or "image"
        return f'🖼️ <a href="{html_escape(safe_url, quote=True)}">{html_escape(label)}</a>'

    def codespan(self, text: str) -> str:  # type: ignore[override]
        return f"<code>{html_escape(text)}</code>"

    def paragraph(self, text: str) -> str:  # type: ignore[override]
        return text + "\n\n"

    def heading(self, text: str, level: int) -> str:  # type: ignore[override]
        return f"<b>{text}</b>\n\n"

    def list(self, text: str, ordered: bool, **attrs) -> str:  # type: ignore[override]
        start = int(attrs.get("start") or 1)
        items = [line for line in text.strip("\n").split("\n") if line]
        bullets = []
        for index, item in enumerate(items, start=start):
            bullet = f"{index}. " if ordered else "• "
            bullets.append(bullet + item.strip())
        return "\n".join(bullets) + "\n\n"

    def list_item(self, text: str) -> str:  # type: ignore[override]
        return text.strip() + "\n"

    def block_quote(self, text: str) -> str:  # type: ignore[override]
        return f"<blockquote>{text.strip()}</blockquote>\n\n"

    def block_code(self, code: str, info: Optional[str] = None) -> str:  # type: ignore[override]
        language = (info or "").split()[0] if info else ""
        escaped = html_escape(code)
        if language:
            return f'<pre><code class="language-{html_escape(language)}">{escaped}</code></pre>\n'
        return f"<pre>{escaped}</pre>\n"

    def thematic_break(self) -> str:  # type: ignore[override]
        return "────────\n"


def md_to_tg_html(markdown_text: str) -> str:
    normalized = (markdown_text or "").replace("\r\n", "\n").strip()
    parser = mistune.create_markdown(
        renderer=_TelegramHTMLRenderer(),
        plugins=["strikethrough", "task_lists"],
    )
    html = parser(normalized)
    html = re.sub(r"</?(?:p|ul|ol|li|hr|table|thead|tbody|tr|th|td|div)>", "", html, flags=re.IGNORECASE)
    html = html.replace("&nbsp;", " ")
    return html.strip()


def split_html_for_telegram(html: str, limit: int = TELEGRAM_LIMIT, headroom: int = 0) -> list[str]:
    text = html or ""
    max_len = max(1, min(limit, TELEGRAM_LIMIT) - max(0, headroom))
    boundary_tokens = (
        ("\n\n", 0),
        ("</pre>", len("</pre>")),
        ("</blockquote>", len("</blockquote>")),
    )
    parts: list[str] = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        window = text[:max_len]
        cut = -1
        include = 0
        for token, token_len in boundary_tokens:
            idx = window.rfind(token)
            if idx > cut:
                cut = idx
                include = token_len
        if cut == -1 or cut < int(max_len * 0.7):
            cut = max_len
            include = 0
        else:
            cut += include
        if cut <= 0:
            cut = max_len
        parts.append(text[:cut])
        text = text[cut:].lstrip()
    return parts


@dataclass
class TelegramConfig:
    chat_id: str
    bot_token: str
    parse_mode: Optional[str] = "HTML"
    link_preview_disabled: bool = True
    chunk_limit: int = TELEGRAM_LIMIT
    timeout_sec: float = 30.0
    retries: int = 3


class TelegramPublisher:
    def __init__(self, cfg: TelegramConfig):
        self.cfg = cfg
        self.session = retry_session(total=cfg.retries)

    def send_markdown(self, markdown_text: str) -> None:
        if not (self.cfg.chat_id and self.cfg.bot_token):
            raise RuntimeError("telegram: chat_id or bot_token missing")

        text = markdown_text or ""
        if self.cfg.parse_mode == "HTML":
            text = md_to_tg_html(text)
        chunks = split_html_for_telegram(text, min(self.cfg.chunk_limit, TELEGRAM_LIMIT))
        url = f"https://api.telegram.org/bot{self.cfg.bot_token}/sendMessage"

        for chunk in chunks:
            payload = {"chat_id": self.cfg.chat_id, "text": chunk}
            if self.cfg.parse_mode and self.cfg.parse_mode != "None":
                payload["parse_mode"] = self.cfg.parse_mode
            payload["link_preview_options"] = {"is_disabled": self.cfg.link_preview_disabled}
            response = self.session.post(url, json=payload, timeout=self.cfg.timeout_sec)
            if response.status_code != 200:
                logger.error(
                    "telegram_send failed status=%s body=%s",
                    response.status_code,
                    redact_secrets(response.text),
                )
                raise RuntimeError(f"Telegram send failed: {response.text[:256]}")
        logger.info("telegram_send success parts=%d", len(chunks))


def maybe_publish_telegram(markdown_text: str, output_cfg: dict) -> None:
    tg = (output_cfg or {}).get("telegram") or {}
    if not tg.get("enabled"):
        return

    token = tg.get("bot_token") or os.getenv(tg.get("bot_token_env", "TELEGRAM_BOT_TOKEN"), "")
    cfg = TelegramConfig(
        chat_id=tg.get("chat_id", ""),
        bot_token=token,
        parse_mode=(tg.get("parse_mode") if "parse_mode" in tg else "HTML") or None,
        link_preview_disabled=bool(tg.get("disable_link_preview", True)),
        chunk_limit=int(tg.get("chunk_size", TELEGRAM_LIMIT)),
        timeout_sec=float(tg.get("timeout_sec", 30.0)),
        retries=int(tg.get("retries", 3)),
    )

    if not (cfg.chat_id and cfg.bot_token):
        logger.warning("telegram not configured: chat_id or token missing")
        return

    TelegramPublisher(cfg).send_markdown(markdown_text)


@dataclass
class GitHubArtifactStoreConfig:
    repo: str
    token: str
    branch: str = "main"
    commit_prefix: str = "briefing"
    committer_name: str = "ai-briefing"
    committer_email: str = "noreply@example.com"
    retries: int = 3
    timeout_sec: float = 30.0


class GitHubArtifactStore:
    def __init__(self, cfg: GitHubArtifactStoreConfig):
        self.cfg = cfg
        self.session = retry_session(total=cfg.retries)
        self.headers = {
            "Authorization": f"token {cfg.token}",
            "Accept": "application/vnd.github.v3+json",
        }

    def _contents_api(self, path: str) -> str:
        target = path.lstrip("/")
        return f"https://api.github.com/repos/{self.cfg.repo}/contents/{target}"

    def upload(self, local_path: Path, dest_path: str, commit_message: str) -> None:
        if not (self.cfg.token and self.cfg.repo):
            raise RuntimeError("github: token or repo missing")

        url = self._contents_api(dest_path)
        params = {"ref": self.cfg.branch}
        response = self.session.get(url, headers=self.headers, params=params, timeout=self.cfg.timeout_sec)
        existing_sha = response.json().get("sha") if response.status_code == 200 else None

        data = local_path.read_bytes()
        encoded = __import__("base64").b64encode(data).decode("utf-8")
        payload = {
            "message": commit_message,
            "content": encoded,
            "branch": self.cfg.branch,
            "committer": {
                "name": self.cfg.committer_name,
                "email": self.cfg.committer_email,
            },
        }
        if existing_sha:
            payload["sha"] = existing_sha

        put = self.session.put(url, headers=self.headers, json=payload, timeout=self.cfg.timeout_sec)
        if put.status_code not in (200, 201):
            logger.error(
                "github upload failed status=%s body=%s",
                put.status_code,
                redact_secrets(put.text),
            )
            raise RuntimeError(f"github put failed: {put.status_code} {put.text[:256]}")


def maybe_briefing_archive(generated_files: Iterable[str], output_cfg: dict, briefing_id: str, run_id: str) -> None:
    archive_cfg = (output_cfg or {}).get("briefing_archive") or {}
    if not archive_cfg.get("enabled"):
        return

    provider = (archive_cfg.get("provider") or "github").lower()
    if provider != "github":
        logger.error("briefing_archive: unsupported provider %s", provider)
        return

    token = archive_cfg.get("token") or os.getenv(archive_cfg.get("token_env", "GITHUB_TOKEN"), "")
    repo = archive_cfg.get("repo") or archive_cfg.get("repo_url", "")
    cfg = GitHubArtifactStoreConfig(
        repo=repo,
        token=token,
        branch=archive_cfg.get("branch", "main"),
        commit_prefix=archive_cfg.get("commit_message_prefix", "briefing"),
        committer_name=archive_cfg.get("committer_name", "ai-briefing"),
        committer_email=archive_cfg.get("committer_email", "noreply@example.com"),
        retries=int(archive_cfg.get("retries", 3)),
        timeout_sec=float(archive_cfg.get("timeout_sec", 30.0)),
    )

    if not (cfg.token and cfg.repo):
        logger.error("briefing_archive: missing token or repo")
        return

    store = GitHubArtifactStore(cfg)
    now = __import__("datetime").datetime.now()
    files = [Path(p) for p in (generated_files or [])]
    if not files:
        return

    success = 0
    for path in files:
        if not path.exists():
            logger.warning("briefing_archive: file not found %s", path)
            continue
        destination = f"{now:%Y}/{now:%m}/{briefing_id}/{path.name}"
        message = f"{cfg.commit_prefix}: {briefing_id} {path.name} run={run_id}"
        try:
            store.upload(path, destination, message)
        except Exception as exc:  # pragma: no cover - network errors are logged
            logger.error("briefing_archive: failed upload %s error=%s", path.name, exc)
        else:
            success += 1
            logger.info("briefing_archive: uploaded %s", path.name)

    logger.info("briefing_archive: uploaded %d/%d files", success, len(files))


@dataclass
class EmailConfig:
    smtp_host: str
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_addr: str = ""
    to_addrs: list[str] = None  # type: ignore[assignment]
    subject_prefix: str = "AI Briefing"
    use_tls: bool = True
    timeout_sec: float = 30.0

    def __post_init__(self):
        if self.to_addrs is None:
            self.to_addrs = []


class EmailPublisher:
    def __init__(self, cfg: EmailConfig):
        self.cfg = cfg

    def send_markdown(self, markdown_text: str, subject_suffix: str = "") -> None:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        if not (self.cfg.smtp_host and self.cfg.to_addrs):
            raise RuntimeError("email: smtp_host or to_addrs missing")

        # Convert markdown to HTML for email body
        html_body = md_to_tg_html(markdown_text)
        html_email = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 680px; margin: 0 auto; padding: 20px; line-height: 1.6;">
{html_body}
</body></html>"""

        subject = self.cfg.subject_prefix
        if subject_suffix:
            subject = f"{subject} — {subject_suffix}"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.cfg.from_addr or self.cfg.smtp_user
        msg["To"] = ", ".join(self.cfg.to_addrs)

        msg.attach(MIMEText(markdown_text, "plain", "utf-8"))
        msg.attach(MIMEText(html_email, "html", "utf-8"))

        try:
            with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=self.cfg.timeout_sec) as server:
                if self.cfg.use_tls:
                    server.starttls()
                if self.cfg.smtp_user and self.cfg.smtp_password:
                    server.login(self.cfg.smtp_user, self.cfg.smtp_password)
                server.sendmail(
                    msg["From"],
                    self.cfg.to_addrs,
                    msg.as_string(),
                )
            logger.info("email sent to %d recipients", len(self.cfg.to_addrs))
        except Exception as exc:
            logger.error("email send failed: %s", exc)
            raise


def maybe_publish_email(markdown_text: str, output_cfg: dict, briefing_title: str = "") -> None:
    email_cfg = (output_cfg or {}).get("email") or {}
    if not email_cfg.get("enabled"):
        return

    password = email_cfg.get("smtp_password") or os.getenv(
        email_cfg.get("smtp_password_env", "SMTP_PASSWORD"), ""
    )
    cfg = EmailConfig(
        smtp_host=email_cfg.get("smtp_host", ""),
        smtp_port=int(email_cfg.get("smtp_port", 587)),
        smtp_user=email_cfg.get("smtp_user", ""),
        smtp_password=password,
        from_addr=email_cfg.get("from_addr", ""),
        to_addrs=email_cfg.get("to_addrs", []),
        subject_prefix=email_cfg.get("subject_prefix", "AI Briefing"),
        use_tls=bool(email_cfg.get("use_tls", True)),
        timeout_sec=float(email_cfg.get("timeout_sec", 30.0)),
    )

    if not (cfg.smtp_host and cfg.to_addrs):
        logger.warning("email not configured: smtp_host or to_addrs missing")
        return

    EmailPublisher(cfg).send_markdown(markdown_text, subject_suffix=briefing_title)


def _run_safe(cmd_args, cwd=None, env=None):
    """Execute git commands safely with whitelist validation (legacy helper)."""
    allowed = [
        "init",
        "config",
        "remote",
        "add",
        "set-url",
        "checkout",
        "commit",
        "push",
        "status",
        "rev-parse",
        "log",
        "diff",
    ]

    if not cmd_args or len(cmd_args) < 2:
        raise ValueError("Invalid command format")
    if cmd_args[0] != "git":
        raise ValueError(f"Command not allowed: only git commands are permitted, got: {cmd_args[0]}")
    if cmd_args[1] not in allowed:
        raise ValueError(f"Git subcommand '{cmd_args[1]}' not allowed")

    safe_cmd = [part if "x-access-token" not in part else "***" for part in cmd_args]
    logger.info("git$ %s", " ".join(safe_cmd))

    completed = subprocess.run(
        cmd_args,
        shell=False,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        logger.error(
            "git failed: rc=%d stdout=%s stderr=%s",
            completed.returncode,
            redact_secrets(completed.stdout),
            redact_secrets(completed.stderr),
        )
        raise RuntimeError(f"git error: {redact_secrets(completed.stderr)}")
    return completed.stdout.strip()
