"""services/templates.py — Wayforth Cloud template registry (v0.9.0, 10 templates).

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

# ── Template 4: Topic Monitor ─────────────────────────────────────────────────

_TOPIC_MONITOR_PY = '''\
"""
Wayforth Cloud Template: Topic Monitor (Python)
Searches for any topic via Serper and uses Groq to compile a structured briefing.
Schedule it daily to stay on top of an industry, competitor, or keyword.

Credits: ~7 per run (3 Serper + 3 Groq + 1 compute)
"""
import json
import os

import httpx

KEY    = os.environ["WAYFORTH_API_KEY"]
RUN_ID = os.environ.get("X_WAYFORTH_AGENT_ID", "")
TOPIC  = os.environ.get("TOPIC", "artificial intelligence agents")
NUM    = min(int(os.environ.get("NUM_RESULTS", "5")), 10)
MODEL  = os.environ.get("MODEL", "llama-3.3-70b-versatile")

HDR = {"X-Wayforth-API-Key": KEY, "X-Wayforth-Agent-ID": RUN_ID}
NL  = chr(10)

print(f"[topic-monitor] topic={TOPIC!r} n={NUM} run={RUN_ID[:8]}...")

# Step 1: Web search
sr = httpx.post(
    "https://gateway.wayforth.io/proxy/serper",
    headers=HDR,
    json={"query": TOPIC, "num": NUM},
    timeout=30,
)
sr.raise_for_status()
organic = sr.json().get("organic", [])[:NUM]
print(f"[topic-monitor] serper: {len(organic)} result(s)  wri={sr.headers.get('x-wayforth-wri')}")

context = NL.join(
    f"{i + 1}. {r.get('title', '')}: {r.get('snippet', '')}"
    for i, r in enumerate(organic)
)

# Step 2: Groq summarize into briefing
gr = httpx.post(
    "https://gateway.wayforth.io/proxy/groq",
    headers=HDR,
    json={
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a concise research assistant. Summarize the following "
                    "web search results into 3-5 bullet points. Be factual. "
                    "Cite sources by number in brackets like [1], [2]."
                ),
            },
            {"role": "user", "content": f"Topic: {TOPIC}{NL}{NL}Results:{NL}{context}{NL}{NL}Briefing:"},
        ],
        "model": MODEL,
        "max_tokens": 512,
    },
    timeout=30,
)
gr.raise_for_status()
summary = gr.json().get("content", "")
print(f"[topic-monitor] groq summary: {len(summary)} chars  wri={gr.headers.get('x-wayforth-wri')}")

output = {
    "topic":        TOPIC,
    "result_count": len(organic),
    "summary":      summary,
    "sources": [
        {"n": i + 1, "title": r.get("title", ""), "url": r.get("link", "")}
        for i, r in enumerate(organic)
    ],
    "run_id": RUN_ID,
}
print(json.dumps(output, indent=2))
print("[topic-monitor] done")
'''

_TOPIC_MONITOR_TS = '''\
/**
 * Wayforth Cloud Template: Topic Monitor (TypeScript)
 * Searches for any topic via Serper and uses Groq to compile a structured briefing.
 * Schedule it daily to stay on top of an industry, competitor, or keyword.
 *
 * Credits: ~7 per run (3 Serper + 3 Groq + 1 compute)
 */
const KEY    = process.env.WAYFORTH_API_KEY!;
const RUN_ID = process.env.X_WAYFORTH_AGENT_ID ?? \'\';
const TOPIC  = process.env.TOPIC ?? \'artificial intelligence agents\';
const NUM    = Math.min(parseInt(process.env.NUM_RESULTS ?? \'5\', 10), 10);
const MODEL  = process.env.MODEL ?? \'llama-3.3-70b-versatile\';

const HDR = {
  \'X-Wayforth-API-Key\':  KEY,
  \'X-Wayforth-Agent-ID\': RUN_ID,
  \'Content-Type\':         \'application/json\',
};

console.log(`[topic-monitor] topic=${JSON.stringify(TOPIC)} n=${NUM} run=${RUN_ID.slice(0, 8)}...`);

// Step 1: Web search
const sr = await fetch(\'https://gateway.wayforth.io/proxy/serper\', {
  method: \'POST\', headers: HDR,
  body: JSON.stringify({ query: TOPIC, num: NUM }),
});
if (!sr.ok) throw new Error(`Serper ${sr.status}: ${await sr.text()}`);
const srData = await sr.json() as {
  organic?: Array<{ title?: string; link?: string; snippet?: string }>;
};
const organic = (srData.organic ?? []).slice(0, NUM);
console.log(`[topic-monitor] serper: ${organic.length} result(s)`);

const context = organic.map((r, i) =>
  `${i + 1}. ${r.title ?? \'\'}: ${r.snippet ?? \'\'}`
).join(\'\\n\');

// Step 2: Groq summarize
const gr = await fetch(\'https://gateway.wayforth.io/proxy/groq\', {
  method: \'POST\', headers: HDR,
  body: JSON.stringify({
    messages: [
      {
        role: \'system\',
        content: \'You are a concise research assistant. Summarize the following web search results into 3-5 bullet points. Be factual. Cite sources by number in brackets like [1], [2].\',
      },
      {
        role: \'user\',
        content: `Topic: ${TOPIC}\\n\\nResults:\\n${context}\\n\\nBriefing:`,
      },
    ],
    model: MODEL,
    max_tokens: 512,
  }),
});
if (!gr.ok) throw new Error(`Groq ${gr.status}: ${await gr.text()}`);
const { content: summary = \'\' } = await gr.json() as { content?: string };
console.log(`[topic-monitor] groq summary: ${summary.length} chars`);

const output = {
  topic: TOPIC,
  result_count: organic.length,
  summary,
  sources: organic.map((r, i) => ({ n: i + 1, title: r.title ?? \'\', url: r.link ?? \'\' })),
  run_id: RUN_ID,
};
console.log(JSON.stringify(output, null, 2));
console.log(\'[topic-monitor] done\');
'''

_TOPIC_MONITOR_README = """\
# Topic Monitor

Searches for any topic via Serper and uses Groq to compile a 3-5 bullet
structured briefing. Schedule it daily to track an industry, competitor,
technology trend, or keyword without opening a browser.

## Credits per run
- 3 credits Serper (web search)
- 3 credits Groq (LLM summarization)
- 1 credit compute
- **Total: ~7 credits/run**

## Configuration (env vars on the agent)
| Variable | Default | Description |
|---|---|---|
| `TOPIC` | `artificial intelligence agents` | Topic or query to monitor |
| `NUM_RESULTS` | `5` | Number of search results to read (1–10) |
| `MODEL` | `llama-3.3-70b-versatile` | Groq model to use for summarization |

## Output
```json
{
  "topic": "artificial intelligence agents",
  "result_count": 5,
  "summary": "• LangGraph released v0.2 with improved ...[1]\\n• Wayforth ...[2]",
  "sources": [{ "n": 1, "title": "...", "url": "..." }],
  "run_id": "..."
}
```

## Customization ideas
- Set `TOPIC` to a competitor name, product keyword, or industry term
- Chain the output to Resend for a daily email digest
- Run multiple instances with different topics for a multi-track monitor
"""

# ── Template 5: Content Drafter ───────────────────────────────────────────────

_CONTENT_DRAFTER_PY = '''\
"""
Wayforth Cloud Template: Content Drafter (Python)
Researches a topic with Tavily (deep search + AI answer) then drafts a
long-form piece with inline citations via Groq.

