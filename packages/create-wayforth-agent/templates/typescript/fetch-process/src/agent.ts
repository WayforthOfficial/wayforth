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

const PROXY_SERVICES = new Set([
  'alphavantage', 'assemblyai', 'brave', 'deepl', 'elevenlabs', 'firecrawl',
  'gemini', 'groq', 'jina', 'mistral', 'openweather', 'perplexity',
  'resend', 'serper', 'stability', 'tavily', 'together',
]);

async function main() {
  const wf = new WayforthClient({ apiKey: API_KEY });

  // ── 1. DISCOVER ──────────────────────────────────────────────────────────
  console.log('Discovering best weather/data service...');
  const { results } = await wf.search('current weather conditions city', { limit: 10 });
  const hit = results.find((r) => r.slug !== null && PROXY_SERVICES.has(r.slug!));
  if (!hit?.slug) throw new Error('No proxy-managed data service found.');
  console.log(`→ ${hit.name}  WRI=${hit.wri}  slug=${hit.slug}`);

  // ── 2. FETCH via proxy (GET with query params) ────────────────────────────
  console.log(`\nFetching from /proxy/${hit.slug}?city=${CITY}...`);
  const url  = `${GATEWAY}/proxy/${hit.slug}?city=${encodeURIComponent(CITY)}`;
  const resp = await fetch(url, { headers: { 'X-Wayforth-API-Key': API_KEY } });
  if (!resp.ok) throw new Error(`Proxy returned ${resp.status}: ${await resp.text()}`);

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
