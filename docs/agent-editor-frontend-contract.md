# Agent Editor — Frontend Contract (for Lovable, Steps 6–7)

Exact, self-contained contract for the code editor + version UI. Build against these
shapes, not an approximation. Companion to `agent-params-frontend-contract.md` (the run
form); this doc covers **editing, deploying, versioning, the requirements picker, and the
template picker**.

All paths are under the API base. Auth is the same dual mode the rest of `/cloud/*` uses:
Supabase **session** (dashboard) or **API key** (SDK) — send whichever the dashboard
already sends; no change here.

---

## 0. What's testable NOW vs after the flip (read this first)

Versioned **writes** (deploy/rollback) are behind a server flag,
`AGENT_VERSIONED_DISPATCH_ENABLED` (default **OFF**, flips in Step 4b once the build
mirror is up). When OFF, those endpoints return **`409 versioning_disabled`**.

| Endpoint | Flag-gated? | Testable now? |
|---|---|---|
| `GET /cloud/agents/{id}/versions` | no | **yes** (returns the backfilled v1 today) |
| `GET /cloud/deps/allowlist` *(to add — §2)* | no | yes, once implemented |
| `GET /templates`, `GET /templates/{id}` | no | **yes** (already live) |
| `POST /cloud/agents/{id}/deploy` | **yes** | after the flip (409 until then) |
| `POST /cloud/agents/{id}/rollback` | **yes** | after the flip (409 until then) |

Build the read-only views (history, template picker, requirements picker) against live
data now; build deploy/rollback against this spec and they light up at the flip. Handle
`409 versioning_disabled` gracefully (e.g. a "deploys not yet enabled" state).

---

## 1. Endpoints

### 1a. `POST /cloud/agents/{id}/deploy` — multi-file deploy
Creates a new immutable version, validates it, builds its image, and (on success)
activates it. The current code stays live until the new version is fully built — a failed
deploy never takes the agent down.

**Request body**
```json
{
  "files": { "agent.py": "PARAMS = [...]\n...", "helper.py": "def util(): ..." },
  "requirements": "httpx==0.28.1\nbeautifulsoup4==4.15.0"
}
```
- `files` — map of **relative path → file content**. Must include the **entrypoint**:
  `agent.py` (python) or `agent.ts` (node). PARAMS is extracted from the entrypoint only;
  other files are helper modules. Total size ≤ **524288 bytes** (512 KB).
- `requirements` — a `requirements.txt` body: one **`name==version`** per line, pinned and
  from the allowlist (§2). Empty string `""` = no dependencies.

**Response `200`**
```json
{
  "id": "<agent_id>",
  "version_no": 3,
  "version_id": "<uuid>",
  "image_ref": "<snapshot-id|null>",
  "activated": true,
  "status": "active"
}
```
- `activated` — `true` if this version became the active one. `false` + `status:"superseded"`
  can happen if a newer version was deployed concurrently (forward-only activation; the
  later submission wins — rare, but handle it: re-fetch `/versions`).

**Errors** — see §4 for the full table. Validation failures are `422`; build failures `502`.

---

### 1b. `POST /cloud/agents/{id}/rollback` — revert to a prior version
Instant + safe: the target's image is already built, so this is a pure pointer move (no
rebuild). Roll **forward or backward** to any prior version that was actually live.

**Request body** — one of:
```json
{ "version_id": "<uuid>" }      // or
{ "version_no": 2 }
```

**Response `200`**
```json
{ "id": "<agent_id>", "rolled_back_to": 2, "version_id": "<uuid>" }
```

**Errors**
| Status | `error` | When |
|---|---|---|
| `409` | `versioning_disabled` | flag off |
| `409` | `agent_running` | a run is in progress |
| `404` | `version_not_found` | no such version for this agent |
| `422` | `rollback_rejected` | target is `failed`/`building` (never was live) — `message` says which |

---

### 1c. `GET /cloud/agents/{id}/versions` — history (for the version UI)
**Response `200`**
```json
{
  "id": "<agent_id>",
  "active_version_id": "<uuid|null>",
  "versions": [
    { "id": "<uuid>", "version_no": 3, "status": "active",     "image_ref": "<snapshot|null>", "created_at": "2026-06-27T18:00:00+00:00" },
    { "id": "<uuid>", "version_no": 2, "status": "superseded", "image_ref": "<snapshot|null>", "created_at": "2026-06-27T17:30:00+00:00" },
    { "id": "<uuid>", "version_no": 1, "status": "active",     "image_ref": null,              "created_at": "2026-06-20T10:00:00+00:00" }
  ]
}
```
- Ordered **newest first**. File bodies are excluded (use a future per-version GET if the UI
  needs to diff — not in this contract).
- **Active flag**: a version is the active one iff `version.id === active_version_id`.

---

## 2. The requirements picker — the allowlist source

**Answer to "is there a GET, or is it static?":** the allowlist is currently a **static
server-side lockfile** (`services/agent_deps_lock.json`, shape `{name: {version: [hashes]}}`)
with **no public endpoint** yet. The frontend cannot and should not read that file or bundle
a copy (it drifts + leaks hashes). So this contract specifies a small read-only endpoint to
add:

### `GET /cloud/deps/allowlist` — **NOT YET IMPLEMENTED (trivial to add)**
**Response `200`**
```json
{
  "packages": {
    "httpx": ["0.28.1"],
    "beautifulsoup4": ["4.15.0"],
    "anyio": ["4.14.1"]
  },
  "max_direct_deps": 20
}
```
- `packages` — **name → sorted list of installable versions** (derived from the lockfile;
  **hashes omitted** — the server attaches them at build time).
- `max_direct_deps` — the cap the editor should enforce client-side (server enforces too).

**Picker behavior:** user picks a package + a version from `packages`; the editor emits a
`name==version` line into `requirements`. Anything not in this map will be rejected at
deploy with `not_allowed` (§4) — so gate the "add" button on this list.

> **Action needed (one-liner, your call):** implement `GET /cloud/deps/allowlist` returning
> the lockfile minus hashes. Until then, the picker has no data source. Flagging it here so
> it's not discovered mid-build.

---

## 3. The version object (history UI)

| field | type | values / notes |
|---|---|---|
| `id` | string (uuid) | the version's id (use for rollback) |
| `version_no` | int | monotonic per agent, 1-based |
| `status` | string | `building` \| `active` \| `superseded` \| `failed` |
| `image_ref` | string \| null | built image; `null` = backfilled v1 (runs legacy `.code` path) |
| `created_at` | string | ISO-8601 |
| `active` | derived | `id === active_version_id` (from the `/versions` envelope) |

- **Status → UI**: `active` = green/current; `superseded` = a prior good version (rollback
  target); `failed` = a deploy that didn't build (show the error, **not** a rollback target);
  `building` = in progress.
- **Optional**: versions also carry a server-side `dep_flagged` boolean (set when a baked
  package is later revoked — §5). It is **not** in the `/versions` response today; if you want
  a "uses a revoked package — rebuild" badge, ask and we'll add it to the projection.

---

## 4. Error shapes (render inline in the editor)

Every editor write error is JSON `{ "error": "<code>", "message": "<human>", "errors": [...] }`.
The `errors` array (when present) is per-field, for inline rendering.

### Deploy validation (`422`)
| `error` | `errors[]`? | Render |
|---|---|---|
| `redeploy_files` | no | top-level: `message` (e.g. "missing entrypoint 'agent.py'") |
| `redeploy_params` | no | on the **entrypoint editor**: `message` (e.g. "PARAMS must be a pure literal (no function calls, names, f-strings, or comprehensions)") |
| `redeploy_requirements` | **yes** | per requirement line — see codes below |
| `no_files` / `code_too_large` | no | top-level banner (`code_too_large` includes `max_bytes`) |

### Build failure (`502`)
`{ "error": "redeploy_build", "message": "<install/build error, truncated>", "errors": [] }`
→ banner; the prior version is still serving (reassure the user their agent is fine).

### `redeploy_requirements` → `errors[]` items: `{ "field": "<pkg or line>", "code": "...", "message": "..." }`
| `code` | meaning (map `field` → the offending requirement row) |
|---|---|
| `not_allowed` | package isn't in the allowlist |
| `version_not_allowed` | package ok, that version isn't (message lists allowed versions) |
| `unpinned` | not `name==version` |
| `unsupported` | malformed line |
| `duplicate` | same package twice |
| `too_many` | more than `max_direct_deps` (`field` is null) |
| `revoked` | package was allowlisted but later revoked (§5) |
| `base_dep_conflict` | collides with a baked base dependency |

---

## 5. Package revocation (informational)

A previously-allowlisted package can be **revoked** (supply-chain backstop). Effects the
editor should know:
- New deploys using a revoked pin fail with `redeploy_requirements` → `code: "revoked"`.
- After the allowlist GET (§2) is live, revoked packages/versions simply won't appear in it.
- Existing versions that baked a now-revoked package are server-flagged (`dep_flagged`) — see
  the optional badge note in §3. Revocation itself is an **admin** action
  (`POST /admin/packages/revoke`), not part of the user-facing editor.

---

## 6. Template picker — reuse the existing registry (already live)

No new endpoints. The "new agent from template" flow uses:

### `GET /templates` → list (no code)
```json
{ "templates": [ { "id": "research-agent", "name": "Research Agent", "description": "…", "runtimes": ["python3.12","node20"], "services": [...], "credits": ... } ], "total": 7 }
```

### `GET /templates/{id}` → full detail (incl. code per runtime + readme)
404 → `{ "error": "template_not_found", "template_id": "...", "available": ["..."] }`.

### `POST /templates/{id}/deploy` → create a hosted agent pre-loaded with the template
(Existing one-call deploy; the editor's "start from template" can either call this, or load
the template's code into the editor and let the user `POST /deploy`.)

Treat the **live `GET /templates` response as the source of truth** for the picker fields.

---

## 7. Conventions recap
- **Entrypoint**: `agent.py` (python) / `agent.ts` (node) — required key in `files`; PARAMS
  extracted from it; the run form (params contract) is driven by its declared `PARAMS`.
- **Editing is safe**: a save that fails validation or build never changes what's running.
- **Rollback is instant**: prior images are retained; reverting is a pointer move.
- **Flag-gated writes**: handle `409 versioning_disabled` until Step 4b flips the flag.