Credits: ~14 per run (10 Tavily + 3 Groq + 1 compute)
"""
import json
import os

import httpx

KEY    = os.environ["WAYFORTH_API_KEY"]
RUN_ID = os.environ.get("X_WAYFORTH_AGENT_ID", "")
TOPIC  = os.environ.get("TOPIC", "how AI agents are changing software development")
WORDS  = int(os.environ.get("WORD_COUNT", "600"))
TONE   = os.environ.get("TONE", "professional")
MODEL  = os.environ.get("MODEL", "llama-3.3-70b-versatile")

HDR = {"X-Wayforth-API-Key": KEY, "X-Wayforth-Agent-ID": RUN_ID}
NL  = chr(10)

print(f"[content-drafter] topic={TOPIC!r} words={WORDS} tone={TONE} run={RUN_ID[:8]}...")

# Step 1: Tavily deep research
tv = httpx.post(
    "https://gateway.wayforth.io/proxy/tavily",
    headers=HDR,
    json={"query": TOPIC, "max_results": 5, "search_depth": "advanced"},
    timeout=45,
)
tv.raise_for_status()
tv_data  = tv.json()
results  = tv_data.get("results", [])
ai_answer = tv_data.get("answer", "")
print(f"[content-drafter] tavily: {len(results)} sources, answer={len(ai_answer)} chars  wri={tv.headers.get('x-wayforth-wri')}")

sources_text = NL.join(
    f"[{i + 1}] {r.get('title', '')}: {r.get('content', '')[:300]}"
    for i, r in enumerate(results)
)

research_block = f"AI Answer: {ai_answer}{NL}{NL}Sources:{NL}{sources_text}" if ai_answer else f"Sources:{NL}{sources_text}"

# Step 2: Draft with Groq
system_msg = (
    f"You are an expert content writer. Write in a {TONE} tone. "
    f"Write approximately {WORDS} words. Use inline citations [1], [2] etc. "
    "Structure with a brief intro, 2-3 body sections, and a conclusion."
)
gr = httpx.post(
    "https://gateway.wayforth.io/proxy/groq",
    headers=HDR,
    json={
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": f"Write a {WORDS}-word {TONE} piece on: {TOPIC}{NL}{NL}Research:{NL}{research_block}"},
        ],
        "model": MODEL,
        "max_tokens": max(WORDS * 2, 1024),
    },
    timeout=45,
)
gr.raise_for_status()
draft = gr.json().get("content", "")
word_count = len(draft.split())
print(f"[content-drafter] draft: {word_count} words  wri={gr.headers.get('x-wayforth-wri')}")

output = {
    "topic":      TOPIC,
    "tone":       TONE,
    "word_count": word_count,
    "draft":      draft,
    "sources": [
        {"n": i + 1, "title": r.get("title", ""), "url": r.get("url", "")}
        for i, r in enumerate(results)
    ],
    "run_id": RUN_ID,
}
print(json.dumps(output, indent=2))
print("[content-drafter] done")
'''

_CONTENT_DRAFTER_TS = '''\
/**
 * Wayforth Cloud Template: Content Drafter (TypeScript)
 * Researches a topic with Tavily (deep search + AI answer) then drafts a
 * long-form piece with inline citations via Groq.
 *
 * Credits: ~14 per run (10 Tavily + 3 Groq + 1 compute)
 */
const KEY   = process.env.WAYFORTH_API_KEY!;
const RUN_ID = process.env.X_WAYFORTH_AGENT_ID ?? \'\';
const TOPIC  = process.env.TOPIC ?? \'how AI agents are changing software development\';
const WORDS  = parseInt(process.env.WORD_COUNT ?? \'600\', 10);
const TONE   = process.env.TONE ?? \'professional\';
const MODEL  = process.env.MODEL ?? \'llama-3.3-70b-versatile\';

const HDR = {
  \'X-Wayforth-API-Key\':  KEY,
  \'X-Wayforth-Agent-ID\': RUN_ID,
  \'Content-Type\':         \'application/json\',
};

console.log(`[content-drafter] topic=${JSON.stringify(TOPIC)} words=${WORDS} run=${RUN_ID.slice(0, 8)}...`);

// Step 1: Tavily deep research
const tv = await fetch(\'https://gateway.wayforth.io/proxy/tavily\', {
  method: \'POST\', headers: HDR,
  body: JSON.stringify({ query: TOPIC, max_results: 5, search_depth: \'advanced\' }),
});
if (!tv.ok) throw new Error(`Tavily ${tv.status}: ${await tv.text()}`);
const tvData = await tv.json() as {
  results?: Array<{ title?: string; url?: string; content?: string }>;
  answer?: string;
};
const results   = tvData.results ?? [];
const aiAnswer  = tvData.answer ?? \'\';
console.log(`[content-drafter] tavily: ${results.length} sources, answer=${aiAnswer.length} chars`);

const sourcesText = results.map((r, i) =>
  `[${i + 1}] ${r.title ?? \'\'}: ${(r.content ?? \'\').slice(0, 300)}`
).join(\'\\n\');
const researchBlock = aiAnswer
  ? `AI Answer: ${aiAnswer}\\n\\nSources:\\n${sourcesText}`
  : `Sources:\\n${sourcesText}`;

// Step 2: Draft with Groq
const gr = await fetch(\'https://gateway.wayforth.io/proxy/groq\', {
  method: \'POST\', headers: HDR,
  body: JSON.stringify({
    messages: [
      {
        role: \'system\',
        content: `You are an expert content writer. Write in a ${TONE} tone. Write approximately ${WORDS} words. Use inline citations [1], [2] etc. Structure with a brief intro, 2-3 body sections, and a conclusion.`,
      },
      {
        role: \'user\',
        content: `Write a ${WORDS}-word ${TONE} piece on: ${TOPIC}\\n\\nResearch:\\n${researchBlock}`,
      },
    ],
    model: MODEL,
    max_tokens: Math.max(WORDS * 2, 1024),
  }),
});
if (!gr.ok) throw new Error(`Groq ${gr.status}: ${await gr.text()}`);
const { content: draft = \'\' } = await gr.json() as { content?: string };
const wordCount = draft.split(/\\s+/).filter(Boolean).length;
console.log(`[content-drafter] draft: ${wordCount} words`);

const output = {
  topic: TOPIC,
  tone: TONE,
  word_count: wordCount,
  draft,
  sources: results.map((r, i) => ({ n: i + 1, title: r.title ?? \'\', url: r.url ?? \'\' })),
  run_id: RUN_ID,
};
console.log(JSON.stringify(output, null, 2));
console.log(\'[content-drafter] done\');
'''

_CONTENT_DRAFTER_README = """\
# Content Drafter

Researches a topic using Tavily's deep search (which returns curated results
plus a pre-synthesized AI answer), then asks Groq to draft a long-form
article with inline citations. Ready to edit and publish.

## Credits per run
- 10 credits Tavily (deep/advanced search)
- 3 credits Groq (LLM drafting)
- 1 credit compute
- **Total: ~14 credits/run**

## Configuration (env vars on the agent)
| Variable | Default | Description |
|---|---|---|
| `TOPIC` | `how AI agents are changing software development` | Article topic or title |
| `WORD_COUNT` | `600` | Target word count for the draft |
| `TONE` | `professional` | Writing tone: `professional`, `casual`, `technical` |
| `MODEL` | `llama-3.3-70b-versatile` | Groq model for drafting |

## Output
```json
{
  "topic": "...",
  "tone": "professional",
  "word_count": 612,
  "draft": "# How AI Agents Are Changing...\\n\\nIn recent months...[1]",
  "sources": [{ "n": 1, "title": "...", "url": "..." }],
  "run_id": "..."
}
```

## Customization ideas
- Set `TONE=technical` for developer documentation
- Chain to Resend to email the draft for human review
- Use `WORD_COUNT=1200` for a full blog post
"""

# ── Template 6: Page Snapshot ─────────────────────────────────────────────────

_PAGE_WATCHER_PY = '''\
"""
Wayforth Cloud Template: Page Snapshot (Python)
Scrapes a URL with Firecrawl, then uses Groq to extract key structured facts
into a snapshot. Does NOT auto-diff between runs — schedule it and compare
the snapshots yourself.

