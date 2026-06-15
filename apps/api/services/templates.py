"""services/templates.py — Wayforth Cloud template registry.

Templates are Hyperagent-500-grade agents that run on Cloud out of the box.
Each template ships in both Python 3.12 and TypeScript/Node 20 and has been
proven end-to-end against the live gateway and E2B sandbox.

Credit costs per run (from managed.py SERVICE_CONFIGS):
  serper    : 3 credits/call
  deepl     : 20 credits/call
  resend    : 3 credits/call
  openweather: 2 credits/call
  compute   : 1 credit/min (ceil, min 1)

All template code is stored as plain strings so the deploy endpoint can
insert it into hosted_agents.code with a single DB write — no filesystem reads
required at deploy time.
"""
from __future__ import annotations

# ── Template 1: Research Agent ────────────────────────────────────────────────

_RESEARCH_PY = '''\
"""
Wayforth Cloud Template: Research Agent (Python)
Searches the web for any topic and returns structured results.

Credits: ~4 per run (3 Serper + 1 compute)
Customize: set SEARCH_QUERY and NUM_RESULTS env vars on the agent.
"""
import json
import os

import httpx

KEY    = os.environ["WAYFORTH_API_KEY"]
RUN_ID = os.environ.get("X_WAYFORTH_AGENT_ID", "")
QUERY  = os.environ.get("SEARCH_QUERY", "latest AI agent frameworks 2025")
NUM    = min(int(os.environ.get("NUM_RESULTS", "5")), 10)

print(f"[research] query={QUERY!r} n={NUM} run={RUN_ID[:8]}...")

resp = httpx.post(
    "https://gateway.wayforth.io/proxy/serper",
    headers={
        "X-Wayforth-API-Key": KEY,
        "X-Wayforth-Agent-ID": RUN_ID,
    },
    json={"query": QUERY, "num": NUM},
    timeout=30,
)
resp.raise_for_status()
data = resp.json()

results = [
    {
        "position": i + 1,
        "title":    r.get("title", ""),
        "url":      r.get("link", ""),
        "snippet":  r.get("snippet", ""),
    }
    for i, r in enumerate(data.get("organic", [])[:NUM])
]

output = {
    "query":        QUERY,
    "result_count": len(results),
    "results":      results,
    "answer_box":   data.get("answer_box"),
    "wri":          resp.headers.get("x-wayforth-wri"),
    "run_id":       RUN_ID,
}

print(json.dumps(output, indent=2))
print(f"[research] done — {len(results)} result(s) for {QUERY!r}")
'''

_RESEARCH_TS = '''\
/**
 * Wayforth Cloud Template: Research Agent (TypeScript)
 * Searches the web for any topic and returns structured results.
 *
 * Credits: ~4 per run (3 Serper + 1 compute)
 * Customize: set SEARCH_QUERY and NUM_RESULTS env vars on the agent.
 */
const KEY    = process.env.WAYFORTH_API_KEY!;
const RUN_ID = process.env.X_WAYFORTH_AGENT_ID ?? \'\';
const QUERY  = process.env.SEARCH_QUERY ?? \'latest AI agent frameworks 2025\';
const NUM    = Math.min(parseInt(process.env.NUM_RESULTS ?? \'5\', 10), 10);

console.log(`[research] query=${JSON.stringify(QUERY)} n=${NUM} run=${RUN_ID.slice(0, 8)}...`);

const resp = await fetch(\'https://gateway.wayforth.io/proxy/serper\', {
  method: \'POST\',
  headers: {
    \'Content-Type\':          \'application/json\',
    \'X-Wayforth-API-Key\':    KEY,
    \'X-Wayforth-Agent-ID\':   RUN_ID,
  },
  body: JSON.stringify({ query: QUERY, num: NUM }),
});

if (!resp.ok) throw new Error(`Serper error ${resp.status}: ${await resp.text()}`);

const data = await resp.json() as {
  organic?: Array<{ title?: string; link?: string; snippet?: string }>;
  answer_box?: string;
};

const results = (data.organic ?? []).slice(0, NUM).map((r, i) => ({
  position: i + 1,
  title:    r.title   ?? \'\',
  url:      r.link    ?? \'\',
  snippet:  r.snippet ?? \'\',
}));

const output = {
  query:        QUERY,
  result_count: results.length,
  results,
  answer_box:   data.answer_box ?? null,
  wri:          resp.headers.get(\'x-wayforth-wri\'),
  run_id:       RUN_ID,
};

console.log(JSON.stringify(output, null, 2));
console.log(`[research] done — ${results.length} result(s) for ${JSON.stringify(QUERY)}`);
'''

