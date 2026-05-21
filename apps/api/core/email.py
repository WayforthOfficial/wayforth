"""Template-based transactional email service using Resend."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger("wayforth")

_TEMPLATE_DIR = Path(__file__).parent.parent / "emails"
_FROM = "Wayforth <hello@wayforth.io>"
_REPLY_TO = "support@wayforth.io"

_SUBJECTS: dict[str, str] = {
    "welcome": "Welcome to Wayforth",
    "subscription_confirmed": "Wayforth {{plan_name}} — confirmed",
    "low_credits": "Your Wayforth credits are running low",
    "founding_member": "500 founding member credits added",
}

# Rendered HTML inserted into welcome.html for founding member accounts
_FOUNDING_NOTE_HTML = """
<div style="background:#1E293B;border-radius:10px;padding:20px;margin:0 0 20px;border-left:4px solid #8B5CF6">
  <p style="margin:0 0 4px;font-size:12px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:#8B5CF6">FOUNDING MEMBER</p>
  <p style="margin:0;font-size:14px;color:#E2E8F0">
    You joined before August 31, 2026 and qualify for a <strong>500-credit founding member bonus</strong>.
    It's credited automatically on your first paid invoice — no action needed.
  </p>
</div>
"""

# Rendered HTML inserted into low_credits.html for free-tier accounts
_UPGRADE_CTA_HTML = """
<div style="text-align:center;margin:0 0 24px">
  <a href="https://wayforth.io/billing"
     style="display:inline-block;background:#10B981;color:#fff;padding:13px 32px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px">
    Upgrade your plan &rarr;
  </a>
  <p style="margin:8px 0 0;font-size:12px;color:#64748B">Plans from $12/mo · cancel anytime</p>
</div>
"""


def _load_template(name: str) -> str:
    return (_TEMPLATE_DIR / f"{name}.html").read_text(encoding="utf-8")


def _render(text: str, data: dict[str, str]) -> str:
    for key, value in data.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def _build_founding_note(is_founding: bool) -> str:
    return _FOUNDING_NOTE_HTML if is_founding else ""


def _build_upgrade_cta(tier: str) -> str:
    return _UPGRADE_CTA_HTML if tier == "free" else ""


async def send_email(to: str, template: str, data: dict[str, str]) -> str:
    """
    Load `<template>.html` from apps/api/emails/, substitute {{variable}} placeholders,
    and send via Resend. Returns the Resend message_id, or "" when RESEND_API_KEY is unset.
    Raises on Resend API failure (caller should suppress in background tasks).
    """
    api_key = os.getenv("RESEND_API_KEY", "")
    if not api_key:
        logger.debug("RESEND_API_KEY unset — skipping %s email to %s", template, to)
        return ""

    html = _render(_load_template(template), data)
    subject = _render(_SUBJECTS.get(template, "Wayforth"), data)

    import resend as _resend  # imported lazily so tests can patch before import

    _resend.api_key = api_key

    def _send() -> dict:
        return _resend.Emails.send({
            "from": _FROM,
            "reply_to": _REPLY_TO,
            "to": to,
            "subject": subject,
            "html": html,
        })

    result = await asyncio.to_thread(_send)
    message_id: str = result.get("id", "") if isinstance(result, dict) else ""
    logger.info("email sent template=%s to=%s id=%s", template, to, message_id)
    return message_id