Credits: ~13 per run (6 Firecrawl + 3 Groq + 3 Resend optional + 1 compute)
"""
import json
import os

import httpx

KEY      = os.environ["WAYFORTH_API_KEY"]
RUN_ID   = os.environ.get("X_WAYFORTH_AGENT_ID", "")
PAGE_URL = os.environ.get("PAGE_URL", "https://example.com")
FOCUS    = os.environ.get("EXTRACT_FOCUS", "pricing, features, contact info")
NOTIFY   = os.environ.get("NOTIFY_EMAIL", "")
FROM     = os.environ.get("FROM_EMAIL", "noreply@wayforth.io")
MODEL    = os.environ.get("MODEL", "llama-3.3-70b-versatile")

HDR = {"X-Wayforth-API-Key": KEY, "X-Wayforth-Agent-ID": RUN_ID}

print(f"[page-snapshot] url={PAGE_URL!r} focus={FOCUS!r} run={RUN_ID[:8]}...")

# Step 1: Firecrawl — scrape page to markdown
fc = httpx.post(
    "https://gateway.wayforth.io/proxy/firecrawl",
    headers=HDR,
    json={"url": PAGE_URL, "formats": ["markdown"]},
    timeout=45,
)
fc.raise_for_status()
fc_data  = fc.json()
markdown = fc_data.get("markdown", "")[:6000]
title    = fc_data.get("title", PAGE_URL)
print(f"[page-snapshot] firecrawl: {len(markdown)} chars  title={title!r}  wri={fc.headers.get('x-wayforth-wri')}")

# Step 2: Groq — extract structured snapshot
gr = httpx.post(
    "https://gateway.wayforth.io/proxy/groq",
    headers=HDR,
    json={
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a structured data extractor. Given a web page in markdown, "
                    "extract the requested information as a JSON object. "
                    "If a field is not found, set it to null. Return ONLY valid JSON."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Extract the following from this page: {FOCUS}\\n\\n"
                    f"Page URL: {PAGE_URL}\\nPage title: {title}\\n\\n"
                    f"Page content (markdown):\\n{markdown}\\n\\n"
                    f"Return a JSON object with keys based on the requested fields."
                ),
            },
        ],
        "model": MODEL,
        "max_tokens": 512,
    },
    timeout=30,
)
gr.raise_for_status()
raw_extract = gr.json().get("content", "{}")
print(f"[page-snapshot] groq extract: {len(raw_extract)} chars  wri={gr.headers.get('x-wayforth-wri')}")

try:
    # Strip markdown code fences if the model wrapped the JSON
    clean = raw_extract.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    extracted = json.loads(clean)
except Exception:
    extracted = {"raw": raw_extract}

output = {
    "url":       PAGE_URL,
    "title":     title,
    "focus":     FOCUS,
    "snapshot":  extracted,
    "run_id":    RUN_ID,
}

# Step 3: Optional email notification
if NOTIFY:
    snapshot_text = json.dumps(extracted, indent=2)
    email_resp = httpx.post(
        "https://gateway.wayforth.io/proxy/resend",
        headers=HDR,
        json={
            "from":    FROM,
            "to":      NOTIFY,
            "subject": f"Page snapshot: {title}",
            "html": (
                f"<h2>Page Snapshot</h2>"
                f"<p><strong>URL:</strong> <a href='{PAGE_URL}'>{PAGE_URL}</a></p>"
                f"<p><strong>Focus:</strong> {FOCUS}</p>"
                f"<pre>{snapshot_text}</pre>"
                f"<p><em>Wayforth Cloud · run {RUN_ID[:8]}</em></p>"
            ),
        },
        timeout=30,
    )
    email_resp.raise_for_status()
    output["email_sent"] = True
    output["email_id"]   = email_resp.json().get("email_id")
    print(f"[page-snapshot] email sent to {NOTIFY}")
else:
    output["email_sent"] = False

print(json.dumps(output, indent=2))
print("[page-snapshot] done")
'''

_PAGE_WATCHER_TS = '''\
/**
 * Wayforth Cloud Template: Page Snapshot (TypeScript)
 * Scrapes a URL with Firecrawl, then uses Groq to extract key structured facts
 * into a snapshot. Does NOT auto-diff between runs — schedule it and compare
 * the snapshots yourself.
 *
 * Credits: ~13 per run (6 Firecrawl + 3 Groq + 3 Resend optional + 1 compute)
 */
const KEY      = process.env.WAYFORTH_API_KEY!;
const RUN_ID   = process.env.X_WAYFORTH_AGENT_ID ?? \'\';
const PAGE_URL = process.env.PAGE_URL ?? \'https://example.com\';
const FOCUS    = process.env.EXTRACT_FOCUS ?? \'pricing, features, contact info\';
const NOTIFY   = process.env.NOTIFY_EMAIL ?? \'\';
const FROM     = process.env.FROM_EMAIL ?? \'noreply@wayforth.io\';
const MODEL    = process.env.MODEL ?? \'llama-3.3-70b-versatile\';

const HDR = {
  \'X-Wayforth-API-Key\':  KEY,
  \'X-Wayforth-Agent-ID\': RUN_ID,
  \'Content-Type\':         \'application/json\',
};

console.log(`[page-snapshot] url=${PAGE_URL} focus=${JSON.stringify(FOCUS)} run=${RUN_ID.slice(0, 8)}...`);

// Step 1: Firecrawl — scrape page to markdown
const fc = await fetch(\'https://gateway.wayforth.io/proxy/firecrawl\', {
  method: \'POST\', headers: HDR,
  body: JSON.stringify({ url: PAGE_URL, formats: [\'markdown\'] }),
});
if (!fc.ok) throw new Error(`Firecrawl ${fc.status}: ${await fc.text()}`);
const fcData   = await fc.json() as { markdown?: string; title?: string };
const markdown = (fcData.markdown ?? \'\').slice(0, 6000);
const title    = fcData.title ?? PAGE_URL;
console.log(`[page-snapshot] firecrawl: ${markdown.length} chars  title=${JSON.stringify(title)}`);

// Step 2: Groq — extract structured snapshot
const gr = await fetch(\'https://gateway.wayforth.io/proxy/groq\', {
  method: \'POST\', headers: HDR,
  body: JSON.stringify({
    messages: [
      {
        role: \'system\',
        content: \'You are a structured data extractor. Given a web page in markdown, extract the requested information as a JSON object. If a field is not found, set it to null. Return ONLY valid JSON.\',
      },
      {
        role: \'user\',
        content: `Extract the following from this page: ${FOCUS}\\n\\nPage URL: ${PAGE_URL}\\nPage title: ${title}\\n\\nPage content (markdown):\\n${markdown}\\n\\nReturn a JSON object with keys based on the requested fields.`,
      },
    ],
    model: MODEL,
    max_tokens: 512,
  }),
});
if (!gr.ok) throw new Error(`Groq ${gr.status}: ${await gr.text()}`);
const { content: rawExtract = \'{}\' } = await gr.json() as { content?: string };

let extracted: Record<string, unknown>;
try {
  const clean = rawExtract.trim().replace(/^```json\\s*/, \'\').replace(/^```\\s*/, \'\').replace(/\\s*```$/, \'\').trim();
  extracted = JSON.parse(clean) as Record<string, unknown>;
} catch {
  extracted = { raw: rawExtract };
}

const output: Record<string, unknown> = {
  url: PAGE_URL, title, focus: FOCUS, snapshot: extracted, run_id: RUN_ID,
};

// Step 3: Optional email notification
if (NOTIFY) {
  const snapshotText = JSON.stringify(extracted, null, 2);
  const emailResp = await fetch(\'https://gateway.wayforth.io/proxy/resend\', {
    method: \'POST\', headers: HDR,
    body: JSON.stringify({
      from: FROM, to: NOTIFY,
      subject: `Page snapshot: ${title}`,
      html: `<h2>Page Snapshot</h2><p><strong>URL:</strong> <a href="${PAGE_URL}">${PAGE_URL}</a></p><p><strong>Focus:</strong> ${FOCUS}</p><pre>${snapshotText}</pre><p><em>Wayforth Cloud · run ${RUN_ID.slice(0, 8)}</em></p>`,
    }),
  });
  if (!emailResp.ok) throw new Error(`Resend ${emailResp.status}: ${await emailResp.text()}`);
  const emailData = await emailResp.json() as { email_id?: string };
  output.email_sent = true;
  output.email_id = emailData.email_id;
  console.log(`[page-snapshot] email sent to ${NOTIFY}`);
} else {
  output.email_sent = false;
}

console.log(JSON.stringify(output, null, 2));
console.log(\'[page-snapshot] done\');
'''

_PAGE_WATCHER_README = """\
# Page Snapshot

Scrapes any public URL using Firecrawl (returns clean markdown), then uses
Groq to extract structured facts from the page content. Returns a JSON
snapshot of the fields you specify. Optionally emails the snapshot via Resend.

**This agent does not auto-diff.** It produces one structured snapshot per
run. Schedule it and compare the snapshots yourself — store them, diff them
in your own tooling, or send them to a human for review.

## Credits per run
| Step | Service | Credits |
|---|---|---|
| Scrape | Firecrawl | 6 |
| Extract | Groq | 3 |
| Email (optional) | Resend | 3 |
| Compute | E2B | 1 |
| **Total** | | **10** (no email) or **13** (with email) |

## Configuration (env vars on the agent)
| Variable | Default | Description |
|---|---|---|
| `PAGE_URL` | `https://example.com` | URL to scrape (must be public) |
| `EXTRACT_FOCUS` | `pricing, features, contact info` | Comma-separated fields to extract |
| `NOTIFY_EMAIL` | *(empty — email disabled)* | Recipient for snapshot email |
| `FROM_EMAIL` | `noreply@wayforth.io` | Sender (must be Resend-verified domain) |
| `MODEL` | `llama-3.3-70b-versatile` | Groq model for extraction |

## Output
```json
{
  "url": "https://example.com/pricing",
  "title": "Pricing — Example Co",
  "focus": "pricing, features, contact info",
  "snapshot": {
    "pricing": "Starter $9/mo, Pro $49/mo, Enterprise custom",
    "features": ["Unlimited users", "API access", "Priority support"],
    "contact_info": "support@example.com"
  },
  "email_sent": false,
  "run_id": "..."
}
```

## Customization ideas
- Set `EXTRACT_FOCUS` to `"stock price, P/E ratio, market cap"` for finance pages
- Schedule hourly and store snapshots in your own database for trend analysis
- Chain two Page Snapshot agents on competitor pages and compare outputs weekly
"""

# ── Template 7: Daily Briefing ────────────────────────────────────────────────

_DAILY_BRIEFING_PY = '''\
"""
Wayforth Cloud Template: Daily Briefing (Python)
Pulls weather, a stock quote, and top news, then uses Groq to compile a
morning briefing. Optionally emails it via Resend. Schedule at 0 7 * * *.