_RESEARCH_README = """\
# Research Agent

Searches the web for any topic via Serper and returns structured results.
Ideal for news summarization, competitive research, and information gathering.

## Credits per run
- 3 credits per Serper search call
- 1 credit compute (ceil to 1 min)
- **Total: ~4 credits/run**

## Configuration (env vars on the agent)
| Variable | Default | Description |
|---|---|---|
| `SEARCH_QUERY` | `latest AI agent frameworks 2025` | Topic or question to research |
| `NUM_RESULTS` | `5` | Results to return (1–10) |

## Output
```json
{
  "query": "...",
  "result_count": 5,
  "results": [
    { "position": 1, "title": "...", "url": "...", "snippet": "..." }
  ],
  "answer_box": "...",
  "wri": "96.0",
  "run_id": "..."
}
```

## Customization ideas
- Chain with an LLM call (Groq/Together/Mistral proxy) to summarize the results
- Add multiple queries in a loop for multi-topic research
- Filter results by domain using snippet keywords
"""

# ── Template 2: Translate-Notify Agent ────────────────────────────────────────

_TRANSLATE_PY = '''\
"""
Wayforth Cloud Template: Translate-Notify Agent (Python)
Translates text via DeepL, optionally sends an email notification via Resend.

Credits: ~21 per run (20 DeepL + 1 compute) + 3 if NOTIFY_EMAIL is set (Resend)
Customize: set TRANSLATE_TEXT, TARGET_LANG, NOTIFY_EMAIL env vars on the agent.

Note: To send email, set NOTIFY_EMAIL and ensure your FROM_EMAIL domain is
verified at resend.com/domains. The translation step works without email setup.
"""
import json
import os

import httpx

KEY    = os.environ["WAYFORTH_API_KEY"]
RUN_ID = os.environ.get("X_WAYFORTH_AGENT_ID", "")
TEXT   = os.environ.get("TRANSLATE_TEXT", "Hello, world! Welcome to Wayforth Cloud.")
LANG   = os.environ.get("TARGET_LANG", "ES")
NOTIFY = os.environ.get("NOTIFY_EMAIL", "")
FROM   = os.environ.get("FROM_EMAIL", "noreply@wayforth.io")

HEADERS = {
    "X-Wayforth-API-Key":  KEY,
    "X-Wayforth-Agent-ID": RUN_ID,
}

print(f"[translate-notify] run={RUN_ID[:8]}... text={TEXT[:40]!r} lang={LANG}")

# Step 1: Translate
trans_resp = httpx.post(
    "https://gateway.wayforth.io/proxy/deepl",
    headers=HEADERS,
    json={"text": TEXT, "target_lang": LANG},
    timeout=30,
)
trans_resp.raise_for_status()
translated = trans_resp.json().get("translated_text", "")
detected   = trans_resp.json().get("detected_source_lang", "")
print(f"[translate-notify] translated ({detected} → {LANG}): {translated[:80]!r}")

# Step 2: Notify (optional — only runs when NOTIFY_EMAIL is set)
email_result = None
if NOTIFY:
    email_resp = httpx.post(
        "https://gateway.wayforth.io/proxy/resend",
        headers=HEADERS,
        json={
            "from":    FROM,
            "to":      NOTIFY,
            "subject": f"Translation to {LANG} ready",
            "html": (
                f"<h2>Translation complete</h2>"
                f"<p><strong>Original ({detected}):</strong><br>{TEXT}</p>"
                f"<p><strong>Translation ({LANG}):</strong><br>{translated}</p>"
                f"<p><em>Sent by Wayforth Cloud · run {RUN_ID[:8]}</em></p>"
            ),
        },
        timeout=30,
    )
    email_resp.raise_for_status()
    email_result = email_resp.json()
    print(f"[translate-notify] email sent: id={email_result.get('email_id')}")

output = {
    "original":          TEXT,
    "source_lang":       detected,
    "target_lang":       LANG,
    "translated":        translated,
    "email_sent":        email_result is not None,
    "email_id":          email_result.get("email_id") if email_result else None,
    "wri_translate":     trans_resp.headers.get("x-wayforth-wri"),
    "run_id":            RUN_ID,
}

print(json.dumps(output, indent=2))
print("[translate-notify] done")
'''

