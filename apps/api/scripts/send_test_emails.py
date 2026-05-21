"""Send one test email per template to dorassulin1@gmail.com."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.email import send_email, _FOUNDING_NOTE_HTML, _UPGRADE_CTA_HTML

TO = "dorassulin1@gmail.com"

EMAILS = [
    (
        "welcome",
        {
            "credits": "100",
            "quick_start": "uvx wayforth-mcp",
            "founding_note": _FOUNDING_NOTE_HTML,
        },
    ),
    (
        "subscription_confirmed",
        {
            "plan_name": "Starter",
            "credits_added": "21,000",
            "amount": "$29.00",
            "renewal_date": "June 21, 2026",
        },
    ),
    (
        "low_credits",
        {
            "credits_remaining": "1,050",
            "percent_remaining": "5%",
            "renewal_date": "June 21, 2026",
            "upgrade_cta": _UPGRADE_CTA_HTML,
        },
    ),
    (
        "founding_member",
        {
            "bonus_credits": "500",
            "cutoff_date": "August 31, 2026",
        },
    ),
]


async def main() -> None:
    for template, data in EMAILS:
        msg_id = await send_email(TO, template, data)
        if msg_id:
            print(f"  ✓  {template:<28} {msg_id}")
        else:
            print(f"  ✗  {template:<28} (no message ID — check RESEND_API_KEY)", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