Credits: ~16 per run (2 OpenWeather + 4 AlphaVantage + 3 Serper + 3 Groq + 3 Resend opt + 1 compute)
"""
import json
import os

import httpx

KEY     = os.environ["WAYFORTH_API_KEY"]
RUN_ID  = os.environ.get("X_WAYFORTH_AGENT_ID", "")
CITY    = os.environ.get("CITY", "London")
SYMBOL  = os.environ.get("STOCK_SYMBOL", "AAPL")
TOPIC   = os.environ.get("NEWS_TOPIC", "technology")
NOTIFY  = os.environ.get("NOTIFY_EMAIL", "")
FROM    = os.environ.get("FROM_EMAIL", "noreply@wayforth.io")
MODEL   = os.environ.get("MODEL", "llama-3.3-70b-versatile")

HDR = {"X-Wayforth-API-Key": KEY, "X-Wayforth-Agent-ID": RUN_ID}
NL  = chr(10)

print(f"[daily-briefing] city={CITY} symbol={SYMBOL} news={TOPIC!r} run={RUN_ID[:8]}...")

# Step 1: Weather
wr = httpx.get(
    "https://gateway.wayforth.io/proxy/openweather",
    headers=HDR, params={"city": CITY}, timeout=30,
)
wr.raise_for_status()
weather = wr.json()
print(f"[daily-briefing] weather: {weather.get('temp_c')}C {weather.get('condition')}")

# Step 2: Stock quote
ar = httpx.get(
    "https://gateway.wayforth.io/proxy/alphavantage",
    headers=HDR, params={"symbol": SYMBOL}, timeout=30,
)
ar.raise_for_status()
quote = ar.json()
print(f"[daily-briefing] {SYMBOL}: ${quote.get('price')} ({quote.get('change_pct')})")

# Step 3: News headlines
nr = httpx.post(
    "https://gateway.wayforth.io/proxy/serper",
    headers=HDR,
    json={"query": f"{TOPIC} news today", "num": 5},
    timeout=30,
)
nr.raise_for_status()
headlines = nr.json().get("organic", [])[:5]
print(f"[daily-briefing] news: {len(headlines)} headlines")

# Step 4: Groq compile briefing
news_lines = NL.join(f"- {h.get('title','')}" for h in headlines)
summary_prompt = (
    f"Weather in {CITY}: {weather.get('temp_c')}C, {weather.get('condition')}, "
    f"humidity {weather.get('humidity')}%, wind {weather.get('wind_kph')} kph.{NL}"
    f"{SYMBOL}: ${quote.get('price')} ({quote.get('change_pct')}, "
    f"volume {quote.get('volume'):,} if {type(quote.get('volume')) is int else quote.get('volume')}).{NL}"
    f"Top {TOPIC} headlines:{NL}{news_lines}"
)
gr = httpx.post(
    "https://gateway.wayforth.io/proxy/groq",
    headers=HDR,
    json={
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a personal briefing assistant. Given weather, market, and "
                    "news data, write a friendly 3-4 sentence morning briefing. "
                    "Be concise and upbeat."
                ),
            },
            {"role": "user", "content": summary_prompt},
        ],
        "model": MODEL,
        "max_tokens": 256,
    },
    timeout=30,
)
gr.raise_for_status()
briefing = gr.json().get("content", "")
print(f"[daily-briefing] groq briefing: {len(briefing)} chars")

output = {
    "date":    __import__("datetime").date.today().isoformat(),
    "weather": weather,
    "quote":   {"symbol": SYMBOL, **{k: quote.get(k) for k in ("price","change","change_pct","volume")}},
    "headlines": [h.get("title","") for h in headlines],
    "briefing": briefing,
    "email_sent": False,
    "run_id": RUN_ID,
}

# Step 5: Optional email
if NOTIFY:
    hlines_html = "".join(f"<li>{h.get('title','')}</li>" for h in headlines)
    email_resp = httpx.post(
        "https://gateway.wayforth.io/proxy/resend",
        headers=HDR,
        json={
            "from":    FROM,
            "to":      NOTIFY,
            "subject": f"Daily briefing — {output['date']}",
            "html": (
                f"<h2>Good morning!</h2>"
                f"<p>{briefing}</p>"
                f"<hr>"
                f"<p><strong>Weather ({CITY}):</strong> {weather.get('temp_c')}C, {weather.get('condition')}</p>"
                f"<p><strong>{SYMBOL}:</strong> ${quote.get('price')} ({quote.get('change_pct')})</p>"
                f"<p><strong>Top {TOPIC} headlines:</strong></p><ul>{hlines_html}</ul>"
                f"<p><em>Wayforth Cloud · run {RUN_ID[:8]}</em></p>"
            ),
        },
        timeout=30,
    )
    email_resp.raise_for_status()
    output["email_sent"] = True
    output["email_id"]   = email_resp.json().get("email_id")
    print(f"[daily-briefing] email sent to {NOTIFY}")

print(json.dumps(output, indent=2))
print("[daily-briefing] done")
'''

_DAILY_BRIEFING_TS = '''\
/**
 * Wayforth Cloud Template: Daily Briefing (TypeScript)
 * Pulls weather, a stock quote, and top news, then uses Groq to compile a
 * morning briefing. Optionally emails it via Resend. Schedule at "0 7 * * *".
 *
 * Credits: ~16 per run (2 OpenWeather + 4 AlphaVantage + 3 Serper + 3 Groq + 3 Resend opt + 1 compute)
 */
const KEY    = process.env.WAYFORTH_API_KEY!;
const RUN_ID = process.env.X_WAYFORTH_AGENT_ID ?? \'\';
const CITY   = process.env.CITY   ?? \'London\';
const SYMBOL = process.env.STOCK_SYMBOL ?? \'AAPL\';
const TOPIC  = process.env.NEWS_TOPIC   ?? \'technology\';
const NOTIFY = process.env.NOTIFY_EMAIL ?? \'\';
const FROM   = process.env.FROM_EMAIL   ?? \'noreply@wayforth.io\';
const MODEL  = process.env.MODEL        ?? \'llama-3.3-70b-versatile\';

const HDR = {
  \'X-Wayforth-API-Key\':  KEY,
  \'X-Wayforth-Agent-ID\': RUN_ID,
  \'Content-Type\':         \'application/json\',
};

console.log(`[daily-briefing] city=${CITY} symbol=${SYMBOL} news=${TOPIC} run=${RUN_ID.slice(0, 8)}...`);

// Step 1: Weather
const wrUrl = new URL(\'https://gateway.wayforth.io/proxy/openweather\');
wrUrl.searchParams.set(\'city\', CITY);
const wrResp = await fetch(wrUrl.toString(), { headers: HDR });
if (!wrResp.ok) throw new Error(`OpenWeather ${wrResp.status}`);
const weather = await wrResp.json() as {
  city?: string; temp_c?: number; temp_f?: number; condition?: string; humidity?: number; wind_kph?: number;
};
console.log(`[daily-briefing] weather: ${weather.temp_c}C ${weather.condition}`);

// Step 2: Stock quote
const avUrl = new URL(\'https://gateway.wayforth.io/proxy/alphavantage\');
avUrl.searchParams.set(\'symbol\', SYMBOL);
const avResp = await fetch(avUrl.toString(), { headers: HDR });
if (!avResp.ok) throw new Error(`AlphaVantage ${avResp.status}`);
const quote = await avResp.json() as {
  price?: number; change?: number; change_pct?: string; volume?: number;
};
console.log(`[daily-briefing] ${SYMBOL}: $${quote.price} (${quote.change_pct})`);

// Step 3: News headlines
const nrResp = await fetch(\'https://gateway.wayforth.io/proxy/serper\', {
  method: \'POST\', headers: HDR,
  body: JSON.stringify({ query: `${TOPIC} news today`, num: 5 }),
});
if (!nrResp.ok) throw new Error(`Serper ${nrResp.status}`);
const nrData = await nrResp.json() as { organic?: Array<{ title?: string; link?: string }> };
const headlines = (nrData.organic ?? []).slice(0, 5);
console.log(`[daily-briefing] news: ${headlines.length} headlines`);

// Step 4: Groq compile briefing
const newsLines = headlines.map(h => `- ${h.title ?? \'\'}`).join(\'\\n\');
const summaryPrompt = [
  `Weather in ${CITY}: ${weather.temp_c}C, ${weather.condition}, humidity ${weather.humidity}%, wind ${weather.wind_kph} kph.`,
  `${SYMBOL}: $${quote.price} (${quote.change_pct}, volume ${(quote.volume ?? 0).toLocaleString()}).`,
  `Top ${TOPIC} headlines:`,
  newsLines,
].join(\'\\n\');

const grResp = await fetch(\'https://gateway.wayforth.io/proxy/groq\', {
  method: \'POST\', headers: HDR,
  body: JSON.stringify({
    messages: [
      {
        role: \'system\',
        content: \'You are a personal briefing assistant. Given weather, market, and news data, write a friendly 3-4 sentence morning briefing. Be concise and upbeat.\',
      },
      { role: \'user\', content: summaryPrompt },
    ],
    model: MODEL,
    max_tokens: 256,
  }),
});
if (!grResp.ok) throw new Error(`Groq ${grResp.status}`);
const { content: briefing = \'\' } = await grResp.json() as { content?: string };
console.log(`[daily-briefing] groq briefing: ${briefing.length} chars`);

const today = new Date().toISOString().slice(0, 10);
const output: Record<string, unknown> = {
  date: today,
  weather,
  quote: { symbol: SYMBOL, price: quote.price, change: quote.change, change_pct: quote.change_pct, volume: quote.volume },
  headlines: headlines.map(h => h.title ?? \'\'),
  briefing,
  email_sent: false,
  run_id: RUN_ID,
};

// Step 5: Optional email
if (NOTIFY) {
  const hlHtml = headlines.map(h => `<li>${h.title ?? \'\'}</li>`).join(\'\');
  const emailResp = await fetch(\'https://gateway.wayforth.io/proxy/resend\', {
    method: \'POST\', headers: HDR,
    body: JSON.stringify({
      from: FROM, to: NOTIFY,
      subject: `Daily briefing — ${today}`,
      html: `<h2>Good morning!</h2><p>${briefing}</p><hr><p><strong>Weather (${CITY}):</strong> ${weather.temp_c}C, ${weather.condition}</p><p><strong>${SYMBOL}:</strong> $${quote.price} (${quote.change_pct})</p><p><strong>Top ${TOPIC} headlines:</strong></p><ul>${hlHtml}</ul><p><em>Wayforth Cloud · run ${RUN_ID.slice(0, 8)}</em></p>`,
    }),
  });
  if (!emailResp.ok) throw new Error(`Resend ${emailResp.status}: ${await emailResp.text()}`);
  const emailData = await emailResp.json() as { email_id?: string };
  output.email_sent = true;
  output.email_id = emailData.email_id;
  console.log(`[daily-briefing] email sent to ${NOTIFY}`);
}

console.log(JSON.stringify(output, null, 2));
console.log(\'[daily-briefing] done\');
'''

_DAILY_BRIEFING_README = """\
# Daily Briefing

Pulls current weather, a stock quote, and top news headlines, then asks Groq
to compile a friendly 3-4 sentence morning briefing. Optionally emails it
via Resend. Schedule at `0 7 * * *` for a 7am daily delivery.

## Credits per run
| Step | Service | Credits |
|---|---|---|
| Weather | OpenWeather | 2 |
| Stock quote | Alpha Vantage | 4 |
| News | Serper | 3 |
| Compile | Groq | 3 |
| Email (optional) | Resend | 3 |
| Compute | E2B | 1 |
| **Total** | | **13** (no email) or **16** (with email) |

## Configuration (env vars on the agent)
| Variable | Default | Description |
|---|---|---|
| `CITY` | `London` | City for weather data |
| `STOCK_SYMBOL` | `AAPL` | Stock ticker to quote (NYSE/NASDAQ) |
| `NEWS_TOPIC` | `technology` | Topic for news headlines |
| `NOTIFY_EMAIL` | *(empty — email disabled)* | Recipient for daily email |
| `FROM_EMAIL` | `noreply@wayforth.io` | Sender (must be Resend-verified domain) |
| `MODEL` | `llama-3.3-70b-versatile` | Groq model for briefing text |

