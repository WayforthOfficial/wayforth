# @wayforth/sdk

[![npm](https://img.shields.io/npm/v/wayforth-sdk)](https://www.npmjs.com/package/wayforth-sdk)

TypeScript/JavaScript SDK for [Wayforth](https://wayforth.io) — search, execute, and pay for any of 4,974 indexed APIs.

## Install

```bash
npm install wayforth-sdk
```

## Usage

```typescript
import { WayforthClient } from "wayforth-sdk";

const client = new WayforthClient("wf_live_...");

// Search by natural language intent
const results = await client.search("translate text to Spanish");
for (const svc of results.results.slice(0, 3)) {
  console.log(`${svc.name}  WRI: ${svc.wri}  tier ${svc.coverage_tier}`);
}

// Execute a managed service directly — no external API key needed
const translation = await client.execute("deepl", { text: "Hello", target_lang: "ES" });
console.log(translation);

// Intent-based routing — Wayforth picks the best service and runs it
const result = await client.run("get current weather in London");
console.log(result);
```

## API

### `new WayforthClient(apiKey, baseUrl?)`

`baseUrl` defaults to `https://gateway.wayforth.io`.

### Methods

| Method | Description |
|--------|-------------|
| `search(query, options?)` | Search by natural language intent |
| `query(params)` | Structured WayforthQL query |
| `listServices(options?)` | List services with category/tier/limit filters |
| `getService(id)` | Get a single service by slug or ID |
| `getSimilar(serviceId, limit?)` | Services co-used alongside this one |
| `execute(slug, params?)` | Execute a managed service by slug |
| `run(query, params?)` | Intent-based routing and execution |
| `stats()` | Catalog statistics by tier and category |
| `status()` | API health check |
| `balance()` | Account credit balance |
| `getIdentity(agentId)` | Look up a registered agent identity |
| `registerIdentity(agentId, displayName?)` | Register an agent identity |

Full API reference: [gateway.wayforth.io/guide/](https://gateway.wayforth.io/guide/)

## Build from source

```bash
npm install
npm run build
```
