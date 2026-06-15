/**
 * {{AGENT_NAME}} — search-summarize agent
 *
 * Discovers the best available search service via Wayforth at runtime,
 * executes through the proxy (native upstream shape + automatic failover),
 * and prints top results.
 *
 * Pattern: discovery via SDK search · execution via /proxy/{slug}
 */
import { WayforthClient } from 'wayforth-sdk';

const API_KEY = process.env.WAYFORTH_API_KEY ?? '';
if (!API_KEY) {
  console.error('Set WAYFORTH_API_KEY in your environment. Get a key at wayforth.io/signup');
  process.exit(1);
}

const QUERY = 'latest developments in AI agents';
const GATEWAY = 'https://gateway.wayforth.io';

// Slugs Wayforth currently runs as a managed proxy.
// Run `wayforth services --tier 3` to see the current list.
const PROXY_SERVICES = new Set([
  'alphavantage', 'assemblyai', 'brave', 'deepl', 'elevenlabs', 'firecrawl',
  'gemini', 'groq', 'jina', 'mistral', 'openweather', 'perplexity',
  'resend', 'serper', 'stability', 'tavily', 'together',
]);

async function main() {
  const wf = new WayforthClient({ apiKey: API_KEY });

  // ── 1. DISCOVER ──────────────────────────────────────────────────────────
  console.log('Discovering best search service...');
  const { results } = await wf.search('web search recent results', { limit: 10 });
  const hit = results.find((r) => r.slug !== null && PROXY_SERVICES.has(r.slug!));
  if (!hit?.slug) throw new Error('No proxy-managed search service found in discovery results.');
  console.log(`→ ${hit.name}  WRI=${hit.wri}  slug=${hit.slug}`);

  // ── 2. EXECUTE ───────────────────────────────────────────────────────────
  console.log(`\nCalling /proxy/${hit.slug}...`);
  const resp = await fetch(`${GATEWAY}/proxy/${hit.slug}`, {
    method: 'POST',
    headers: { 'X-Wayforth-API-Key': API_KEY, 'Content-Type': 'application/json' },
    body: JSON.stringify({ query: QUERY }),
  });
  if (!resp.ok) throw new Error(`Proxy returned ${resp.status}: ${await resp.text()}`);

  console.log(`[wayforth] failover  : ${resp.headers.get('x-wayforth-failover')}`);
  console.log(`[wayforth] wri       : ${resp.headers.get('x-wayforth-wri')}`);
  console.log(`[wayforth] credits   : ${resp.headers.get('x-wayforth-credits-remaining')} remaining`);

  // ── 3. PROCESS ───────────────────────────────────────────────────────────
  // Native upstream shape — no Wayforth envelope
  const data = await resp.json() as Record<string, unknown>;
  const organic = (data['organic'] ?? data['results'] ?? []) as Array<Record<string, string>>;
  console.log(`\nTop results for '${QUERY}':`);
  for (const item of organic.slice(0, 3)) {
    console.log(`  · ${item['title'] ?? item['name'] ?? ''}`);
    const url = item['link'] ?? item['url'] ?? '';
    if (url) console.log(`    ${url}`);
    const snippet = (item['snippet'] ?? '').slice(0, 120);
    if (snippet) console.log(`    ${snippet}...`);
    console.log();
  }
}

main().catch((e) => { console.error(e.message); process.exit(1); });