## Output
```json
{
  "date": "2026-06-15",
  "weather": { "city": "London", "temp_c": 18.2, "condition": "partly cloudy" },
  "quote": { "symbol": "AAPL", "price": 192.5, "change_pct": "+0.8000%" },
  "headlines": ["OpenAI releases ...", "Google announces ..."],
  "briefing": "Good morning! It's a mild 18°C in London today...",
  "email_sent": false,
  "run_id": "..."
}
```

## Customization ideas
- Set `STOCK_SYMBOL=BTC` for crypto data (if supported by Alpha Vantage)
- Set `NEWS_TOPIC=AI startups` to track a niche vertical
- Add multiple email recipients by chaining to a second Resend call
"""

# ── Template 8: URL Summarizer ────────────────────────────────────────────────

_URL_SUMMARIZER_PY = '''\
"""
Wayforth Cloud Template: URL Summarizer (Python)
Reads any URL via Jina Reader (clean markdown) then uses Groq to summarize
it in your chosen style. Works on articles, docs, blog posts, and reports.

Credits: ~8 per run (4 Jina + 3 Groq + 1 compute)
"""
import json
import os

import httpx

KEY    = os.environ["WAYFORTH_API_KEY"]
RUN_ID = os.environ.get("X_WAYFORTH_AGENT_ID", "")
URL    = os.environ.get("URL", "https://openai.com/blog")
STYLE  = os.environ.get("SUMMARY_STYLE", "bullets")
MODEL  = os.environ.get("MODEL", "llama-3.3-70b-versatile")

HDR = {"X-Wayforth-API-Key": KEY, "X-Wayforth-Agent-ID": RUN_ID}
NL  = chr(10)

STYLE_PROMPTS = {
    "bullets":    "Summarize in 5-7 concise bullet points. Each bullet is one key insight.",
    "paragraph":  "Summarize in 2-3 cohesive paragraphs. Preserve the main argument flow.",
    "executive":  "Write a 3-sentence executive summary: one sentence each for context, findings, and action.",
}
style_instruction = STYLE_PROMPTS.get(STYLE, STYLE_PROMPTS["bullets"])

print(f"[url-summarizer] url={URL!r} style={STYLE!r} run={RUN_ID[:8]}...")

# Step 1: Jina Reader — URL to clean markdown
jr = httpx.post(
    "https://gateway.wayforth.io/proxy/jina",
    headers=HDR,
    json={"url": URL},
    timeout=30,
)
jr.raise_for_status()
jr_data = jr.json()
content = jr_data.get("content", "")[:8000]
title   = jr_data.get("title", URL)
print(f"[url-summarizer] jina: {len(content)} chars  title={title!r}  wri={jr.headers.get('x-wayforth-wri')}")

# Step 2: Groq summarize
gr = httpx.post(
    "https://gateway.wayforth.io/proxy/groq",
    headers=HDR,
    json={
        "messages": [
            {"role": "system", "content": style_instruction},
            {"role": "user",   "content": f"Title: {title}{NL}URL: {URL}{NL}{NL}Content:{NL}{content}"},
        ],
        "model": MODEL,
        "max_tokens": 512,
    },
    timeout=30,
)
gr.raise_for_status()
summary = gr.json().get("content", "")
word_count = len(summary.split())
print(f"[url-summarizer] groq summary: {word_count} words  wri={gr.headers.get('x-wayforth-wri')}")

output = {
    "url":        URL,
    "title":      title,
    "style":      STYLE,
    "summary":    summary,
    "word_count": word_count,
    "source_length_chars": len(content),
    "run_id":     RUN_ID,
}
print(json.dumps(output, indent=2))
print("[url-summarizer] done")
'''

_URL_SUMMARIZER_TS = '''\
/**
 * Wayforth Cloud Template: URL Summarizer (TypeScript)
 * Reads any URL via Jina Reader (clean markdown) then uses Groq to summarize
 * it in your chosen style.
 *
 * Credits: ~8 per run (4 Jina + 3 Groq + 1 compute)
 */
const KEY    = process.env.WAYFORTH_API_KEY!;
const RUN_ID = process.env.X_WAYFORTH_AGENT_ID ?? \'\';
const URL    = process.env.URL   ?? \'https://openai.com/blog\';
const STYLE  = process.env.SUMMARY_STYLE ?? \'bullets\';
const MODEL  = process.env.MODEL ?? \'llama-3.3-70b-versatile\';

const HDR = {
  \'X-Wayforth-API-Key\':  KEY,
  \'X-Wayforth-Agent-ID\': RUN_ID,
  \'Content-Type\':         \'application/json\',
};

const STYLE_PROMPTS: Record<string, string> = {
  bullets:   \'Summarize in 5-7 concise bullet points. Each bullet is one key insight.\',
  paragraph: \'Summarize in 2-3 cohesive paragraphs. Preserve the main argument flow.\',
  executive: \'Write a 3-sentence executive summary: one sentence each for context, findings, and action.\',
};
const styleInstruction = STYLE_PROMPTS[STYLE] ?? STYLE_PROMPTS.bullets;

console.log(`[url-summarizer] url=${URL} style=${STYLE} run=${RUN_ID.slice(0, 8)}...`);

// Step 1: Jina Reader — URL to clean markdown
const jr = await fetch(\'https://gateway.wayforth.io/proxy/jina\', {
  method: \'POST\', headers: HDR,
  body: JSON.stringify({ url: URL }),
});
if (!jr.ok) throw new Error(`Jina ${jr.status}: ${await jr.text()}`);
const jrData  = await jr.json() as { content?: string; title?: string };
const content = (jrData.content ?? \'\').slice(0, 8000);
const title   = jrData.title ?? URL;
console.log(`[url-summarizer] jina: ${content.length} chars  title=${JSON.stringify(title)}`);

// Step 2: Groq summarize
const gr = await fetch(\'https://gateway.wayforth.io/proxy/groq\', {
  method: \'POST\', headers: HDR,
  body: JSON.stringify({
    messages: [
      { role: \'system\', content: styleInstruction },
      { role: \'user\',   content: `Title: ${title}\\nURL: ${URL}\\n\\nContent:\\n${content}` },
    ],
    model: MODEL,
    max_tokens: 512,
  }),
});
if (!gr.ok) throw new Error(`Groq ${gr.status}: ${await gr.text()}`);
const { content: summary = \'\' } = await gr.json() as { content?: string };
const wordCount = summary.split(/\\s+/).filter(Boolean).length;
console.log(`[url-summarizer] groq summary: ${wordCount} words`);

const output = {
  url: URL, title, style: STYLE, summary, word_count: wordCount,
  source_length_chars: content.length, run_id: RUN_ID,
};
console.log(JSON.stringify(output, null, 2));
console.log(\'[url-summarizer] done\');
'''

_URL_SUMMARIZER_README = """\
# URL Summarizer

Reads any public URL via Jina Reader (which strips ads and boilerplate and
returns clean markdown), then uses Groq to summarize the content in your
chosen style. Works on articles, blog posts, docs, reports, and landing pages.

## Credits per run
- 4 credits Jina Reader (URL → markdown)
- 3 credits Groq (LLM summarization)
- 1 credit compute
- **Total: ~8 credits/run**

## Configuration (env vars on the agent)
| Variable | Default | Description |
|---|---|---|
| `URL` | `https://openai.com/blog` | Any public URL to summarize |
| `SUMMARY_STYLE` | `bullets` | `bullets`, `paragraph`, or `executive` |
| `MODEL` | `llama-3.3-70b-versatile` | Groq model for summarization |

## Output
```json
{
  "url": "https://example.com/article",
  "title": "Example Article Title",
  "style": "bullets",
  "summary": "• The article covers...\\n• Key finding: ...",
  "word_count": 87,
  "source_length_chars": 4200,
  "run_id": "..."
}
```

## Customization ideas
- `SUMMARY_STYLE=executive` for C-suite briefs
- Chain multiple URL Summarizer runs for a reading list digest
- Combine with Resend to email summaries to a distribution list
"""

# ── Template 9: Company Enricher ──────────────────────────────────────────────

_COMPANY_ENRICHER_PY = '''\
"""
Wayforth Cloud Template: Company Enricher (Python)
Looks up a company name via Brave Search, fetches their homepage with Jina,
and uses Groq to extract a structured company profile. Supply COMPANY_URL
directly to skip Brave and save 6 credits.

Credits: ~14 per run (6 Brave + 4 Jina + 3 Groq + 1 compute)
         ~8 per run  (4 Jina + 3 Groq + 1 compute) if COMPANY_URL is set
