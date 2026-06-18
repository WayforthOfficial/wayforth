"""services/templates_catalog.py — generated Cloud templates (catalog expansion).

The hand-tuned originals live in templates.py. This module generates the rest of
the catalog from compact specs so every template is uniform, parse-clean, and
locally verifiable. Each generated agent:

  * reads its gateway base from WAYFORTH_BASE_URL (default the prod gateway), so
    the same code runs unchanged in E2B (prod) and under the local verification
    gateway — no host edits, no prod writes during verification;
  * authenticates with WAYFORTH_API_KEY + X-Wayforth-Agent-ID;
  * reads its inputs from env vars (present-tense, ready-to-go defaults);
  * calls the managed /proxy/<slug> rail (or the x402 pay-per-call rail for the
    payment-native templates, which read GET /payments/rails for live status);
  * prints a structured JSON result and exits 0.

EXTRA_TEMPLATES is merged into templates.TEMPLATES at import time.
"""
from __future__ import annotations

# ── code fragments shared by every generated runtime ──────────────────────────

_PY_HEAD = '''\
"""
Wayforth Cloud Template: {name} (Python)
{tagline}

Credits: ~{credits} per run ({breakdown})
"""
import json
import os

import httpx

KEY    = os.environ["WAYFORTH_API_KEY"]
RUN_ID = os.environ.get("X_WAYFORTH_AGENT_ID", "")
BASE   = os.environ.get("WAYFORTH_BASE_URL", "https://gateway.wayforth.io").rstrip("/")
HDR    = {{"X-Wayforth-API-Key": KEY, "X-Wayforth-Agent-ID": RUN_ID}}
'''

_TS_HEAD = '''\
/**
 * Wayforth Cloud Template: {name} (TypeScript)
 * {tagline}
 *
 * Credits: ~{credits} per run ({breakdown})
 */
const KEY    = process.env.WAYFORTH_API_KEY!;
const RUN_ID = process.env.X_WAYFORTH_AGENT_ID ?? \'\';
const BASE   = (process.env.WAYFORTH_BASE_URL ?? \'https://gateway.wayforth.io\').replace(/\\/$/, \'\');
const HDR    = {{ \'Content-Type\': \'application/json\', \'X-Wayforth-API-Key\': KEY, \'X-Wayforth-Agent-ID\': RUN_ID }};
'''


def _py_post(slug: str, body_expr: str, var: str = "resp") -> str:
    return (
        f'{var} = httpx.post(f"{{BASE}}/proxy/{slug}", headers=HDR, json={body_expr}, timeout=60)\n'
        f'{var}.raise_for_status()\n'
        f'{var}_data = {var}.json()\n'
    )


def _ts_post(slug: str, body_expr: str, var: str = "resp") -> str:
    return (
        f"const {var} = await fetch(`${{BASE}}/proxy/{slug}`, {{ method: 'POST', headers: HDR, "
        f"body: JSON.stringify({body_expr}) }});\n"
        f"if (!{var}.ok) throw new Error(`{slug} ${{{var}.status}}: ${{await {var}.text()}}`);\n"
        f"const {var}Data = await {var}.json() as any;\n"
    )


# ── shape generators ──────────────────────────────────────────────────────────


def _gen_llm(spec: dict) -> dict:
    slug = spec["slug"]
    sys_prompt = spec["system"]
    py = _PY_HEAD.format(**spec)
    py += f'PROMPT = os.environ.get("PROMPT", {spec["default_prompt"]!r})\n'
    py += f'MODEL  = os.environ.get("MODEL", {spec["model"]!r})\n'
    py += f'print(f"[{spec["id"]}] run={{RUN_ID[:8]}}...")\n'
    body = (
        '{"messages": [{"role": "system", "content": ' + repr(sys_prompt) + '}, '
        '{"role": "user", "content": PROMPT}], "model": MODEL, "max_tokens": 700}'
    )
    py += _py_post(slug, body)
    py += 'content = resp_data.get("content") or resp_data.get("choices", [{}])[0].get("message", {}).get("content", "")\n'
    py += (
        'output = {"input": PROMPT, "output": content, "model": MODEL, '
        '"wri": resp.headers.get("x-wayforth-wri"), "run_id": RUN_ID}\n'
    )
    py += 'print(json.dumps(output, indent=2))\n'
    py += f'print("[{spec["id"]}] done")\n'

    ts = _TS_HEAD.format(**spec)
    ts += f"const PROMPT = process.env.PROMPT ?? {_js(spec['default_prompt'])};\n"
    ts += f"const MODEL  = process.env.MODEL ?? {_js(spec['model'])};\n"
    ts += f"console.log(`[{spec['id']}] run=${{RUN_ID.slice(0, 8)}}...`);\n"
    ts_body = (
        "{ messages: [{ role: 'system', content: " + _js(sys_prompt) + " }, "
        "{ role: 'user', content: PROMPT }], model: MODEL, max_tokens: 700 }"
    )
    ts += _ts_post(slug, ts_body)
    ts += "const content = respData.content ?? respData?.choices?.[0]?.message?.content ?? '';\n"
    ts += ("const output = { input: PROMPT, output: content, model: MODEL, "
           "wri: resp.headers.get('x-wayforth-wri'), run_id: RUN_ID };\n")
    ts += "console.log(JSON.stringify(output, null, 2));\n"
    ts += f"console.log(`[{spec['id']}] done`);\n"
    return {"python3.12": py, "node20": ts}