_TRANSLATE_TS = '''\
/**
 * Wayforth Cloud Template: Translate-Notify Agent (TypeScript)
 * Translates text via DeepL, optionally sends an email notification via Resend.
 *
 * Credits: ~21/run (20 DeepL + 1 compute) + 3 if NOTIFY_EMAIL set (Resend)
 * Note: To send email, NOTIFY_EMAIL must be set and FROM_EMAIL domain verified
 * at resend.com/domains. Translation works without any email setup.
 */
const KEY    = process.env.WAYFORTH_API_KEY!;
const RUN_ID = process.env.X_WAYFORTH_AGENT_ID ?? \'\';
const TEXT   = process.env.TRANSLATE_TEXT ?? \'Hello, world! Welcome to Wayforth Cloud.\';
const LANG   = process.env.TARGET_LANG ?? \'ES\';
const NOTIFY = process.env.NOTIFY_EMAIL ?? \'\';
const FROM   = process.env.FROM_EMAIL ?? \'noreply@wayforth.io\';

const HEADERS = {
  \'X-Wayforth-API-Key\':  KEY,
  \'X-Wayforth-Agent-ID\': RUN_ID,
  \'Content-Type\':         \'application/json\',
};

console.log(`[translate-notify] run=${RUN_ID.slice(0, 8)}... text=${JSON.stringify(TEXT.slice(0, 40))} lang=${LANG}`);

// Step 1: Translate
const transResp = await fetch(\'https://gateway.wayforth.io/proxy/deepl\', {
  method: \'POST\',
  headers: HEADERS,
  body: JSON.stringify({ text: TEXT, target_lang: LANG }),
});
if (!transResp.ok) throw new Error(`DeepL error ${transResp.status}: ${await transResp.text()}`);
const transData = await transResp.json() as { translated_text: string; detected_source_lang: string };
const translated = transData.translated_text;
const detected   = transData.detected_source_lang;
console.log(`[translate-notify] translated (${detected} → ${LANG}): ${JSON.stringify(translated.slice(0, 80))}`);

// Step 2: Notify (only runs when NOTIFY_EMAIL is set)
let emailResult: { email_id?: string; status?: string } | null = null;
if (NOTIFY) {
  const emailResp = await fetch(\'https://gateway.wayforth.io/proxy/resend\', {
    method: \'POST\',
    headers: HEADERS,
    body: JSON.stringify({
      from:    FROM,
      to:      NOTIFY,
      subject: `Translation to ${LANG} ready`,
      html: [
        \'<h2>Translation complete</h2>\',
        `<p><strong>Original (${detected}):</strong><br>${TEXT}</p>`,
        `<p><strong>Translation (${LANG}):</strong><br>${translated}</p>`,
        `<p><em>Sent by Wayforth Cloud · run ${RUN_ID.slice(0, 8)}</em></p>`,
      ].join(\'\'),
    }),
  });
  if (!emailResp.ok) throw new Error(`Resend error ${emailResp.status}: ${await emailResp.text()}`);
  emailResult = await emailResp.json() as { email_id?: string; status?: string };
  console.log(`[translate-notify] email sent: id=${emailResult.email_id}`);
}

const output = {
  original:      TEXT,
  source_lang:   detected,
  target_lang:   LANG,
  translated,
  email_sent:    emailResult !== null,
  email_id:      emailResult?.email_id ?? null,
  wri_translate: transResp.headers.get(\'x-wayforth-wri\'),
  run_id:        RUN_ID,
};

console.log(JSON.stringify(output, null, 2));
console.log(\'[translate-notify] done\');
'''