"""
import json
import os

import httpx

KEY          = os.environ["WAYFORTH_API_KEY"]
RUN_ID       = os.environ.get("X_WAYFORTH_AGENT_ID", "")
COMPANY_NAME = os.environ.get("COMPANY_NAME", "Stripe")
COMPANY_URL  = os.environ.get("COMPANY_URL", "")
MODEL        = os.environ.get("MODEL", "llama-3.3-70b-versatile")

HDR = {"X-Wayforth-API-Key": KEY, "X-Wayforth-Agent-ID": RUN_ID}
NL  = chr(10)

print(f"[company-enricher] company={COMPANY_NAME!r} url={COMPANY_URL or 'auto'} run={RUN_ID[:8]}...")

homepage_url = COMPANY_URL

# Step 1: Brave search (skip if COMPANY_URL already set)
if not homepage_url:
    bv = httpx.post(
        "https://gateway.wayforth.io/proxy/brave",
        headers=HDR,
        json={"query": f"{COMPANY_NAME} official website", "count": 3},
        timeout=30,
    )
    bv.raise_for_status()
    brave_results = bv.json().get("results", [])
    homepage_url  = brave_results[0]["url"] if brave_results else ""
    print(f"[company-enricher] brave → {homepage_url}  wri={bv.headers.get('x-wayforth-wri')}")

if not homepage_url:
    raise RuntimeError(f"Could not find a URL for {COMPANY_NAME!r}. Set COMPANY_URL explicitly.")

# Step 2: Jina Reader — homepage to markdown
jr = httpx.post(
    "https://gateway.wayforth.io/proxy/jina",
    headers=HDR,
    json={"url": homepage_url},
    timeout=30,
)
jr.raise_for_status()
jr_data = jr.json()
content = jr_data.get("content", "")[:6000]
title   = jr_data.get("title", homepage_url)
print(f"[company-enricher] jina: {len(content)} chars  title={title!r}  wri={jr.headers.get('x-wayforth-wri')}")

# Step 3: Groq extract company profile
gr = httpx.post(
    "https://gateway.wayforth.io/proxy/groq",
    headers=HDR,
    json={
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a company research assistant. Extract a structured company profile "
                    "from the provided web page content. Return ONLY a valid JSON object with these fields: "
                    "name (string), description (string, 1-2 sentences), industry (string), "
                    "products (array of strings, up to 5), headquarters (string or null), "
                    "founded (string or null), size (string: startup/small/medium/large/enterprise or null), "
                    "url (string). If a field is not found, use null."
                ),
            },
            {
                "role": "user",
                "content": f"Company: {COMPANY_NAME}{NL}URL: {homepage_url}{NL}Page title: {title}{NL}{NL}Page content:{NL}{content}",
            },
        ],
        "model": MODEL,
        "max_tokens": 512,
    },
    timeout=30,
)
gr.raise_for_status()
raw = gr.json().get("content", "{}")
print(f"[company-enricher] groq extract: {len(raw)} chars  wri={gr.headers.get('x-wayforth-wri')}")

try:
    clean   = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    profile = json.loads(clean)
except Exception:
    profile = {"raw": raw}

output = {
    "query":   COMPANY_NAME,
    "source":  homepage_url,
    "profile": profile,
    "run_id":  RUN_ID,
}
print(json.dumps(output, indent=2))
print("[company-enricher] done")
'''

_COMPANY_ENRICHER_TS = '''\
/**
 * Wayforth Cloud Template: Company Enricher (TypeScript)
 * Looks up a company via Brave Search, fetches their homepage with Jina,
 * and extracts a structured profile via Groq.
 * Supply COMPANY_URL to skip Brave and save 6 credits.
 *
 * Credits: ~14/run (6 Brave + 4 Jina + 3 Groq + 1 compute)
 *          ~8/run  (4 Jina + 3 Groq + 1 compute) if COMPANY_URL is set
 */
const KEY          = process.env.WAYFORTH_API_KEY!;
const RUN_ID       = process.env.X_WAYFORTH_AGENT_ID ?? \'\';
const COMPANY_NAME = process.env.COMPANY_NAME ?? \'Stripe\';
const COMPANY_URL  = process.env.COMPANY_URL  ?? \'\';
const MODEL        = process.env.MODEL ?? \'llama-3.3-70b-versatile\';

const HDR = {
  \'X-Wayforth-API-Key\':  KEY,
  \'X-Wayforth-Agent-ID\': RUN_ID,
  \'Content-Type\':         \'application/json\',
};

console.log(`[company-enricher] company=${COMPANY_NAME} url=${COMPANY_URL || \'auto\'} run=${RUN_ID.slice(0, 8)}...`);

let homepageUrl = COMPANY_URL;

// Step 1: Brave search (skip if COMPANY_URL already set)
if (!homepageUrl) {
  const bv = await fetch(\'https://gateway.wayforth.io/proxy/brave\', {
    method: \'POST\', headers: HDR,
    body: JSON.stringify({ query: `${COMPANY_NAME} official website`, count: 3 }),
  });
  if (!bv.ok) throw new Error(`Brave ${bv.status}: ${await bv.text()}`);
  const bvData = await bv.json() as { results?: Array<{ url?: string }> };
  homepageUrl = bvData.results?.[0]?.url ?? \'\';
  console.log(`[company-enricher] brave → ${homepageUrl}`);
}
if (!homepageUrl) throw new Error(`Could not find a URL for "${COMPANY_NAME}". Set COMPANY_URL explicitly.`);

// Step 2: Jina Reader — homepage to markdown
const jr = await fetch(\'https://gateway.wayforth.io/proxy/jina\', {
  method: \'POST\', headers: HDR,
  body: JSON.stringify({ url: homepageUrl }),
});
if (!jr.ok) throw new Error(`Jina ${jr.status}: ${await jr.text()}`);
const jrData  = await jr.json() as { content?: string; title?: string };
const content = (jrData.content ?? \'\').slice(0, 6000);
const title   = jrData.title ?? homepageUrl;
console.log(`[company-enricher] jina: ${content.length} chars  title=${JSON.stringify(title)}`);

// Step 3: Groq extract profile
const gr = await fetch(\'https://gateway.wayforth.io/proxy/groq\', {
  method: \'POST\', headers: HDR,
  body: JSON.stringify({
    messages: [
      {
        role: \'system\',
        content: \'You are a company research assistant. Extract a structured company profile from the provided web page. Return ONLY a valid JSON object with: name, description (1-2 sentences), industry, products (array, max 5), headquarters (string or null), founded (string or null), size (startup/small/medium/large/enterprise or null), url.\',
      },
      {
        role: \'user\',
        content: `Company: ${COMPANY_NAME}\\nURL: ${homepageUrl}\\nPage title: ${title}\\n\\nPage content:\\n${content}`,
      },
    ],
    model: MODEL,
    max_tokens: 512,
  }),
});
if (!gr.ok) throw new Error(`Groq ${gr.status}: ${await gr.text()}`);
const { content: raw = \'{}\' } = await gr.json() as { content?: string };

let profile: Record<string, unknown>;
try {
  const clean = raw.trim().replace(/^```json\\s*/, \'\').replace(/^```\\s*/, \'\').replace(/\\s*```$/, \'\').trim();
  profile = JSON.parse(clean) as Record<string, unknown>;
} catch {
  profile = { raw };
}

const output = { query: COMPANY_NAME, source: homepageUrl, profile, run_id: RUN_ID };
console.log(JSON.stringify(output, null, 2));
console.log(\'[company-enricher] done\');
'''

_COMPANY_ENRICHER_README = """\
# Company Enricher

Looks up a company name with Brave Search, fetches their homepage using Jina
Reader (clean markdown), and uses Groq to extract a structured company profile.
Use it to enrich a CRM, research prospects, or build company databases.

Provide `COMPANY_URL` to skip the Brave search step and save 6 credits.

## Credits per run
| Path | Services | Credits |
|---|---|---|
| Name only (auto-discover URL) | Brave + Jina + Groq + compute | **~14** |
| URL provided (`COMPANY_URL` set) | Jina + Groq + compute | **~8** |

## Configuration (env vars on the agent)
| Variable | Default | Description |
|---|---|---|
| `COMPANY_NAME` | `Stripe` | Company to research |
| `COMPANY_URL` | *(empty — auto-discover via Brave)* | Homepage URL to fetch directly |
| `MODEL` | `llama-3.3-70b-versatile` | Groq model for extraction |

## Output
```json
{
  "query": "Stripe",
  "source": "https://stripe.com",
  "profile": {
    "name": "Stripe",
    "description": "Stripe is a financial infrastructure platform for businesses...",
    "industry": "Fintech / Payments",
    "products": ["Stripe Payments", "Stripe Connect", "Stripe Billing", "Radar", "Atlas"],
    "headquarters": "San Francisco, CA",
    "founded": "2010",
    "size": "large",
    "url": "https://stripe.com"
  },
  "run_id": "..."
}
```

## Customization ideas
- Loop over a list of company names by running multiple agents in parallel
- Combine with AlphaVantage for public companies to add stock data
- Chain to Resend to email enriched profiles to your sales team
"""

# ── Template 10: Image Generator ─────────────────────────────────────────────

_IMAGE_GENERATOR_PY = '''\
"""
Wayforth Cloud Template: Image Generator (Python)
Optionally refines the prompt with Groq, then generates an image via
Stability AI and returns base64 output with metadata.