def _gen_search(spec: dict) -> dict:
    slug = spec["slug"]
    # response key + item field mapping per search provider
    py = _PY_HEAD.format(**spec)
    py += f'QUERY = os.environ.get("QUERY", {spec["default_query"]!r})\n'
    py += 'NUM   = min(int(os.environ.get("NUM_RESULTS", "5")), 10)\n'
    py += f'print(f"[{spec["id"]}] query={{QUERY!r}} run={{RUN_ID[:8]}}...")\n'
    body = spec["body"]
    py += _py_post(slug, body)
    py += f'items = {spec["py_extract"]}\n'
    py += (
        'results = [{"position": i + 1, "title": (it.get("title") or ""), '
        '"url": (it.get("url") or it.get("link") or ""), '
        '"snippet": (it.get("snippet") or it.get("content") or it.get("description") or "")} '
        'for i, it in enumerate(items[:NUM])]\n'
    )
    py += (
        'output = {"query": QUERY, "result_count": len(results), "results": results, '
        '"wri": resp.headers.get("x-wayforth-wri"), "run_id": RUN_ID}\n'
    )
    py += 'print(json.dumps(output, indent=2))\n'
    py += f'print(f"[{spec["id"]}] done — {{len(results)}} result(s)")\n'

    ts = _TS_HEAD.format(**spec)
    ts += f"const QUERY = process.env.QUERY ?? {_js(spec['default_query'])};\n"
    ts += "const NUM   = Math.min(parseInt(process.env.NUM_RESULTS ?? '5', 10), 10);\n"
    ts += f"console.log(`[{spec['id']}] query=${{JSON.stringify(QUERY)}} run=${{RUN_ID.slice(0, 8)}}...`);\n"
    ts += _ts_post(slug, spec["ts_body"])
    ts += f"const items = {spec['ts_extract']} ?? [];\n"
    ts += ("const results = items.slice(0, NUM).map((it: any, i: number) => ({ position: i + 1, "
           "title: it.title ?? '', url: it.url ?? it.link ?? '', "
           "snippet: it.snippet ?? it.content ?? it.description ?? '' }));\n")
    ts += ("const output = { query: QUERY, result_count: results.length, results, "
           "wri: resp.headers.get('x-wayforth-wri'), run_id: RUN_ID };\n")
    ts += "console.log(JSON.stringify(output, null, 2));\n"
    ts += f"console.log(`[{spec['id']}] done`);\n"
    return {"python3.12": py, "node20": ts}


def _gen_simple(spec: dict) -> dict:
    """One POST to /proxy/<slug>, env-driven body, print the raw structured result."""
    slug = spec["slug"]
    py = _PY_HEAD.format(**spec)
    for ev in spec["env_reads"]:
        py += f'{ev["var"]} = os.environ.get("{ev["env"]}", {ev["default"]!r})\n'
    py += f'print(f"[{spec["id"]}] run={{RUN_ID[:8]}}...")\n'
    py += _py_post(slug, spec["body"])
    py += f'output = {{"service": "{slug}", "result": {spec["py_result"]}, '
    py += '"wri": resp.headers.get("x-wayforth-wri"), "run_id": RUN_ID}\n'
    py += 'print(json.dumps(output, indent=2, default=str)[:4000])\n'
    py += f'print("[{spec["id"]}] done")\n'

    ts = _TS_HEAD.format(**spec)
    for ev in spec["env_reads"]:
        ts += f"const {ev['var']} = process.env.{ev['env']} ?? {_js(ev['default'])};\n"
    ts += f"console.log(`[{spec['id']}] run=${{RUN_ID.slice(0, 8)}}...`);\n"
    ts += _ts_post(slug, spec["ts_body"])
    ts += (f"const output = {{ service: '{slug}', result: {spec['ts_result']}, "
           "wri: resp.headers.get('x-wayforth-wri'), run_id: RUN_ID };\n")
    ts += "console.log(JSON.stringify(output, null, 2).slice(0, 4000));\n"
    ts += f"console.log(`[{spec['id']}] done`);\n"
    return {"python3.12": py, "node20": ts}