_TRANSLATE_README = """\
# Translate-Notify Agent

Translates text to any language via DeepL, then optionally sends the result
to an email address via Resend. A two-service chain that demonstrates how
Wayforth Cloud agents compose multiple proxy calls in a single run.

## Credits per run
| Step | Service | Credits |
|---|---|---|
| Translation | DeepL | 20 |
| Email (optional) | Resend | 3 |
| Compute | E2B | 1 |
| **Total** | | **21** (translate only) or **24** (with email) |

## Configuration (env vars on the agent)
| Variable | Default | Description |
|---|---|---|
| `TRANSLATE_TEXT` | `Hello, world! Welcome to Wayforth Cloud.` | Text to translate (max 2,000 chars) |
| `TARGET_LANG` | `ES` | DeepL language code (ES, FR, DE, JA, ZH, PT-BR, …) |
| `NOTIFY_EMAIL` | *(empty — email disabled)* | Recipient address for the translation email |
| `FROM_EMAIL` | `noreply@wayforth.io` | Sender address (must be a Resend-verified domain) |

## Email setup
Email sending is **optional** and only fires when `NOTIFY_EMAIL` is set.
Translation works without any email setup. To enable email:
1. Verify your sender domain at [resend.com/domains](https://resend.com/domains)
2. Set `FROM_EMAIL` to an address on that domain

## Output
```json
{
  "original": "Hello, world!",
  "source_lang": "EN",
  "target_lang": "ES",
  "translated": "¡Hola, mundo!",
  "email_sent": true,
  "email_id": "re_...",
  "wri_translate": "88.0",
  "run_id": "..."
}
```

## Customization ideas
- Batch-translate multiple strings by looping over a list in `TRANSLATE_TEXT`
- Chain to a Slack webhook or Discord instead of email
- Add a sentiment analysis step after translation using a Groq/Mistral call
"""

# ── Template 3: Market-Data Agent ─────────────────────────────────────────────

_DATAFETCH_PY = '''\
"""
Wayforth Cloud Template: Market Data Agent (Python)
Fetches current weather or stock data and returns a structured report.

Credits: ~3 per run (2 OpenWeather or 4 Alpha Vantage + 1 compute)
Customize: set DATA_SOURCE, CITY, or SYMBOL env vars on the agent.

Supported sources:
  weather  — OpenWeather current conditions (2 credits)
  finance  — Alpha Vantage quote (4 credits)  [default: weather]
"""
import json
import os

import httpx

KEY       = os.environ["WAYFORTH_API_KEY"]
RUN_ID    = os.environ.get("X_WAYFORTH_AGENT_ID", "")
SOURCE    = os.environ.get("DATA_SOURCE", "weather").lower()
CITY      = os.environ.get("CITY", "London")
SYMBOL    = os.environ.get("SYMBOL", "AAPL")

HEADERS = {
    "X-Wayforth-API-Key":  KEY,
    "X-Wayforth-Agent-ID": RUN_ID,
}

print(f"[data-fetch] run={RUN_ID[:8]}... source={SOURCE}")

if SOURCE == "finance":
    # Alpha Vantage: stock quote
    resp = httpx.get(
        f"https://gateway.wayforth.io/proxy/alphavantage",
        headers=HEADERS,
        params={"symbol": SYMBOL, "function": "GLOBAL_QUOTE"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    output = {
        "source":   "alphavantage",
        "symbol":   SYMBOL,
        "price":    data.get("price"),
        "change":   data.get("change"),
        "change_pct": data.get("change_percent"),
        "volume":   data.get("volume"),
        "wri":      resp.headers.get("x-wayforth-wri"),
        "run_id":   RUN_ID,
    }
    print(f"[data-fetch] {SYMBOL}: ${data.get('price')} ({data.get('change_percent')})")
else:
    # OpenWeather: current conditions
    resp = httpx.get(
        "https://gateway.wayforth.io/proxy/openweather",
        headers=HEADERS,
        params={"city": CITY},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    output = {
        "source":    "openweather",
        "city":      data.get("city", CITY),
        "temp_c":    data.get("temp_c"),
        "temp_f":    data.get("temp_f"),
        "condition": data.get("condition"),
        "humidity":  data.get("humidity"),
        "wind_kph":  data.get("wind_kph"),
        "wri":       resp.headers.get("x-wayforth-wri"),
        "run_id":    RUN_ID,
    }
    print(f"[data-fetch] {data.get('city')}: {data.get('temp_c')}°C, {data.get('condition')}")

print(json.dumps(output, indent=2))
print("[data-fetch] done")
'''

