# Security Policy

## Reporting Vulnerabilities

[Contact Us](https://wayforth.io/contact)

Please do not open public GitHub issues for security vulnerabilities. Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (optional)

We will respond within 48 hours and aim to patch within 7 days.

## Scope

In scope: API endpoints, MCP server, SDKs, credits/payment system.
Out of scope: third-party services indexed in the Wayforth catalog.

## Vendored Third-Party Code

The following paths contain vendored upstream code and are excluded from
internal security review. Findings reported against these paths should be
suppressed in the SAST dashboard (Aikido/etc.) as "third-party upstream":

- `contracts/base/lib/forge-std/**` — Foundry `forge-std` library, installed
  via `forge install` and not authored by Wayforth. Build-time only; not
  reachable from any deployed binary. Patching it would diverge from upstream
  and be clobbered on next `forge install`. If a finding is critical, report
  upstream at https://github.com/foundry-rs/forge-std and pin to a fixed tag.

If you add new vendored code, list it here.
