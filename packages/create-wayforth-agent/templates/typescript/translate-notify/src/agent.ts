/**
 * {{AGENT_NAME}} — translate-notify agent
 *
 * Discovers the best translation service via Wayforth at runtime,
 * translates text through the proxy (native upstream shape + failover),
 * and optionally sends a notification email.
 *
 * Pattern: discovery via SDK search · execution via /proxy/{slug}
 */
import { WayforthClient } from 'wayforth-sdk';

const API_KEY = process.env.WAYFORTH_API_KEY ?? '';
if (!API_KEY) {
  console.error('Set WAYFORTH_API_KEY in your environment. Get a key at wayforth.io/signup');
  process.exit(1);
}

const TEXT_TO_TRANSLATE = 'Hello from Wayforth — your agent is live.';
const TARGET_LANG       = 'FR';
const NOTIFY_EMAIL      = process.env.NOTIFY_EMAIL ?? '';
const GATEWAY           = 'https://gateway.wayforth.io';

const PROXY_CATALOG_SLUGS = new Set([
  'alphavantage', 'assemblyai', 'brave_search', 'deepl', 'elevenlabs', 'firecrawl',
  'gemini', 'groq', 'jina', 'mistral', 'openweather', 'perplexity',
  'resend', 'serper', 'stability', 'tavily', 'together',
]);
const CATALOG_TO_PROXY: Record<string, string> = { brave_search: 'brave' };

async function proxyPost(slug: string, params: Record<string, unknown>) {
  const resp = await fetch(`${GATEWAY}/proxy/${slug}`, {
    method: 'POST',
    headers: { 'X-Wayforth-API-Key': API_KEY, 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  });
  if (!resp.ok) throw new Error(`${slug} proxy returned ${resp.status}: ${await resp.text()}`);
  return resp;
}

async function main() {
  const wf = new WayforthClient({ apiKey: API_KEY });

  // ── 1. DISCOVER translation service ──────────────────────────────────────
  console.log('Discovering best translation service...');
  const { results } = await wf.search('translate text to another language', { limit: 10 });
  const candidates = results.filter((r) => r.slug !== null && PROXY_CATALOG_SLUGS.has(r.slug!));
  if (!candidates.length) throw new Error('No proxy-managed translation service found.');

  // ── 2. TRANSLATE via proxy (try candidates in WRI order) ─────────────────
  let resp: Response | null = null;
  let usedSlug = '';
  for (const hit of candidates) {
    const slug = hit.slug!;
    console.log(`→ Trying ${hit.name}  WRI=${hit.wri}  slug=${slug}`);
    const r = await fetch(`${GATEWAY}/proxy/${slug}`, {
      method: 'POST',
      headers: { 'X-Wayforth-API-Key': API_KEY, 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: TEXT_TO_TRANSLATE, target_lang: TARGET_LANG }),
    });
    if (r.ok) { resp = r; usedSlug = slug; break; }
    console.log(`  ✗ ${slug} returned ${r.status} — trying next service`);
  }
  if (!resp) throw new Error('All discovered services failed. Check your API key and credits.');
  console.log(`\nTranslating via /proxy/${usedSlug}...`);
  console.log(`[wayforth] failover  : ${resp.headers.get('x-wayforth-failover')}`);
  console.log(`[wayforth] wri       : ${resp.headers.get('x-wayforth-wri')}`);
  console.log(`[wayforth] credits   : ${resp.headers.get('x-wayforth-credits-remaining')} remaining`);

  const data = await resp.json() as Record<string, string>;
  const translatedText = data['translated_text'] ?? '';
  const sourceLang     = data['detected_source_lang'] ?? '?';
  console.log(`\nOriginal  (${sourceLang}): ${TEXT_TO_TRANSLATE}`);
  console.log(`Translated (${TARGET_LANG}): ${translatedText}`);

  // ── 3. NOTIFY (optional) ─────────────────────────────────────────────────
  if (!NOTIFY_EMAIL) {
    console.log('\n[skip] Set NOTIFY_EMAIL to also send a notification via Resend.');
    return;
  }
  console.log('\nDiscovering notification service...');
  const { results: notifResults } = await wf.search('send email notification', { limit: 10 });
  const notifHit = notifResults.find((r) => r.slug !== null && PROXY_CATALOG_SLUGS.has(r.slug!));
  if (!notifHit?.slug) { console.log('[skip] No proxy-managed email service found.'); return; }

  const notifResp = await proxyPost(notifHit.slug, {
    to: NOTIFY_EMAIL, subject: `Translation complete: ${TARGET_LANG}`,
    html: `<p><strong>Original:</strong> ${TEXT_TO_TRANSLATE}</p><p><strong>Translated:</strong> ${translatedText}</p>`,
  });
  console.log('Notification sent:', await notifResp.json());
}

main().catch((e) => { console.error(e.message); process.exit(1); });
