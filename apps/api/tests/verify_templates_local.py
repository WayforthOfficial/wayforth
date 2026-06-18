"""verify_templates_local.py — verify every Cloud template runs green.

Not a pytest module (kept out of pytest.ini python_files): run it directly.

    .venv/bin/python tests/verify_templates_local.py

Two gates, neither of which touches production:

  STATIC   — ast.parse every python3.12 body; registry consistency
             (services_used ⊆ SERVICE_CONFIGS, code per runtime); env-var
             contract (every os.environ key the code reads is declared).

  LOCAL    — start a mock gateway on 127.0.0.1 that implements the gateway
             CONTRACT (/proxy/<slug> returns representative upstream-shaped
             JSON; /x402/execute returns a settled result; /payments/rails
             returns the real core.rails snapshot). Run each template's
             python3.12 code as a subprocess with WAYFORTH_BASE_URL pointed at
             the mock, and require exit 0 + valid JSON on stdout.

This proves the template code + gateway contract end-to-end without 17 paid
upstream keys and without creating any prod agents/runs/signals.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.templates import TEMPLATES  # noqa: E402
from services.managed import SERVICE_CONFIGS  # noqa: E402

# Env keys the runtime always injects — allowed without an env_vars declaration.
_AMBIENT = {"WAYFORTH_API_KEY", "X_WAYFORTH_AGENT_ID", "WAYFORTH_BASE_URL"}

# Representative upstream-shaped payloads, keyed by managed slug. Shapes match
# what the template code reads (see services/templates_specs.py).
# These match the proxy's NORMALIZED output (services.managed adapters), NOT the
# raw upstream APIs — that is what a deployed agent actually receives.
_MOCK = {
    "groq": {"content": "Mock LLM response with three key points.", "model": "llama-3.3-70b-versatile", "tokens_used": 42},
    "together": {"content": "Mock LLM response.", "model": "together", "tokens_used": 42},
    "mistral": {"content": "{\"field\": \"value\"}", "model": "mistral", "tokens_used": 42},
    "perplexity": {"content": "Mock answer with reasoning.", "model": "sonar", "citations": []},
    "gemini": {"content": "Mock Gemini answer.", "model": "gemini-2.5-flash", "tokens_used": 42},
    "serper": {"organic": [{"title": "Result", "link": "https://example.com", "snippet": "Snippet."}]},
    "tavily": {"query": "q", "results": [{"title": "Result", "url": "https://example.com", "content": "Content."}], "answer": "Answer."},
    "brave": {"query": "q", "results": [{"title": "Result", "url": "https://example.com", "description": "Desc."}]},
    "deepl": {"translated_text": "Hola, mundo!", "detected_source_lang": "EN"},
    "openweather": {"city": "London", "temp_c": 15.2, "temp_f": 59.4, "condition": "clear sky", "humidity": 70, "wind_kph": 10},
    "alphavantage": {"symbol": "AAPL", "price": 180.0, "open": 178.0, "high": 181.0, "low": 177.0, "previous_close": 179.0, "change": 1.0, "change_pct": "0.56%", "volume": 1000000},
    "jina": {"title": "Page", "content": "Clean page text content.", "url": "https://example.com"},
    "firecrawl": {"url": "https://example.com", "markdown": "# Page\n\nScraped markdown.", "title": "Page"},
    "stability": {"image_base64": "QUJDRA==" * 8, "seed": 1, "finish_reason": "SUCCESS"},
    "elevenlabs": {"audio_base64": "QUJDRA==" * 8, "voice_id": "21m00Tcm4TlvDq8ikWAM", "characters": 50},
    "assemblyai": {"transcript_id": "t_1", "text": "This is the transcribed audio text.", "status": "completed"},
    "resend": {"email_id": "email_mock_123", "status": "sent"},
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def _send(self, code, body):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("x-wayforth-wri", "82")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.startswith("/payments/rails"):
            from core.rails import rails_status
            return self._send(200, rails_status())
        m = re.match(r"/proxy/([a-z0-9_\-]+)", self.path)  # GET-style proxy (openweather etc.)
        if m:
            return self._send(200, _MOCK.get(m.group(1), {"ok": True, "slug": m.group(1)}))
        return self._send(404, {"error": "not_found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        _ = self.rfile.read(length)
        m = re.match(r"/proxy/([a-z0-9_\-]+)", self.path)
        if m:
            slug = m.group(1)
            return self._send(200, _MOCK.get(slug, {"ok": True, "slug": slug}))
        if self.path.startswith("/x402/execute"):
            return self._send(200, {"result": {"ok": True}, "payment_confirmed": True})
        return self._send(404, {"error": "not_found"})


def _start_gateway():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def _declared_env_keys(t: dict) -> set:
    return {e["name"] for e in t.get("env_vars", [])} | _AMBIENT


def _read_env_keys(code: str) -> set:
    keys = set(re.findall(r'os\.environ\.get\(\s*["\']([A-Z0-9_]+)["\']', code))
    keys |= set(re.findall(r'os\.environ\[\s*["\']([A-Z0-9_]+)["\']\s*\]', code))
    return keys


def main() -> int:
    static_errors: list[str] = []
    for tid, t in TEMPLATES.items():
        for s in t.get("services_used", []):
            if s not in SERVICE_CONFIGS:
                static_errors.append(f"{tid}: unknown service {s}")
        for rt in t["runtimes"]:
            if rt not in t["code"] or not t["code"][rt].strip():
                static_errors.append(f"{tid}: missing/empty {rt} code")
        py = t["code"]["python3.12"]
        try:
            ast.parse(py)
        except SyntaxError as e:
            static_errors.append(f"{tid}: PY SYNTAX line {e.lineno}: {e.msg}")
            continue
        undeclared = _read_env_keys(py) - _declared_env_keys(t)
        if undeclared:
            static_errors.append(f"{tid}: reads undeclared env {sorted(undeclared)}")

    print(f"STATIC: {len(TEMPLATES)} templates")
    if static_errors:
        print("  STATIC FAILURES:")
        for e in static_errors:
            print("   -", e)
        return 1
    print("  ✓ python parse + registry + env-contract clean")

    srv, port = _start_gateway()
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "WAYFORTH_API_KEY": "wf_local_test",
        "X_WAYFORTH_AGENT_ID": "local-verify-run",
        "WAYFORTH_BASE_URL": base,
        "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    }
    green, failures = 0, []
    for tid, t in TEMPLATES.items():
        code = t["code"]["python3.12"]
        # Inject the template's declared env-var defaults — exactly what a deploy
        # supplies. Templates with a non-empty default run green with no config.
        run_env = dict(env)
        for ev in t.get("env_vars", []):
            if ev.get("default") not in (None, ""):
                run_env[ev["name"]] = str(ev["default"])
        proc = subprocess.run(
            [sys.executable, "-c", code], env=run_env,
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            failures.append(f"{tid}: exit {proc.returncode} :: {proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ''}")
            continue
        # Require at least one valid JSON object on stdout.
        ok_json = any(
            ln.strip().startswith("{") and _is_json(ln) for ln in proc.stdout.splitlines()
        ) or _has_json_block(proc.stdout)
        if not ok_json:
            failures.append(f"{tid}: no JSON output")
            continue
        green += 1
    srv.shutdown()

    print(f"\nLOCAL (python3.12 vs mock gateway): {green}/{len(TEMPLATES)} green")
    if failures:
        print("  LOCAL FAILURES:")
        for f in failures:
            print("   -", f)
        return 1
    print("  ✓ all templates ran to completion and emitted JSON")
    print(f"\nVERIFIED: {len(TEMPLATES)} templates green (static + local).")
    return 0


def _is_json(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except Exception:
        return False


def _has_json_block(out: str) -> bool:
    # indent=2 JSON spans multiple lines; try to find a {...} block that parses.
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return False
    return _is_json(m.group(0))


if __name__ == "__main__":
    raise SystemExit(main())