def _gen_chain(spec: dict) -> dict:
    """Two-step chain: a fetch step (search or scrape) feeding an LLM step."""
    fetch_slug = spec["fetch_slug"]
    llm_slug = spec["llm_slug"]
    py = _PY_HEAD.format(**spec)
    py += f'TOPIC = os.environ.get("{spec["input_env"]}", {spec["default_input"]!r})\n'
    py += f'MODEL = os.environ.get("MODEL", {spec["model"]!r})\n'
    py += f'print(f"[{spec["id"]}] input={{TOPIC!r}} run={{RUN_ID[:8]}}...")\n'
    py += _py_post(fetch_slug, spec["fetch_body"], var="fr")
    py += f'context = {spec["py_context"]}\n'
    llm_body = (
        '{"messages": [{"role": "system", "content": ' + repr(spec["system"]) + '}, '
        '{"role": "user", "content": f"{TOPIC}\\n\\n" + context}], '
        '"model": MODEL, "max_tokens": 800}'
    )
    py += _py_post(llm_slug, llm_body, var="lr")
    py += 'summary = lr_data.get("content") or lr_data.get("choices", [{}])[0].get("message", {}).get("content", "")\n'
    py += ('output = {"input": TOPIC, "summary": summary, "context_chars": len(context), '
           '"run_id": RUN_ID}\n')
    py += 'print(json.dumps(output, indent=2))\n'
    py += f'print("[{spec["id"]}] done")\n'

    ts = _TS_HEAD.format(**spec)
    ts += f"const TOPIC = process.env.{spec['input_env']} ?? {_js(spec['default_input'])};\n"
    ts += f"const MODEL = process.env.MODEL ?? {_js(spec['model'])};\n"
    ts += f"console.log(`[{spec['id']}] run=${{RUN_ID.slice(0, 8)}}...`);\n"
    ts += _ts_post(fetch_slug, spec["ts_fetch_body"], var="fr")
    ts += f"const context = {spec['ts_context']};\n"
    ts_llm_body = (
        "{ messages: [{ role: 'system', content: " + _js(spec["system"]) + " }, "
        "{ role: 'user', content: `${TOPIC}\\n\\n` + context }], model: MODEL, max_tokens: 800 }"
    )
    ts += _ts_post(llm_slug, ts_llm_body, var="lr")
    ts += "const summary = lrData.content ?? lrData?.choices?.[0]?.message?.content ?? '';\n"
    ts += ("const output = { input: TOPIC, summary, context_chars: context.length, "
           "run_id: RUN_ID };\n")
    ts += "console.log(JSON.stringify(output, null, 2));\n"
    ts += f"console.log(`[{spec['id']}] done`);\n"
    return {"python3.12": py, "node20": ts}


