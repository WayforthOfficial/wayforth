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

// Catalog slugs returned by /search that correspond to Wayforth managed proxies.
// Run `wayforth services --tier 3` to see the current list.
const PROXY_CATALOG_SLUGS = new Set([
  'alphavantage', 'assemblyai', 'brave_search', 'deepl', 'elevenlabs', 'firecrawl',
  'gemini', 'groq', 'jina', 'mistral', 'openweather', 'perplexity',
  'resend', 'serper', 'stability', 'tavily', 'together',
]);

// Some catalog slugs differ from the proxy endpoint slug.
const CATALOG_TO_PROXY: Record<string, string> = { brave_search: 'brave' };

async function main() {
  const wf = new WayforthClient({ apiKey: API_KEY });

  // ── 1. DISCOVER ──────────────────────────────────────────────────────────
  console.log('Discovering best search service...');
  const { results } = await wf.search('web search recent results', { limit: 10 });
  const candidates = results.filter((r) => r.slug !== null && PROXY_CATALOG_SLUGS.has(r.slug!));
  if (!candidates.length) throw new Error('No proxy-managed search service found in discovery results.');

  // ── 2. EXECUTE (try candidates in ranked order) ───────────────────────
  let resp: Response | null = null;
  for (const hit of candidates) {
    const catalogSlug = hit.slug!;
    const proxySlug = CATALOG_TO_PROXY[catalogSlug] ?? catalogSlug;
    console.log(`→ Trying ${hit.name}  WRI=${hit.wri}  slug=${proxySlug}`);
    const r = await fetch(`${GATEWAY}/proxy/${proxySlug}`, {
      method: 'POST',
      headers: { 'X-Wayforth-API-Key': API_KEY, 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: QUERY }),
    });
    if (r.ok) { resp = r; break; }
    console.log(`  ✗ ${proxySlug} returned ${r.status} — trying next service`);
  }
  if (!resp) throw new Error('All discovered services failed. Check your API key and credits.');

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