_DATAFETCH_TS = '''\
/**
 * Wayforth Cloud Template: Market Data Agent (TypeScript)
 * Fetches current weather or stock data and returns a structured report.
 *
 * Credits: ~3/run (2 OpenWeather or 4 Alpha Vantage + 1 compute)
 * Supported sources: "weather" (default) or "finance"
 */
const KEY    = process.env.WAYFORTH_API_KEY!;
const RUN_ID = process.env.X_WAYFORTH_AGENT_ID ?? \'\';
const SOURCE = (process.env.DATA_SOURCE ?? \'weather\').toLowerCase();
const CITY   = process.env.CITY   ?? \'London\';
const SYMBOL = process.env.SYMBOL ?? \'AAPL\';

const HEADERS = {
  \'X-Wayforth-API-Key\':  KEY,
  \'X-Wayforth-Agent-ID\': RUN_ID,
};

console.log(`[data-fetch] run=${RUN_ID.slice(0, 8)}... source=${SOURCE}`);

let output: Record<string, unknown>;

if (SOURCE === \'finance\') {
  // Alpha Vantage: stock quote
  const url = new URL(\'https://gateway.wayforth.io/proxy/alphavantage\');
  url.searchParams.set(\'symbol\', SYMBOL);
  url.searchParams.set(\'function\', \'GLOBAL_QUOTE\');

  const resp = await fetch(url.toString(), { headers: HEADERS });
  if (!resp.ok) throw new Error(`Alpha Vantage error ${resp.status}: ${await resp.text()}`);
  const data = await resp.json() as Record<string, unknown>;

  output = {
    source:      \'alphavantage\',
    symbol:      SYMBOL,
    price:       data.price,
    change:      data.change,
    change_pct:  data.change_percent,
    volume:      data.volume,
    wri:         resp.headers.get(\'x-wayforth-wri\'),
    run_id:      RUN_ID,
  };
  console.log(`[data-fetch] ${SYMBOL}: $${data.price} (${data.change_percent})`);

} else {
  // OpenWeather: current conditions
  const url = new URL(\'https://gateway.wayforth.io/proxy/openweather\');
  url.searchParams.set(\'city\', CITY);

  const resp = await fetch(url.toString(), { headers: HEADERS });
  if (!resp.ok) throw new Error(`OpenWeather error ${resp.status}: ${await resp.text()}`);
  const data = await resp.json() as {
    city?: string; temp_c?: number; temp_f?: number;
    condition?: string; humidity?: number; wind_kph?: number;
  };

  output = {
    source:    \'openweather\',
    city:      data.city ?? CITY,
    temp_c:    data.temp_c,
    temp_f:    data.temp_f,
    condition: data.condition,
    humidity:  data.humidity,
    wind_kph:  data.wind_kph,
    wri:       resp.headers.get(\'x-wayforth-wri\'),
    run_id:    RUN_ID,
  };
  console.log(`[data-fetch] ${data.city}: ${data.temp_c}°C, ${data.condition}`);
}

console.log(JSON.stringify(output, null, 2));
console.log(\'[data-fetch] done\');
'''

_DATAFETCH_README = """\
# Market Data Agent

Fetches current weather or stock data and returns a structured report.
Switch between data sources with a single env var — no code changes needed.

## Credits per run
| Source | Service | Credits |
|---|---|---|
| Weather (default) | OpenWeather | 2 |
| Finance | Alpha Vantage | 4 |
| Compute | E2B | 1 |
| **Total** | | **3** (weather) or **5** (finance) |

## Configuration (env vars on the agent)
| Variable | Default | Description |
|---|---|---|
| `DATA_SOURCE` | `weather` | `weather` or `finance` |
| `CITY` | `London` | City name for weather (ignored for finance) |
| `SYMBOL` | `AAPL` | Stock ticker for finance (ignored for weather) |

## Output — Weather
```json
{
  "source": "openweather",
  "city": "London",
  "temp_c": 15.3,
  "temp_f": 59.5,
  "condition": "light rain",
  "humidity": 82,
  "wind_kph": 18.4,
  "wri": "96.0",
  "run_id": "..."
}
```

## Output — Finance
```json
{
  "source": "alphavantage",
  "symbol": "AAPL",
  "price": "189.30",
  "change": "+1.50",
  "change_pct": "+0.8000%",
  "volume": "52314000",
  "wri": "92.0",
  "run_id": "..."
}
```

## Customization ideas
- Schedule hourly weather checks and alert on extreme conditions
- Monitor a basket of stocks and alert on threshold moves
- Combine weather + finance for commodity-weather correlation analysis
"""

# ── Registry ──────────────────────────────────────────────────────────────────

