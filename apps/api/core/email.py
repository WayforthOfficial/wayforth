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

# Plain-text equivalents of the HTML block variables above.
# send_email() swaps these in automatically when rendering the text version
# so callers never need to pass separate text/HTML pairs.
_FOUNDING_NOTE_TEXT = (
    "FOUNDING MEMBER: You joined before August 31, 2026 and qualify for a "
    "500-credit founding member bonus. It is credited automatically on your "
    "first paid invoice — no action needed.\n"
)
_UPGRADE_CTA_TEXT = (
    "Upgrade your plan: https://wayforth.io/billing\n"
    "Plans from $12/mo · cancel anytime\n"
)

_HTML_TO_TEXT: dict[str, str] = {
    _FOUNDING_NOTE_HTML: _FOUNDING_NOTE_TEXT,
    _UPGRADE_CTA_HTML: _UPGRADE_CTA_TEXT,
}

# Plain-text body per template. Uses the same {{variable}} substitution as HTML.
_PLAIN_TEXTS: dict[str, str] = {
    "welcome": """\
Welcome to Wayforth.

You're on the free tier with {{credits}} credits to get started.

YOUR BALANCE: {{credits}} credits
Free tier · resets monthly

QUICK START:
  {{quick_start}}

{{founding_note}}
Read the quickstart guide: https://wayforth.io/quickstart

---
Wayforth Technologies Inc. · wayforth.io · support@wayforth.io
""",
    "subscription_confirmed": """\
Wayforth {{plan_name}} plan confirmed.

SUBSCRIPTION ACTIVE
Plan:    {{plan_name}}
Amount:  {{amount}} · renews {{renewal_date}}

CREDITS ADDED: +{{credits_added}}
Credits reset monthly on your renewal date.

NEXT RENEWAL: {{renewal_date}}

View your dashboard: https://wayforth.io/dashboard

---
Wayforth Technologies Inc. · wayforth.io · support@wayforth.io
""",
    "low_credits": """\
Your Wayforth credits are running low.

CREDITS RUNNING LOW
{{credits_remaining}} credits left — {{percent_remaining}} remaining · resets {{renewal_date}}

Your Wayforth credits are at {{percent_remaining}}. Once exhausted, API calls
will return 402 until your next renewal or top-up.

{{upgrade_cta}}
View usage and top up: https://wayforth.io/dashboard

---
Wayforth Technologies Inc. · wayforth.io · support@wayforth.io
""",
    "founding_member": """\
Founding member bonus: +{{bonus_credits}} credits added.

FOUNDING MEMBER BONUS
+{{bonus_credits}} credits · one-time bonus

Thank you for being an early Wayforth supporter.

You joined before {{cutoff_date}} and qualify for our founding member bonus.
{{bonus_credits}} credits have been added to your balance on your first paid
invoice — no action needed.

This is a one-time bonus. It does not affect your monthly credit renewal.

View your balance: https://wayforth.io/dashboard

---
Wayforth Technologies Inc. · wayforth.io · support@wayforth.io
""",
}

# Sent with every transactional email to help Gmail route to Primary, not Promotions.
_TRANSACTIONAL_HEADERS: dict[str, str] = {
    "X-Priority": "1",
    "Precedence": "transactional",
    "X-Mailer": "Wayforth",
}


def _load_template(name: str) -> str:
    return (_TEMPLATE_DIR / f"{name}.html").read_text(encoding="utf-8")


# Variable names whose values are *pre-rendered HTML blocks* (built by
# _build_founding_note / _build_upgrade_cta) and must be inserted verbatim. All
# other variables are HTML-escaped before substitution so that any future caller
# that passes user-controlled data through the template (display name, email,
# etc.) cannot inject script tags or break out of attributes.
_HTML_RAW_VARS = frozenset({"founding_note", "upgrade_cta"})


def _render(text: str, data: dict[str, str], escape: bool = False) -> str:
    import html as _html
    for key, value in data.items():
        if escape and key not in _HTML_RAW_VARS:
            safe = _html.escape(str(value), quote=True)
        else:
            safe = value
        text = text.replace("{{" + key + "}}", safe)
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

    html = _render(_load_template(template), data, escape=True)
    subject = _render(_SUBJECTS.get(template, "Wayforth"), data, escape=False)

    # Build plain-text version; swap HTML block variables for their text equivalents
    # so callers don't need to maintain parallel text/HTML data dicts.
    text_data = {k: _HTML_TO_TEXT.get(v, v) for k, v in data.items()}
    plain = _render(_PLAIN_TEXTS.get(template, subject), text_data, escape=False)

    import resend as _resend  # imported lazily so tests can patch before import

    _resend.api_key = api_key

    def _send() -> dict:
        return _resend.Emails.send({
            "from": _FROM,
            "reply_to": _REPLY_TO,
            "to": to,
            "subject": subject,
            "html": html,
            "text": plain,
            "headers": _TRANSACTIONAL_HEADERS,
        })

    result = await asyncio.to_thread(_send)
    message_id: str = result.get("id", "") if isinstance(result, dict) else ""
    logger.info("email sent template=%s to=%s id=%s", template, to, message_id)
    return message_id
