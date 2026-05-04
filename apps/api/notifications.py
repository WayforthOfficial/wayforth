import os
import logging

import resend

resend.api_key = os.getenv("RESEND_API_KEY", "")
logger = logging.getLogger("wayforth")
FROM_EMAIL = "Wayforth <noreply@wayforth.io>"


def send_submission_confirmation(to_email: str, service_name: str, service_id: str, endpoint_url: str) -> bool:
    if not resend.api_key or not to_email:
        return False
    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": to_email,
            "subject": f"Your service '{service_name}' has been submitted to Wayforth",
            "html": f"""
            <div style="font-family:sans-serif;max-width:600px;margin:0 auto;background:#0F172A;color:#E2E8F0;padding:40px;border-radius:12px;">
                <h1 style="color:#4F46E5">Wayforth</h1>
                <h2>Service Submitted ✓</h2>
                <p>Your service <strong>{service_name}</strong> has been added to the Wayforth catalog at Tier 0.</p>
                <div style="background:#1E293B;border-radius:8px;padding:20px;margin:24px 0">
                    <p style="color:#64748B;font-size:13px">SERVICE ID</p>
                    <p style="font-family:monospace;color:#4F46E5;font-size:13px">{service_id}</p>
                    <p style="color:#64748B;font-size:13px;margin-top:16px">ENDPOINT</p>
                    <p style="font-family:monospace;font-size:13px">{endpoint_url}</p>
                </div>
                <p>Our crawler probes your endpoint within 24 hours. Pass validation → Tier 1. Maintain 90%+ uptime for 7 days → Tier 2 (default search results).</p>
                <p style="margin-top:24px"><a href="https://wayforth.io/spec/coverage-tiers/v1" style="color:#4F46E5">Coverage tier spec →</a></p>
                <p style="margin-top:32px;color:#64748B;font-size:13px">Wayforth · <a href="https://wayforth.io" style="color:#4F46E5">wayforth.io</a></p>
            </div>
            """,
        })
        return True
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return False


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


def send_tier3_application_notification(to_email: str, service_name: str, company_name: str, app_id: str) -> bool:
    if not resend.api_key:
        return False
    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": to_email,
            "subject": f"Wayforth Tier 3 Application Received — {service_name}",
            "html": f"""
            <div style="font-family:sans-serif;max-width:600px;margin:0 auto;background:#0F172A;color:#E2E8F0;padding:40px;border-radius:12px;">
                <h1 style="color:#4F46E5">Wayforth</h1>
                <h2>Tier 3 Application Received</h2>
                <p>We've received your application for <strong>{service_name}</strong> ({company_name}) to be verified as a Tier 3 service on Wayforth.</p>
                <div style="background:#1E293B;border-radius:8px;padding:20px;margin:24px 0">
                    <p style="color:#64748B;font-size:13px">APPLICATION ID</p>
                    <p style="font-family:monospace;color:#4F46E5">{app_id}</p>
                </div>
                <p><strong>What happens next:</strong></p>
                <ul>
                    <li>We'll send you a KYB documentation checklist within 24 hours</li>
                    <li>Our team reviews documentation within 2 business days</li>
                    <li>SLA terms negotiated based on your uptime target</li>
                    <li>Tier 3 badge live on your service within 24h of approval</li>
                </ul>
                <p style="margin-top:32px;color:#64748B;font-size:13px">Questions? Reply to this email or contact <a href="https://wayforth.io/contact" style="color:#4F46E5">Contact Us</a></p>
                <p style="color:#64748B;font-size:13px">Wayforth · <a href="https://wayforth.io" style="color:#4F46E5">wayforth.io</a></p>
            </div>
            """,
        })
        return True
    except Exception as e:
        logger.error(f"Tier3 email failed: {e}")
        return False


_TIER_CREDITS = {
    "free":     100,
    "starter":  50_000,
    "pro":      300_000,
    "growth":   1_000_000,
    "enterprise": 1_000_000,
}


def send_welcome_email(to_email: str, api_key_prefix: str, tier: str) -> bool:
    if not resend.api_key:
        return False
    credits = _TIER_CREDITS.get(tier, 100)
    credits_display = f"{credits:,}"
    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": to_email,
            "subject": "Your Wayforth API Key is Ready",
            "html": f"""
            <div style="font-family:sans-serif;max-width:600px;margin:0 auto;background:#0F172A;color:#E2E8F0;padding:40px;border-radius:12px;">
                <h1 style="color:#4F46E5;margin:0 0 8px">Wayforth</h1>
                <p style="color:#64748B;margin:0 0 32px;font-size:13px">The search engine for AI agents</p>

                <h2 style="color:#E2E8F0">Your API key is ready.</h2>
                <p>Key prefix: <code style="background:#1E293B;padding:4px 8px;border-radius:4px;color:#4F46E5">{api_key_prefix}...</code></p>

                <div style="background:#1E293B;border-radius:8px;padding:20px;margin:24px 0;border-left:4px solid #4F46E5;">
                    <p style="color:#4F46E5;font-weight:bold;margin:0 0 8px">You have {credits_display} credits</p>
                    <p style="color:#94A3B8;font-size:13px;margin:0">1 credit = $0.001 · Each search costs 1 credit · Top up anytime at wayforth.io/dashboard</p>
                </div>

                <div style="background:#1E293B;border-radius:8px;padding:20px;margin:24px 0">
                    <p style="color:#64748B;font-size:12px;margin:0 0 8px">QUICK START</p>
                    <code style="color:#4F46E5;font-size:13px">uvx wayforth-mcp</code>
                </div>

                <p><strong>What you can do now:</strong></p>
                <ul style="color:#94A3B8">
                    <li>Search 200+ verified API endpoints</li>
                    <li>Use WayforthQL for structured queries</li>
                    <li>Pay for services with credits via wayforth_pay()</li>
                    <li>Track usage and buy more credits at wayforth.io/dashboard</li>
                </ul>

                <p style="margin-top:32px">
                    <a href="https://wayforth.io/demo" style="background:#4F46E5;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:bold">Try Live Demo →</a>
                </p>

                <p style="margin-top:32px;color:#64748B;font-size:13px">
                    Docs: <a href="https://gateway.wayforth.io/docs" style="color:#4F46E5">API Reference</a> ·
                    GitHub: <a href="https://github.com/WayforthOfficial/wayforth" style="color:#4F46E5">WayforthOfficial/wayforth</a>
                </p>
            </div>
            """,
        })
        return True
    except Exception as e:
        logger.error(f"Welcome email failed: {e}")
        return False