TEMPLATES: dict[str, dict] = {
    "research-agent": {
        "id":              "research-agent",
        "name":            "Research Agent",
        "description":     (
            "Searches the web for any topic and returns structured results. "
            "Use for news summarization, competitive research, and information gathering."
        ),
        "category":        "research",
        "default_runtime": "python3.12",
        "runtimes":        ["python3.12", "node20"],
        "services_used":   ["serper"],
        "credits_per_run": {
            "min":       4,
            "max":       4,
            "breakdown": "3 credits Serper + 1 credit compute",
        },
        "env_vars": [
            {
                "name":        "SEARCH_QUERY",
                "description": "Topic or question to research",
                "required":    False,
                "default":     "latest AI agent frameworks 2025",
            },
            {
                "name":        "NUM_RESULTS",
                "description": "Number of results to return (1–10)",
                "required":    False,
                "default":     "5",
            },
        ],
        "tags":   ["search", "research", "web", "serper"],
        "code": {
            "python3.12": _RESEARCH_PY,
            "node20":     _RESEARCH_TS,
        },
        "readme": _RESEARCH_README,
    },

    "translate-notify": {
        "id":              "translate-notify",
        "name":            "Translate-Notify Agent",
        "description":     (
            "Translates text to any language via DeepL, then optionally sends "
            "the result to an email address via Resend. Two-service chain."
        ),
        "category":        "translation",
        "default_runtime": "python3.12",
        "runtimes":        ["python3.12", "node20"],
        "services_used":   ["deepl", "resend"],
        "credits_per_run": {
            "min":       21,
            "max":       24,
            "breakdown": "20 credits DeepL + 3 credits Resend (if NOTIFY_EMAIL set) + 1 compute",
        },
        "env_vars": [
            {
                "name":        "TRANSLATE_TEXT",
                "description": "Text to translate (max 2,000 chars)",
                "required":    False,
                "default":     "Hello, world! Welcome to Wayforth Cloud.",
            },
            {
                "name":        "TARGET_LANG",
                "description": "DeepL target language code (ES, FR, DE, JA, ZH, PT-BR, …)",
                "required":    False,
                "default":     "ES",
            },
            {
                "name":        "NOTIFY_EMAIL",
                "description": "Recipient email address (leave empty to skip email)",
                "required":    False,
                "default":     "",
            },
            {
                "name":        "FROM_EMAIL",
                "description": "Sender address (must be a Resend-verified domain)",
                "required":    False,
                "default":     "noreply@wayforth.io",
            },
        ],
        "tags":   ["translation", "deepl", "resend", "email", "notification"],
        "code": {
            "python3.12": _TRANSLATE_PY,
            "node20":     _TRANSLATE_TS,
        },
        "readme": _TRANSLATE_README,
    },

    "data-fetch": {
        "id":              "data-fetch",
        "name":            "Market Data Agent",
        "description":     (
            "Fetches current weather or stock data and returns a structured report. "
            "Switch between OpenWeather and Alpha Vantage via a single env var."
        ),
        "category":        "data",
        "default_runtime": "python3.12",
        "runtimes":        ["python3.12", "node20"],
        "services_used":   ["openweather", "alphavantage"],
        "credits_per_run": {
            "min":       3,
            "max":       5,
            "breakdown": "2 credits OpenWeather (weather) or 4 credits Alpha Vantage (finance) + 1 compute",
        },
        "env_vars": [
            {
                "name":        "DATA_SOURCE",
                "description": "Data source: 'weather' (OpenWeather) or 'finance' (Alpha Vantage)",
                "required":    False,
                "default":     "weather",
            },
            {
                "name":        "CITY",
                "description": "City name for weather data (e.g. 'London', 'Tokyo', 'New York')",
                "required":    False,
                "default":     "London",
            },
            {
                "name":        "SYMBOL",
                "description": "Stock ticker for finance data (e.g. 'AAPL', 'TSLA', 'MSFT')",
                "required":    False,
                "default":     "AAPL",
            },
        ],
        "tags":   ["weather", "finance", "data", "openweather", "alphavantage", "structured"],
        "code": {
            "python3.12": _DATAFETCH_PY,
            "node20":     _DATAFETCH_TS,
        },
        "readme": _DATAFETCH_README,
    },
}


def get_template(template_id: str) -> dict | None:
    return TEMPLATES.get(template_id)


def list_templates() -> list[dict]:
    """Return template metadata without code (code is large, excluded from list)."""
    out = []
    for t in TEMPLATES.values():
        item = {k: v for k, v in t.items() if k not in ("code", "readme")}
        item["deploy_endpoint"] = f"POST /templates/{t['id']}/deploy"
        item["detail_endpoint"] = f"GET /templates/{t['id']}"
        out.append(item)
    return out
