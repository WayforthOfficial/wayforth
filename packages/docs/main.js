/* Wayforth Docs — main.js */

// Copy buttons
function initCopy() {
  document.querySelectorAll('.copy-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const block = btn.closest('.code-block');
      const text = block?.querySelector('code')?.textContent ?? '';
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        const ta = Object.assign(document.createElement('textarea'), {
          value: text, style: 'position:fixed;opacity:0'
        });
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
      btn.textContent = 'Copied!';
      btn.classList.add('copied');
      setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
    });
  });
}

// Mobile sidebar
function initSidebar() {
  const btn = document.getElementById('menuBtn');
  const sidebar = document.getElementById('sidebar');
  if (!btn || !sidebar) return;
  btn.addEventListener('click', e => { e.stopPropagation(); sidebar.classList.toggle('open'); });
  document.addEventListener('click', e => {
    if (sidebar.classList.contains('open') && !sidebar.contains(e.target) && e.target !== btn)
      sidebar.classList.remove('open');
  });
  sidebar.querySelectorAll('a').forEach(a => a.addEventListener('click', () => sidebar.classList.remove('open')));
}

// Search — cross-page index
const PAGES = [
  { title:'Getting Started',  url:'index.html',         desc:'Install, MCP, API key, free tier' },
  { title:'Search',           url:'search.html',         desc:'wayforth_search, WayforthQL, filters, categories, WRI' },
  { title:'Execution',        url:'execute.html',        desc:'wayforth_execute, managed services, BYOK, x402' },
  { title:'Payment Rails',    url:'payment-rails.html',  desc:'Card, USDC, x402, credits, top-up packages' },
  { title:'SDK Reference',    url:'sdk.html',            desc:'Python SDK, TypeScript SDK, code examples' },
  { title:'REST API',         url:'api.html',            desc:'Endpoints, auth, rate limits, error codes' },
];

function initSearch() {
  const input = document.getElementById('search');
  const results = document.getElementById('searchResults');
  if (!input || !results) return;

  // Build on-page section index
  const sections = [...document.querySelectorAll('section[id]')].map(s => ({
    id: s.id,
    title: s.querySelector('h2,h3')?.textContent?.trim() ?? s.id,
    text: s.textContent?.toLowerCase() ?? '',
  }));

  function render(q) {
    const ql = q.toLowerCase().trim();
    if (ql.length < 2) { results.style.display = 'none'; return; }
    const hits = [];
    sections.forEach(s => {
      if (s.title.toLowerCase().includes(ql) || s.text.includes(ql))
        hits.push(`<a class="sr-item" href="#${s.id}"><strong>${s.title}</strong><span>This page</span></a>`);
    });
    PAGES.forEach(p => {
      if (p.title.toLowerCase().includes(ql) || p.desc.toLowerCase().includes(ql))
        hits.push(`<a class="sr-item" href="${p.url}"><strong>${p.title}</strong><span>${p.desc}</span></a>`);
    });
    results.innerHTML = hits.length
      ? hits.slice(0, 7).join('')
      : '<div class="sr-empty">No results</div>';
    results.style.display = 'block';
    results.querySelectorAll('a').forEach(a => a.addEventListener('click', () => {
      results.style.display = 'none'; input.value = '';
    }));
  }

  input.addEventListener('input', () => render(input.value));
  input.addEventListener('keydown', e => { if (e.key==='Escape') { results.style.display='none'; input.value=''; input.blur(); } });
  document.addEventListener('click', e => { if (!input.parentElement.contains(e.target)) results.style.display='none'; });
}

// Scrollspy
function initScrollspy() {
  const links = [...document.querySelectorAll('.toc-link')];
  if (!links.length) return;
  const obs = new IntersectionObserver(entries => {
    entries.forEach(e => {
      if (e.isIntersecting) {
        links.forEach(l => l.classList.toggle('active', l.getAttribute('href') === '#' + e.target.id));
      }
    });
  }, { rootMargin: '-15% 0px -70% 0px' });
  links.forEach(l => {
    const id = l.getAttribute('href')?.slice(1);
    if (id) { const el = document.getElementById(id); if (el) obs.observe(el); }
  });
}

document.addEventListener('DOMContentLoaded', () => {
  initCopy();
  initSidebar();
  initSearch();
  initScrollspy();
});
