# Wayforth Webhooks

Wayforth fires signed HTTP POST requests to your endpoint when key events occur in your account. Use webhooks to react to credit changes, execution completions, and payment confirmations in real time.

---

## Registration

```http
POST /webhooks/register
X-Wayforth-API-Key: wf_live_...
Content-Type: application/json

{
  "url": "https://yourapp.com/hooks/wayforth",
  "events": ["execution.completed", "credits.low", "payment.confirmed"],
  "secret": "your_signing_secret"
}
```

**Response**

```json
{
  "webhook_id": "wh_01J...",
  "url": "https://yourapp.com/hooks/wayforth",
  "events": ["execution.completed", "credits.low", "payment.confirmed"],
  "active": true,
  "created_at": "2026-05-06T10:00:00Z"
}
```

---

## Events

| Event | Fires when |
|-------|-----------|
| `execution.completed` | A `/execute` call succeeds |
| `credits.low` | Credit balance drops below 20 |
| `payment.confirmed` | A credit purchase settles |

### `execution.completed` payload

```json
{
  "event": "execution.completed",
  "webhook_id": "wh_01J...",
  "timestamp": "2026-05-06T10:01:23Z",
  "data": {
    "service_slug": "groq",
    "credits_used": 3,
    "status": "ok"
  }
}
```

### `credits.low` payload

```json
{
  "event": "credits.low",
  "webhook_id": "wh_01J...",
  "timestamp": "2026-05-06T10:01:23Z",
  "data": {
    "credits_remaining": 15,
    "threshold": 20
  }
}
```

### `payment.confirmed` payload

```json
{
  "event": "payment.confirmed",
  "webhook_id": "wh_01J...",
  "timestamp": "2026-05-06T10:01:23Z",
  "data": {
    "credits_added": 50000,
    "amount_usd": 19.00,
    "plan": "starter"
  }
}
```

---

## Signature Verification

Every request includes an `X-Wayforth-Signature` header — an HMAC-SHA256 hex digest of the raw request body, keyed with the `secret` you provided at registration.

**Always verify the signature before processing the event.**

### Node.js

```js
const crypto = require("crypto");

function verifyWebhook(rawBody, signature, secret) {
  const expected = crypto
    .createHmac("sha256", secret)
    .update(rawBody)
    .digest("hex");
  return crypto.timingSafeEqual(
    Buffer.from(signature),
    Buffer.from(expected)
  );
}

// Express example
app.post("/hooks/wayforth", express.raw({ type: "*/*" }), (req, res) => {
  const sig = req.headers["x-wayforth-signature"];
  if (!verifyWebhook(req.body, sig, process.env.WEBHOOK_SECRET)) {
    return res.status(401).send("Invalid signature");
  }
  const event = JSON.parse(req.body);
  // handle event...
  res.sendStatus(200);
});
```

### Python

```python
import hashlib, hmac
from fastapi import Request, HTTPException

async def verify_webhook(request: Request, secret: str):
    body = await request.body()
    sig = request.headers.get("x-wayforth-signature", "")
    expected = hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=401, detail="Invalid signature")
    return body
```

---

## Management

### List webhooks

```http
GET /webhooks
X-Wayforth-API-Key: wf_live_...
```

### Delete a webhook

```http
DELETE /webhooks/{webhook_id}
X-Wayforth-API-Key: wf_live_...
```

---

## Retry Policy

Wayforth retries failed deliveries (non-2xx or timeout) up to **3 times** with exponential backoff: 10 s, 60 s, 300 s. After three failures the webhook is automatically deactivated.

**Respond with HTTP 2xx within 5 seconds.** For slow processing, acknowledge immediately and handle asynchronously.

---

## Testing

Use the Wayforth dashboard to send a test event to any registered webhook, or trigger one manually:

```bash
curl -X POST https://gateway.wayforth.io/execute \
  -H "X-Wayforth-API-Key: wf_live_..." \
  -H "Content-Type: application/json" \
  -d '{"service_slug": "deepl", "params": {"text": "hello", "target_lang": "ES"}}'
```

A successful call fires `execution.completed` to all registered endpoints.
