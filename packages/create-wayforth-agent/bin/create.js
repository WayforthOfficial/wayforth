#!/usr/bin/env node
'use strict';

const fs   = require('fs');
const path = require('path');
const rl   = require('readline');

const VERSION   = '0.9.0';
const TEMPLATES = path.join(__dirname, '..', 'templates');

const ARCHETYPES = {
  'search-summarize': 'Discover a search API at runtime, run it, print top results',
  'translate-notify': 'Discover a translation API, translate text, optionally notify',
  'fetch-process':    'Discover a data API, fetch structured data, transform it',
};

const NEXT_STEPS = {
  python: (name) => [
    `  cd ${name}`,
    `  cp .env.example .env`,
    `  # Add your key: WAYFORTH_API_KEY=wf_live_...`,
    `  pip install -r requirements.txt`,
    `  python agent.py`,
  ],
  typescript: (name) => [
    `  cd ${name}`,
    `  cp .env.example .env`,
    `  # Add your key: WAYFORTH_API_KEY=wf_live_...`,
    `  npm install`,
    `  npx tsx src/agent.ts`,
  ],
};

function ask(iface, question) {
  return new Promise((resolve) => iface.question(question, (ans) => resolve(ans.trim())));
}

async function pickOne(iface, prompt, choices) {
  const keys = Object.keys(choices);
  console.log(`\n${prompt}`);
  keys.forEach((k, i) => console.log(`  ${i + 1}) ${k}  — ${choices[k]}`));
  while (true) {
    const ans = await ask(iface, `  Choice [1-${keys.length}]: `);
    const idx = parseInt(ans, 10) - 1;
    if (idx >= 0 && idx < keys.length) return keys[idx];
    const lower = ans.toLowerCase();
    if (keys.includes(lower)) return lower;
    console.log(`  Please enter a number between 1 and ${keys.length}.`);
  }
}

function copyTemplate(src, dest, vars) {
  const entries = fs.readdirSync(src, { withFileTypes: true });
  for (const entry of entries) {
    const srcPath  = path.join(src, entry.name);
    const destName = entry.name.startsWith('_dot_') ? `.${entry.name.slice(5)}` : entry.name;
    const destPath = path.join(dest, destName);
    if (entry.isDirectory()) {
      fs.mkdirSync(destPath, { recursive: true });
      copyTemplate(srcPath, destPath, vars);
    } else {
      let content = fs.readFileSync(srcPath, 'utf8');
      for (const [k, v] of Object.entries(vars)) {
        content = content.replaceAll(`{{${k}}}`, v);
      }
      fs.writeFileSync(destPath, content);
      console.log(`  ✓  ${path.relative(dest, destPath)}`);
    }
  }
}

async function main() {
  console.log(`\n  create-wayforth-agent v${VERSION}\n`);
  console.log('  Discovery via SDK · Execution via /proxy/{slug}');
  console.log('  Get a key at wayforth.io/signup\n');

  // --- parse CLI flags ---
  const argv     = process.argv.slice(2);
  let nameArg    = argv.find((a) => !a.startsWith('--'));
  let langArg    = (argv.find((a) => a.startsWith('--lang=')) || '').split('=')[1];
  let archetypeArg = (argv.find((a) => a.startsWith('--archetype=')) || '').split('=')[1];

  const iface = rl.createInterface({ input: process.stdin, output: process.stdout });

  try {
    // --- name ---
    const name = nameArg || await ask(iface, '  Agent name?  › ');
    if (!name) { console.error('Name is required.'); process.exit(1); }
    if (fs.existsSync(name)) { console.error(`  ✗  Directory '${name}' already exists.`); process.exit(1); }

    // --- language ---
    const LANGS = { python: 'Python', typescript: 'TypeScript' };
    const lang = langArg && LANGS[langArg]
      ? langArg
      : await pickOne(iface, 'Language?', LANGS);

    // --- archetype ---
    const archetype = archetypeArg && ARCHETYPES[archetypeArg]
      ? archetypeArg
      : await pickOne(iface, 'Archetype?', ARCHETYPES);

    iface.close();

    // --- scaffold ---
    const templateDir = path.join(TEMPLATES, lang, archetype);
    if (!fs.existsSync(templateDir)) {
      console.error(`  ✗  Template not found: ${lang}/${archetype}`);
      process.exit(1);
    }

    console.log(`\n  Creating ${name}/...`);
    fs.mkdirSync(name, { recursive: true });
    copyTemplate(templateDir, name, { AGENT_NAME: name, ARCHETYPE: archetype, VERSION });

    console.log(`\n  Done! Next steps:\n`);
    NEXT_STEPS[lang](name).forEach((l) => console.log(l));
    console.log('');

  } catch (e) {
    iface.close();
    throw e;
  }
}

main().catch((e) => { console.error(e.message); process.exit(1); });
