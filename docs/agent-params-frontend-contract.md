# Agent Parameters — Frontend Contract (Steps 4–5, Lovable)

This is the exact, self-contained contract for building the agent-params form. The
**backend is complete and live** (schema extraction, authoritative validation, run
injection). The frontend renders a form from a schema, submits values, and renders
server errors. **The server is the sole authority; the client form is advisory UX.**

> **Launch gate:** params are not user-visible until the renderer (Step 4) ships.
> There is no half-state — an agent's `params_schema` is only meaningful once a
> renderer exists to read it. Until then every agent behaves exactly as today.

---

## 1. The schema — `GET /cloud/agents/{id}` → `params_schema`

`GET /cloud/agents/{id}` returns a `params_schema` key. It is **`null`** when the
agent declares no params (render no form — the run dispatches with no body). When
present it is this object:

```jsonc
{
  "fields": [ <field>, ... ],   // declaration order
  "order":  [ "<name>", ... ],  // TOPOLOGICAL order — evaluate visibility in THIS order
  "deps":   { "<name>": ["<name they depend on>", ...] }  // dependency graph (info)
}
```

### 1.1 `<field>` object — every key

| key | type | meaning |
|---|---|---|
| `name` | string | identifier `^[a-z][a-z0-9_]*$`; the key you send in the run body |
| `type` | string | one of `string` · `number` · `integer` · `boolean` · `enum` · `date` |
| `label` | string | form label (defaults to `name`) |
| `help` | string \| null | helper text |
| `required` | boolean | unconditionally required (default `false`) |
| `default` | any \| null | prefill value; for `date` may be the literal `"today"` |
| `validation` | object | type-specific rules (below) |
| `show_if` | predicate \| null | field is visible iff the predicate is true (null = always) |
| `required_if` | predicate \| null | additional conditional requiredness (composes with `required`) |

### 1.2 `validation` by `type`

