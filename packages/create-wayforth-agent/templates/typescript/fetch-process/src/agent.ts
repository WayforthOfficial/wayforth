/**
 * {{AGENT_NAME}} — fetch-process agent
 *
 * Discovers the best weather/data service via Wayforth at runtime,
 * fetches structured data via GET /proxy/{slug}?city=...,
 * and transforms it into a human-readable report.
 *
 * Pattern: discovery via SDK search · execution via GET /proxy/{slug}
 */
import { WayforthClient } from 'wayforth-sdk';

const API_KEY = process.env.WAYFORTH_API_KEY ?? '';
if (!API_KEY) {
  console.error('Set WAYFORTH_API_KEY in your environment. Get a key at wayforth.io/signup');
  process.exit(1);
}

const CITY    = process.env.CITY ?? 'London';
const GATEWAY = 'https://gateway.wayforth.io';

const PROXY_CATALOG_SLUGS = new Set([
  'alphavantage', 'assemblyai', 'brave_search', 'deepl', 'elevenlabs', 'firecrawl',
  'gemini', 'groq', 'jina', 'mistral', 'openweather', 'perplexity',
  'resend', 'serper', 'stability', 'tavily', 'together',
]);
const CATALOG_TO_PROXY: Record<string, string> = { brave_search: 'brave' };

async function main() {
  const wf = new WayforthClient({ apiKey: API_KEY });

  // ── 1. DISCOVER ──────────────────────────────────────────────────────────
  console.log('Discovering best weather/data service...');
  const { results } = await wf.search('current weather conditions city', { limit: 10 });
  const candidates = results.filter((r) => r.slug !== null && PROXY_CATALOG_SLUGS.has(r.slug!));
  if (!candidates.length) throw new Error('No proxy-managed data service found.');

  // ── 2. FETCH via proxy (try candidates in WRI order) ────────────────────
  let resp: Response | null = null;
  let usedSlug = '';
  for (const hit of candidates) {
    const slug = hit.slug!;
    console.log(`→ Trying ${hit.name}  WRI=${hit.wri}  slug=${slug}`);
    const r = await fetch(`${GATEWAY}/proxy/${slug}?city=${encodeURIComponent(CITY)}`, {
      headers: { 'X-Wayforth-API-Key': API_KEY },
    });
    if (r.ok) { resp = r; usedSlug = slug; break; }
    console.log(`  ✗ ${slug} returned ${r.status} — trying next service`);
  }
  if (!resp) throw new Error('All discovered services failed. Check your API key and credits.');
  console.log(`\nFetching from /proxy/${usedSlug}?city=${CITY}...`);

  console.log(`[wayforth] failover  : ${resp.headers.get('x-wayforth-failover')}`);
  console.log(`[wayforth] wri       : ${resp.headers.get('x-wayforth-wri')}`);
  console.log(`[wayforth] credits   : ${resp.headers.get('x-wayforth-credits-remaining')} remaining`);

  // ── 3. PROCESS ───────────────────────────────────────────────────────────
  const d = await resp.json() as Record<string, unknown>;
  const tempC = Number(d['temp_c'] ?? 0);
  const feels = tempC > 25 ? 'hot' : tempC > 15 ? 'mild' : 'cold';
  console.log(`\nWeather report — ${d['city'] ?? CITY}:`);
  console.log(`  Temperature : ${d['temp_c']}°C / ${d['temp_f']}°F`);
  console.log(`  Condition   : ${String(d['condition'] ?? '').charAt(0).toUpperCase() + String(d['condition'] ?? '').slice(1)}`);
  console.log(`  Humidity    : ${d['humidity']}%`);
  console.log(`  Wind        : ${d['wind_kph']} km/h`);
  console.log(`  Assessment  : ${feels}`);
}

main().catch((e) => { console.error(e.message); process.exit(1); });