Credits: ~87-90 per run (86 Stability + 3 Groq optional + 1 compute)
"""
import json
import os

import httpx

KEY    = os.environ["WAYFORTH_API_KEY"]
RUN_ID = os.environ.get("X_WAYFORTH_AGENT_ID", "")
PROMPT = os.environ.get("IMAGE_PROMPT", "a futuristic city skyline at dusk, photorealistic")
QUALITY       = os.environ.get("QUALITY", "core")
ASPECT_RATIO  = os.environ.get("ASPECT_RATIO", "1:1")
ENHANCE       = os.environ.get("ENHANCE_PROMPT", "false").lower() == "true"
ENHANCE_MODEL = os.environ.get("MODEL", "llama-3.3-70b-versatile")

HDR = {"X-Wayforth-API-Key": KEY, "X-Wayforth-Agent-ID": RUN_ID}
NL  = chr(10)

print(f"[image-generator] prompt={PROMPT[:60]!r} quality={QUALITY} enhance={ENHANCE} run={RUN_ID[:8]}...")

final_prompt = PROMPT

# Step 1: Optional Groq prompt enhancement
if ENHANCE:
    gr = httpx.post(
        "https://gateway.wayforth.io/proxy/groq",
        headers=HDR,
        json={
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an expert at writing Stable Diffusion prompts. "
                        "Expand the user's prompt into a detailed, vivid image generation prompt "
                        "with artistic style, lighting, and composition details. "
                        "Keep it under 200 words. Return ONLY the enhanced prompt, no explanation."
                    ),
                },
                {"role": "user", "content": PROMPT},
            ],
            "model": ENHANCE_MODEL,
            "max_tokens": 256,
        },
        timeout=30,
    )
    gr.raise_for_status()
    final_prompt = gr.json().get("content", PROMPT).strip()
    print(f"[image-generator] groq enhanced prompt: {len(final_prompt)} chars  wri={gr.headers.get('x-wayforth-wri')}")

# Step 2: Stability AI image generation
payload = {"prompt": final_prompt, "quality": QUALITY}
if QUALITY == "ultra":
    payload["aspect_ratio"] = ASPECT_RATIO
else:
    # core (SDXL v1): width/height must be 1024x1024 for 1:1
    payload["width"]  = 1024
    payload["height"] = 1024

im = httpx.post(
    "https://gateway.wayforth.io/proxy/stability",
    headers=HDR,
    json=payload,
    timeout=60,
)
im.raise_for_status()
im_data = im.json()
image_b64 = im_data.get("image_base64", "")
image_bytes = len(image_b64) * 3 // 4

print(f"[image-generator] stability: {image_bytes:,} bytes  seed={im_data.get('seed')}  finish={im_data.get('finish_reason')}  wri={im.headers.get('x-wayforth-wri')}")

output = {
    "original_prompt": PROMPT,
    "final_prompt":    final_prompt,
    "prompt_enhanced": ENHANCE,
    "quality":         QUALITY,
    "seed":            im_data.get("seed"),
    "finish_reason":   im_data.get("finish_reason"),
    "image_size_bytes": image_bytes,
    "image_preview":   image_b64[:80] + "...",
    "image_base64":    image_b64,
    "run_id":          RUN_ID,
}

# Print metadata only (not the full base64 — can be 200KB+)
meta = {k: v for k, v in output.items() if k != "image_base64"}
print(json.dumps(meta, indent=2))
print(f"[image-generator] done — image_base64 length={len(image_b64)} chars (omitted from log)")
'''

_IMAGE_GENERATOR_TS = '''\
/**
 * Wayforth Cloud Template: Image Generator (TypeScript)
 * Optionally refines the prompt with Groq, then generates an image via
 * Stability AI and returns base64 output with metadata.
 *
 * Credits: ~87-90 per run (86 Stability + 3 Groq optional + 1 compute)
 */
const KEY          = process.env.WAYFORTH_API_KEY!;
const RUN_ID       = process.env.X_WAYFORTH_AGENT_ID ?? \'\';
const PROMPT       = process.env.IMAGE_PROMPT ?? \'a futuristic city skyline at dusk, photorealistic\';
const QUALITY      = process.env.QUALITY      ?? \'core\';
const ASPECT_RATIO = process.env.ASPECT_RATIO ?? \'1:1\';
const ENHANCE      = (process.env.ENHANCE_PROMPT ?? \'false\').toLowerCase() === \'true\';
const ENH_MODEL    = process.env.MODEL ?? \'llama-3.3-70b-versatile\';

const HDR = {
  \'X-Wayforth-API-Key\':  KEY,
  \'X-Wayforth-Agent-ID\': RUN_ID,
  \'Content-Type\':         \'application/json\',
};

console.log(`[image-generator] prompt=${JSON.stringify(PROMPT.slice(0, 60))} quality=${QUALITY} enhance=${ENHANCE} run=${RUN_ID.slice(0, 8)}...`);

let finalPrompt = PROMPT;

// Step 1: Optional Groq prompt enhancement
if (ENHANCE) {
  const gr = await fetch(\'https://gateway.wayforth.io/proxy/groq\', {
    method: \'POST\', headers: HDR,
    body: JSON.stringify({
      messages: [
        {
          role: \'system\',
          content: \'You are an expert at writing Stable Diffusion prompts. Expand the user\'s prompt into a detailed, vivid image generation prompt with artistic style, lighting, and composition details. Keep it under 200 words. Return ONLY the enhanced prompt, no explanation.\',
        },
        { role: \'user\', content: PROMPT },
      ],
      model: ENH_MODEL,
      max_tokens: 256,
    }),
  });
  if (!gr.ok) throw new Error(`Groq ${gr.status}: ${await gr.text()}`);
  const { content: enhanced = \'\' } = await gr.json() as { content?: string };
  finalPrompt = enhanced.trim() || PROMPT;
  console.log(`[image-generator] groq enhanced prompt: ${finalPrompt.length} chars`);
}

// Step 2: Stability AI image generation
const imPayload: Record<string, unknown> = { prompt: finalPrompt, quality: QUALITY };
if (QUALITY === \'ultra\') {
  imPayload.aspect_ratio = ASPECT_RATIO;
} else {
  imPayload.width  = 1024;
  imPayload.height = 1024;
}

const im = await fetch(\'https://gateway.wayforth.io/proxy/stability\', {
  method: \'POST\', headers: HDR,
  body: JSON.stringify(imPayload),
});
if (!im.ok) throw new Error(`Stability ${im.status}: ${await im.text()}`);
const imData = await im.json() as {
  image_base64?: string; seed?: number; finish_reason?: string; quality?: string;
};
const imageB64    = imData.image_base64 ?? \'\';
const imageBytes  = Math.floor(imageB64.length * 3 / 4);
console.log(`[image-generator] stability: ${imageBytes.toLocaleString()} bytes  seed=${imData.seed}  finish=${imData.finish_reason}`);

const output = {
  original_prompt:  PROMPT,
  final_prompt:     finalPrompt,
  prompt_enhanced:  ENHANCE,
  quality:          QUALITY,
  seed:             imData.seed,
  finish_reason:    imData.finish_reason,
  image_size_bytes: imageBytes,
  image_preview:    imageB64.slice(0, 80) + \'...\',
  image_base64:     imageB64,
  run_id:           RUN_ID,
};

const { image_base64: _omit, ...meta } = output;
console.log(JSON.stringify(meta, null, 2));
console.log(`[image-generator] done — image_base64 length=${imageB64.length} chars (omitted from log)`);
'''

_IMAGE_GENERATOR_README = """\
# Image Generator

Generates an image from a text prompt using Stability AI (SDXL for `core`
quality, v2beta Ultra for `ultra` quality). Optionally uses Groq to enhance
the prompt before generation. Returns the image as base64 in the run output.

## Credits per run
| Step | Service | Credits |
|---|---|---|
| Prompt enhancement (optional) | Groq | 3 |
| Image generation | Stability AI | 86 |
| Compute | E2B | 1 |
| **Total** | | **87** (no enhancement) or **90** (with enhancement) |

## Configuration (env vars on the agent)
| Variable | Default | Description |
|---|---|---|
| `IMAGE_PROMPT` | `a futuristic city skyline at dusk, photorealistic` | Text prompt for the image |
| `QUALITY` | `core` | `core` (SDXL, 1024×1024) or `ultra` (v2beta, higher quality) |
| `ASPECT_RATIO` | `1:1` | For `ultra` only: `1:1`, `16:9`, `9:16`, `4:3`, `3:4` |
| `ENHANCE_PROMPT` | `false` | `true` to use Groq to expand the prompt before generation |
| `MODEL` | `llama-3.3-70b-versatile` | Groq model for prompt enhancement |

## Output
```json
{
  "original_prompt": "a futuristic city skyline at dusk",
  "final_prompt": "A sweeping panoramic view of...",
  "prompt_enhanced": true,
  "quality": "core",
  "seed": 29517843,
  "finish_reason": "SUCCESS",
  "image_size_bytes": 198432,
  "image_preview": "iVBORw0KGgoAAAANS...",
  "image_base64": "<full base64 JPEG>",
  "run_id": "..."
}
```