| type | keys |
|---|---|
| `string` | `minLength` (int), `maxLength` (int), `regex` (string — JS `RegExp`, anchor with `^…$`) |
| `number` | `min` (num), `max` (num) |
| `integer` | `min` (num), `max` (num) |
| `boolean` | — |
| `enum` | `options`: `[{ "value": <scalar>, "label": <string> }, …]` — render these, submit the `value` |
| `date` | `min`/`max` (`"YYYY-MM-DD"` or `"today"`), `min_field`/`max_field` (another field's `name`) |

### 1.3 Predicate format (`show_if` / `required_if`)

**Leaf:** `{ "field": "<name>", "<op>": <value> }` — exactly one operator key.

| op | true when |
|---|---|
| `equals` / `not_equals` | `value(field)` == / != the operand |
| `in` / `not_in` | `value(field)` is / isn't in the operand array |
| `not_empty` / `is_empty` | the field has / hasn't a value (operand ignored) |
| `gt` / `gte` / `lt` / `lte` | numeric/date comparison (absent value ⇒ false) |

**Combinators (nestable):** `{ "all_of": [<pred>, …] }` (AND) · `{ "any_of": [<pred>, …] }` (OR) · `{ "not": <pred> }`.

"Absent" = value is `null` or `""`.

### 1.4 Stock Quote example (what `params_schema` looks like)

```jsonc
{
  "fields": [
    { "name": "ticker", "type": "string", "label": "Ticker symbol",
      "help": "e.g. AAPL", "required": true, "default": null,
      "validation": { "regex": "^[A-Z]{1,5}$", "maxLength": 5 },
      "show_if": null, "required_if": null },

    { "name": "interval", "type": "enum", "label": "Interval", "help": null,
      "required": true, "default": "daily",
      "validation": { "options": [
        { "value": "daily", "label": "Daily" },
        { "value": "weekly", "label": "Weekly" },
        { "value": "intraday", "label": "Intraday" } ] },
      "show_if": null, "required_if": null },

    { "name": "intraday_minutes", "type": "enum", "label": "Intraday resolution",
      "help": null, "required": false, "default": null,
      "validation": { "options": [
        { "value": "1min", "label": "1 min" },
        { "value": "5min", "label": "5 min" } ] },
      "show_if": { "field": "interval", "equals": "intraday" },
      "required_if": { "field": "interval", "equals": "intraday" } },

    { "name": "use_date_range", "type": "boolean", "label": "Limit to a date range",
      "help": null, "required": false, "default": false,
      "validation": {}, "show_if": null, "required_if": null },

    { "name": "start_date", "type": "date", "label": "Start date", "help": null,
      "required": false, "default": null, "validation": { "max": "today" },
      "show_if": { "field": "use_date_range", "equals": true },
      "required_if": { "field": "use_date_range", "equals": true } },

    { "name": "end_date", "type": "date", "label": "End date", "help": null,
      "required": false, "default": null,
      "validation": { "min_field": "start_date", "max": "today" },
      "show_if": { "field": "use_date_range", "equals": true },
      "required_if": { "field": "use_date_range", "equals": true } }
  ],
  "order": ["interval", "ticker", "use_date_range", "intraday_minutes",
            "start_date", "end_date"],
  "deps": { "ticker": [], "interval": [], "use_date_range": [],
            "intraday_minutes": ["interval"],
            "start_date": ["use_date_range"],
            "end_date": ["use_date_range", "start_date"] }
}
```

---

## 2. Dispatching a run — `POST /cloud/agents/{id}/runs`

Body is a single `params` object: `{ "<name>": <value>, … }`. Send only **visible**
fields (clear hidden ones). Types to send per `type`:

| type | send |
|---|---|
| string | JSON string |
| integer | JSON number (whole) — **`5.5` is rejected, never floored** |
| number | JSON number |
| boolean | JSON `true`/`false` |
| date | JSON string `"YYYY-MM-DD"` |
| enum | the option's `value` (verbatim) |

```jsonc
POST /cloud/agents/312f7603-.../runs
{ "params": { "ticker": "AAPL", "interval": "intraday", "intraday_minutes": "5min" } }
```

Omit a field to mean "absent" (server applies its `default`, or errors if required).
`""` and `null` are also treated as absent. Agents with `params_schema === null`:
send no body (or `{}`).

**Success →** `202` with `{ run_id, status: "queued", credits_reserved, … }` (unchanged).

---

## 3. Validation errors — `422`

When server validation fails, the response is `422` with this body:

```jsonc
{ "detail": {
    "error": "invalid_params",
    "errors": [ { "field": "<name | key | null>", "code": "<code>", "message": "<human text>" }, … ]
} }
```

Render each error inline on `field` (a `name`). `field` is the offending key for
`unknown_param`, and `null` for `invalid_body`.

| `code` | meaning |
|---|---|
| `required` | a visible required field was empty |
| `type` | wrong type / uncoercible (e.g. `"5.5"` for an integer, `"yes"` for a boolean) |
| `minLength` / `maxLength` / `regex` | string rule failed |
| `min` / `max` | number or date bound failed |
| `enum` | value not one of the options |
| `min_field` / `max_field` | cross-field date ordering failed |
| `unknown_param` | a key not in the schema was sent |
| `invalid_body` | `params` wasn't an object |

The `message` is human-readable and prefixed with the field's `label`.

---

## 4. Client evaluator — advisory only, mirror §"§2 semantics"

**The server re-validates everything on dispatch and is the only authority.** A
hand-crafted request bypassing the client cannot get an invalid value into a run. The
client form exists for UX (show/hide, inline errors, disabling submit) — **never
assume client validation is sufficient; always handle the `422`.**

Mirror this evaluation (re-run on every field change — schemas are small):

1. **Iterate fields in `order`** (topological). Maintain a `values` map.
2. **Visibility:** `visible = show_if == null || evalPredicate(show_if, values)`.
   - **Hidden ⇒ clear** the field's value (remove from `values`), don't render it,
     don't validate it, and **don't apply its default**. (Hidden fields must not be
     submitted; the server drops them anyway.)
3. **Default:** if visible and value is absent (`null`/`""`), prefill `default`
   (for a `date` default of `"today"`, use the current date).
4. **Required:** `effectiveRequired = visible && (required || (required_if != null && evalPredicate(required_if, values)))`.
   Flag empty + effectiveRequired.
5. **Validate** visible, present values against `validation` (mirror the rules);
   for `min_field`/`max_field`, compare to that field's current value (skip if it's
   absent/hidden). `"today"` resolves to the current date.

`evalPredicate` implements the §1.3 operators; absent (`null`/`""`) value ⇒ `gt/lt`
comparisons are false, `not_empty` false, `is_empty` true.

> The `deps` map and topological `order` are provided so you don't have to derive
> them — evaluate strictly in `order` and each field's predicates reference only
> already-evaluated fields. The backend already rejected any dependency cycle at
> upload, so `order` is always a valid total order.