def _gen_payment(spec: dict) -> dict:
    """Payment-native: read GET /payments/rails, use x402 pay-per-call when live,
    else fall back to the managed key rail. Ships ready-to-go; reads the flag."""
    slug = spec["slug"]
    py = _PY_HEAD.format(**spec)
    py += f'QUERY = os.environ.get("{spec["input_env"]}", {spec["default_input"]!r})\n'
    py += f'print(f"[{spec["id"]}] run={{RUN_ID[:8]}}...")\n'
    py += '# Single source of truth for which rails are live right now.\n'
    py += 'rails = httpx.get(f"{BASE}/payments/rails", timeout=15).json()\n'
    py += 'x402_live = rails.get("rails", {}).get("x402", {}).get("live", False)\n'
    py += f'params = {spec["params"]}\n'
    py += 'if x402_live:\n'
    py += '    # x402 pay-per-call: payment IS auth, no managed key needed.\n'
    py += f'    r = httpx.post(f"{{BASE}}/x402/execute", json={{"service_slug": "{slug}", "params": params}}, timeout=60)\n'
    py += '    paid_via = "x402"\n'
    py += 'else:\n'
    py += f'    r = httpx.post(f"{{BASE}}/proxy/{slug}", headers=HDR, json=params, timeout=60)\n'
    py += '    paid_via = "managed"\n'
    py += 'r.raise_for_status()\n'
    py += ('output = {"query": QUERY, "paid_via": paid_via, "x402_live": x402_live, '
           '"result": r.json(), "run_id": RUN_ID}\n')
    py += 'print(json.dumps(output, indent=2, default=str)[:4000])\n'
    py += f'print(f"[{spec["id"]}] done — paid_via={{paid_via}}")\n'

    ts = _TS_HEAD.format(**spec)
    ts += f"const QUERY = process.env.{spec['input_env']} ?? {_js(spec['default_input'])};\n"
    ts += f"console.log(`[{spec['id']}] run=${{RUN_ID.slice(0, 8)}}...`);\n"
    ts += "const rails = await (await fetch(`${BASE}/payments/rails`)).json() as any;\n"
    ts += "const x402Live = rails?.rails?.x402?.live ?? false;\n"
    ts += f"const params = {spec['ts_params']};\n"
    ts += "let r: Response; let paidVia: string;\n"
    ts += "if (x402Live) {\n"
    ts += (f"  r = await fetch(`${{BASE}}/x402/execute`, {{ method: 'POST', "
           "headers: { 'Content-Type': 'application/json' }, "
           f"body: JSON.stringify({{ service_slug: '{slug}', params }}) }});\n")
    ts += "  paidVia = 'x402';\n"
    ts += "} else {\n"
    ts += (f"  r = await fetch(`${{BASE}}/proxy/{slug}`, {{ method: 'POST', headers: HDR, "
           "body: JSON.stringify(params) });\n")
    ts += "  paidVia = 'managed';\n"
    ts += "}\n"
    ts += "if (!r.ok) throw new Error(`pay path ${r.status}: ${await r.text()}`);\n"
    ts += ("const output = { query: QUERY, paid_via: paidVia, x402_live: x402Live, "
           "result: await r.json(), run_id: RUN_ID };\n")
    ts += "console.log(JSON.stringify(output, null, 2).slice(0, 4000));\n"
    ts += f"console.log(`[{spec['id']}] done`);\n"
    return {"python3.12": py, "node20": ts}


def _js(s: str) -> str:
    """JSON-encode a Python str as a JS string literal (safe quoting)."""
    import json
    return json.dumps(s)


_GENERATORS = {
    "llm": _gen_llm,
    "search": _gen_search,
    "simple": _gen_simple,
    "chain": _gen_chain,
    "payment": _gen_payment,
}


def _readme(spec: dict) -> str:
    services = ", ".join(spec.get("services_used", []))
    envs = "\n".join(
        f"- `{ev['name']}` — {ev['description']}"
        + (f" (default: `{ev['default']}`)" if ev.get("default") not in (None, "") else "")
        for ev in spec.get("env_vars", [])
    )
    return (
        f"# {spec['name']}\n\n{spec['tagline']}\n\n"
        f"**Services:** {services or '—'}  \n"
        f"**Credits per run:** ~{spec['credits']} ({spec['breakdown']})  \n"
        f"**Runtimes:** Python 3.12, Node 20\n\n"
        f"## Configure\n\n{envs or '_No configuration required._'}\n\n"
        f"## Run\n\n```\nPOST /templates/{spec['id']}/deploy\nPOST /cloud/agents/{{agent_id}}/runs\n```\n\n"
        f"Ready to go — deploy and dispatch. The agent reads its inputs from the env "
        f"vars above and prints a structured JSON result.\n"
    )


def _build(spec: dict) -> dict:
    code = _GENERATORS[spec["shape"]](spec)
    return {
        "id":              spec["id"],
        "name":            spec["name"],
        "description":     spec["tagline"],
        "category":        spec["category"],
        "default_runtime": "python3.12",
        "runtimes":        ["python3.12", "node20"],
        "services_used":   spec["services_used"],
        "credits_per_run": {"min": spec["credits"], "max": spec["credits"],
                            "breakdown": spec["breakdown"]},
        "env_vars":        spec["env_vars"],
        "tags":            spec["tags"],
        "code":            code,
        "readme":          _readme(spec),
        "ready":           True,
    }


# ── specs ─────────────────────────────────────────────────────────────────────
# Imported from the sibling spec module to keep this file focused on generation.
from services.templates_specs import SPECS  # noqa: E402

EXTRA_TEMPLATES: dict[str, dict] = {s["id"]: _build(s) for s in SPECS}