## Customization ideas
- `QUALITY=ultra` for higher-quality outputs (same Stability credit cost)
- `ENHANCE_PROMPT=true` for abstract or short prompts that benefit from expansion
- Store `image_base64` in your own storage (S3, R2, Supabase) after the run
- Chain multiple runs for product mockups, concept art, or batch generation
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

    "topic-monitor": {
        "id":              "topic-monitor",
        "name":            "Topic Monitor",
        "description":     (
            "Searches the web for any topic via Serper and uses Groq to compile "
            "a structured 3-5 bullet briefing with cited sources. "
            "Schedule it daily to track an industry, competitor, or keyword."
        ),
        "category":        "research",
        "default_runtime": "python3.12",
        "runtimes":        ["python3.12", "node20"],
        "services_used":   ["serper", "groq"],
        "credits_per_run": {
            "min":       7,
            "max":       7,
            "breakdown": "3 credits Serper + 3 credits Groq + 1 compute",
        },
        "env_vars": [
            {
                "name":        "TOPIC",
                "description": "Topic or keyword to monitor",
                "required":    False,
                "default":     "artificial intelligence agents",
            },
            {
                "name":        "NUM_RESULTS",
                "description": "Number of search results to read (1-10)",
                "required":    False,
                "default":     "5",
            },
            {
                "name":        "MODEL",
                "description": "Groq model for summarization",
                "required":    False,
                "default":     "llama-3.3-70b-versatile",
            },
        ],
        "tags":   ["research", "monitoring", "serper", "groq", "briefing", "scheduled"],
        "code": {
            "python3.12": _TOPIC_MONITOR_PY,
            "node20":     _TOPIC_MONITOR_TS,
        },
        "readme": _TOPIC_MONITOR_README,
    },

    "content-drafter": {
        "id":              "content-drafter",
        "name":            "Content Drafter",
        "description":     (
            "Researches a topic with Tavily deep search, then drafts a long-form "
            "article with inline citations via Groq. "
            "Ready to edit and publish. Set WORD_COUNT and TONE via env vars."
        ),
        "category":        "content",
        "default_runtime": "python3.12",
        "runtimes":        ["python3.12", "node20"],
        "services_used":   ["tavily", "groq"],
        "credits_per_run": {
            "min":       14,
            "max":       14,
            "breakdown": "10 credits Tavily + 3 credits Groq + 1 compute",
        },
        "env_vars": [
            {
                "name":        "TOPIC",
                "description": "Article topic or title",
                "required":    False,
                "default":     "how AI agents are changing software development",
            },
            {
                "name":        "WORD_COUNT",
                "description": "Target word count for the draft",
                "required":    False,
                "default":     "600",
            },
            {
                "name":        "TONE",
                "description": "Writing tone: professional, casual, or technical",
                "required":    False,
                "default":     "professional",
            },
            {
                "name":        "MODEL",
                "description": "Groq model for drafting",
                "required":    False,
                "default":     "llama-3.3-70b-versatile",
            },
        ],
        "tags":   ["content", "writing", "tavily", "groq", "blog", "research"],
        "code": {
            "python3.12": _CONTENT_DRAFTER_PY,
            "node20":     _CONTENT_DRAFTER_TS,
        },
        "readme": _CONTENT_DRAFTER_README,
    },

    "page-watcher": {
        "id":              "page-watcher",
        "name":            "Page Snapshot",
        "description":     (
            "Scrapes any URL with Firecrawl and uses Groq to extract key structured "
            "facts into a JSON snapshot per run. Schedule it and compare the snapshots "
            "yourself — no persistent diff state stored."
        ),
        "category":        "data",
        "default_runtime": "python3.12",
        "runtimes":        ["python3.12", "node20"],
        "services_used":   ["firecrawl", "groq", "resend"],
        "credits_per_run": {
            "min":       10,
            "max":       13,
            "breakdown": "6 credits Firecrawl + 3 credits Groq + 3 credits Resend (optional) + 1 compute",
        },
        "env_vars": [
            {
                "name":        "PAGE_URL",
                "description": "URL to scrape (must be public)",
                "required":    True,
                "default":     "https://example.com",
            },
            {
                "name":        "EXTRACT_FOCUS",
                "description": "Comma-separated fields to extract from the page",
                "required":    False,
                "default":     "pricing, features, contact info",
            },
            {
                "name":        "NOTIFY_EMAIL",
                "description": "Recipient email for snapshot (leave empty to skip)",
                "required":    False,
                "default":     "",
            },
            {
                "name":        "FROM_EMAIL",
                "description": "Sender address (must be Resend-verified domain)",
                "required":    False,
                "default":     "noreply@wayforth.io",
            },
            {
                "name":        "MODEL",
                "description": "Groq model for extraction",
                "required":    False,
                "default":     "llama-3.3-70b-versatile",
            },
        ],
        "tags":   ["scraping", "monitoring", "firecrawl", "groq", "resend", "structured-data"],
        "code": {
            "python3.12": _PAGE_WATCHER_PY,
            "node20":     _PAGE_WATCHER_TS,
        },
        "readme": _PAGE_WATCHER_README,
    },

    "daily-briefing": {
        "id":              "daily-briefing",
        "name":            "Daily Briefing",
        "description":     (
            "Pulls current weather, a stock quote, and top news headlines, then "
            "compiles a friendly morning briefing via Groq. Optionally emails it "
            "via Resend. Schedule at 0 7 * * * for daily delivery."
        ),
        "category":        "productivity",
        "default_runtime": "python3.12",
        "runtimes":        ["python3.12", "node20"],
        "services_used":   ["openweather", "alphavantage", "serper", "groq", "resend"],
        "credits_per_run": {
            "min":       13,
            "max":       16,
            "breakdown": "2 OpenWeather + 4 Alpha Vantage + 3 Serper + 3 Groq + 3 Resend (optional) + 1 compute",
        },
        "env_vars": [
            {
                "name":        "CITY",
                "description": "City for weather data",
                "required":    False,
                "default":     "London",
            },
            {
                "name":        "STOCK_SYMBOL",
                "description": "Stock ticker to quote (e.g. AAPL, TSLA)",
                "required":    False,
                "default":     "AAPL",
            },
            {
                "name":        "NEWS_TOPIC",
                "description": "Topic for news headlines",
                "required":    False,
                "default":     "technology",
            },
            {
                "name":        "NOTIFY_EMAIL",
                "description": "Recipient email for daily briefing (leave empty to skip)",
                "required":    False,
                "default":     "",
            },
            {
                "name":        "FROM_EMAIL",
                "description": "Sender address (must be Resend-verified domain)",
                "required":    False,
                "default":     "noreply@wayforth.io",
            },
            {
                "name":        "MODEL",
                "description": "Groq model for briefing text",
                "required":    False,
                "default":     "llama-3.3-70b-versatile",
            },
        ],
        "tags":   ["briefing", "weather", "finance", "news", "email", "scheduled", "productivity"],
        "code": {
            "python3.12": _DAILY_BRIEFING_PY,
            "node20":     _DAILY_BRIEFING_TS,
        },
        "readme": _DAILY_BRIEFING_README,
    },

    "url-summarizer": {
        "id":              "url-summarizer",
        "name":            "URL Summarizer",
        "description":     (
            "Reads any public URL via Jina Reader (clean markdown) and summarizes "
            "the content via Groq in bullets, paragraph, or executive style. "
            "Works on articles, docs, blog posts, and reports."
        ),
        "category":        "research",
        "default_runtime": "python3.12",
        "runtimes":        ["python3.12", "node20"],
        "services_used":   ["jina", "groq"],
        "credits_per_run": {
            "min":       8,
            "max":       8,
            "breakdown": "4 credits Jina + 3 credits Groq + 1 compute",
        },
        "env_vars": [
            {
                "name":        "URL",
                "description": "Any public URL to summarize",
                "required":    True,
                "default":     "https://openai.com/blog",
            },
            {
                "name":        "SUMMARY_STYLE",
                "description": "bullets, paragraph, or executive",
                "required":    False,
                "default":     "bullets",
            },
            {
                "name":        "MODEL",
                "description": "Groq model for summarization",
                "required":    False,
                "default":     "llama-3.3-70b-versatile",
            },
        ],
        "tags":   ["summarization", "reading", "jina", "groq", "research", "content"],
        "code": {
            "python3.12": _URL_SUMMARIZER_PY,
            "node20":     _URL_SUMMARIZER_TS,
        },
        "readme": _URL_SUMMARIZER_README,
    },

    "company-enricher": {
        "id":              "company-enricher",
        "name":            "Company Enricher",
        "description":     (
            "Looks up a company with Brave Search, reads their homepage via Jina, "
            "and extracts a structured profile (industry, products, HQ, size) via Groq. "
            "Supply COMPANY_URL directly to skip Brave and save 6 credits."
        ),
        "category":        "research",
        "default_runtime": "python3.12",
        "runtimes":        ["python3.12", "node20"],
        "services_used":   ["brave", "jina", "groq"],
        "credits_per_run": {
            "min":       8,
            "max":       14,
            "breakdown": "6 credits Brave (if auto-discover) + 4 credits Jina + 3 credits Groq + 1 compute",
        },
        "env_vars": [
            {
                "name":        "COMPANY_NAME",
                "description": "Company to research",
                "required":    True,
                "default":     "Stripe",
            },
            {
                "name":        "COMPANY_URL",
                "description": "Homepage URL (leave empty to auto-discover via Brave)",
                "required":    False,
                "default":     "",
            },
            {
                "name":        "MODEL",
                "description": "Groq model for profile extraction",
                "required":    False,
                "default":     "llama-3.3-70b-versatile",
            },
        ],
        "tags":   ["enrichment", "research", "crm", "brave", "jina", "groq", "company"],
        "code": {
            "python3.12": _COMPANY_ENRICHER_PY,
            "node20":     _COMPANY_ENRICHER_TS,
        },
        "readme": _COMPANY_ENRICHER_README,
    },

    "image-generator": {
        "id":              "image-generator",
        "name":            "Image Generator",
        "description":     (
            "Generates an image from a text prompt using Stability AI "
            "(SDXL core or Ultra). Optionally enhances the prompt with Groq first. "
            "Returns the image as base64 in the run output."
        ),
        "category":        "creative",
        "default_runtime": "python3.12",
        "runtimes":        ["python3.12", "node20"],
        "services_used":   ["stability", "groq"],
        "credits_per_run": {
            "min":       87,
            "max":       90,
            "breakdown": "86 credits Stability AI + 3 credits Groq (optional enhance) + 1 compute",
        },
        "env_vars": [
            {
                "name":        "IMAGE_PROMPT",
                "description": "Text prompt for image generation",
                "required":    True,
                "default":     "a futuristic city skyline at dusk, photorealistic",
            },
            {
                "name":        "QUALITY",
                "description": "core (SDXL, 1024×1024) or ultra (v2beta, higher quality)",
                "required":    False,
                "default":     "core",
            },
            {
                "name":        "ASPECT_RATIO",
                "description": "For ultra only: 1:1, 16:9, 9:16, 4:3, 3:4",
                "required":    False,
                "default":     "1:1",
            },
            {
                "name":        "ENHANCE_PROMPT",
                "description": "true to expand the prompt with Groq before generation",
                "required":    False,
                "default":     "false",
            },
            {
                "name":        "MODEL",
                "description": "Groq model for prompt enhancement (only used if ENHANCE_PROMPT=true)",
                "required":    False,
                "default":     "llama-3.3-70b-versatile",
            },
        ],
        "tags":   ["image", "generation", "stability", "groq", "creative", "ai-art"],
        "code": {
            "python3.12": _IMAGE_GENERATOR_PY,
            "node20":     _IMAGE_GENERATOR_TS,
        },
        "readme": _IMAGE_GENERATOR_README,
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
