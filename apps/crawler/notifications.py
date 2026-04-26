import logging
import os

import resend

resend.api_key = os.getenv("RESEND_API_KEY", "")
logger = logging.getLogger("wayforth.crawler")
FROM_EMAIL = "Wayforth <noreply@wayforth.io>"


def send_tier2_promotion_email(to_email: str, service_name: str, service_id: str) -> bool:
    if not resend.api_key:
        return False
    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": to_email,
            "subject": f"🎉 {service_name} is now Tier 2 Verified on Wayforth",
            "html": f"""
            <div style="font-family:sans-serif;max-width:600px;margin:0 auto;background:#0F172A;color:#E2E8F0;padding:40px;border-radius:12px;">
                <h1 style="color:#4F46E5">Wayforth</h1>
                <h2>Your service reached Tier 2</h2>
                <p><strong>{service_name}</strong> has passed Wayforth's automated verification and is now <strong>Tier 2 — Executable</strong>.</p>
                <div style="background:#1E293B;border-radius:8px;padding:20px;margin:24px 0;border-left:4px solid #10B981;">
                    <p style="color:#10B981;font-weight:bold;margin:0">✓ Tier 2 — Verified</p>
                    <p style="color:#94A3B8;font-size:13px;margin:8px 0 0">90%+ uptime confirmed over 7 days. Your service now appears by default in agent search results.</p>
                </div>
                <p>What this means:</p>
                <ul>
                    <li>Your service is now the default shown to AI agents searching Wayforth</li>
                    <li>Agents can discover and pay for your service directly</li>
                    <li>Your Tier 2 badge is visible on the leaderboard</li>
                </ul>
                <p>Want to upgrade to Tier 3? <a href="https://wayforth.io/tier3" style="color:#4F46E5">Apply for KYB verification →</a></p>
                <p style="color:#64748B;font-size:13px;margin-top:32px">Wayforth · <a href="https://wayforth.io" style="color:#4F46E5">wayforth.io</a></p>
            </div>
            """,
        })
        return True
    except Exception as e:
        logger.error(f"Tier2 promotion email failed: {e}")
        return False
