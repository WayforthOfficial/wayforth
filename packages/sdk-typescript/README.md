# wayforth-sdk

TypeScript/JavaScript SDK for [Wayforth](https://api-production-fd71.up.railway.app) — discover and call AI services from the catalog.

## Install

```bash
npm install wayforth-sdk
```

## Usage

```typescript
import { WayforthClient } from "wayforth-sdk";

const client = new WayforthClient();

// Check API status
console.log(await client.status());

// Search for services by natural language intent
const results = await client.search("fast cheap inference for coding");
for (const svc of results.results.slice(0, 3)) {
  console.log(`${svc.name} (tier ${svc.coverage_tier}) — ${svc.endpoint_url}`);
}

// List all translation services
const translators = await client.listServices({ category: "translation" });
console.log(`Found ${translators.length} translation services`);
```

## API

### `new WayforthClient(baseUrl?)`

Defaults to the production API. Pass a custom `baseUrl` to point at a local instance.

### `search(query, options?) → Promise<SearchResponse>`

Returns services ranked by semantic similarity. `options`:
- `category` — filter by `"inference"`, `"translation"`, or `"data"`
- `limit` — max results (default 5)
- `tier` — filter by coverage tier (0–3)

### `listServices(options?) → Promise<Service[]>`

Lists services, optionally filtered by `category` and/or `limit`.

### `status() → Promise<HealthResponse>`

Returns API health: `{ status, service, version, db_status }`.

## Build from source

```bash
npm install
npm run build
```
