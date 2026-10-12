# Webhooks

Webhooks are included with Pro, Team, and Enterprise. Saturn notifies an organization's HTTPS endpoint after an approval transition. A successful response means the endpoint accepted the notification. It does not mean the agent resumed or that the tool ran.

## Events

| Type | When |
| --- | --- |
| `approval.granted` | The approval threshold is satisfied. A partial multi-sign vote does not emit this event. |
| `approval.rejected` | A reviewer rejects the request. |
| `approval.expired` | The pending request or the approved resubmission window has passed. `data.phase` is `pending` or `resubmit`. |

The same transition is stored once per endpoint. Delivering it again does not approve, reject, or execute the tool. An agent that does not receive webhooks resubmits by calling the same tool with the same arguments and `saturnRunId` set to the `run_id` from the error. That field is on every tool schema Saturn exposes. Omitting it starts a new request. Saturn does not read a run id or an action id from request headers.

Schema: `saturn.webhook.v1`.

```json
{
  "schema": "saturn.webhook.v1",
  "id": "whe_0123abcd",
  "type": "approval.granted",
  "created_at": "2026-10-12T00:00:00+00:00",
  "data": {
    "approval_id": "approval_0123abcd",
    "run_id": "run_0123abcd",
    "action_id": "act_0123abcd",
    "tool": "pay.send",
    "status": "approved",
    "phase": null,
    "expires_at": "2026-10-13T00:00:00+00:00",
    "resubmit_expires_at": "2026-10-13T00:00:00+00:00"
  }
}
```

The body is canonical JSON: UTF-8, sorted keys, no extra whitespace. The payload does not include tool arguments, API keys, OAuth tokens, or signing secrets.

## Signature

Each request includes:

- `Saturn-Webhook-Id`: the event id
- `Saturn-Webhook-Timestamp`: unix seconds
- `Saturn-Webhook-Signature`: `v1=` plus hex HMAC-SHA256

The signed message is the timestamp, a period, and the raw body. Reject timestamps outside a five-minute window, and ignore an event id you have already handled.

```python
import hashlib
import hmac

def verify(secret: str, timestamp: str, body: bytes, header: str) -> bool:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, f"v1={digest}")
```

The signing secret stays the same for every delivery until an administrator rotates it. `GET /org/webhooks/{id}/secret` downloads the current secret as a text file. The endpoint list does not include it.

Saturn does not follow redirects. Endpoints must be public HTTPS. Loopback, link-local, private, and cloud metadata addresses are rejected when the URL is saved and again when it is delivered.

## Expiration

Pending approval defaults to 24 hours from creation. After approval, a deferred tool call defaults to 24 hours from the approval time. Both can be set from 1 minute through 7 days on `PATCH /org/approval-window`. An approval policy can set a shorter `expires_minutes` or `resubmit_minutes`.

An expired approval cannot be approved or executed. The agent receives Saturn error `-32006` with `data.reason` `APPROVAL_EXPIRED` and `data.phase` `pending` or `resubmit`. This code is Saturn-specific. It is not standard MCP. A call waiting for approval is `-32001`, and the message tells the agent to repeat the tool with `saturnRunId`. Rejection is `-32002`. Retrying a rejected `saturnRunId` returns the same rejection; leaving the field out starts a separate request.

## Delivery

Jobs are written in the same database transaction as the approval change and sent afterward. Network errors, timeouts, HTTP 429, and HTTP 5xx are retried up to five times with exponential backoff and jitter. Other HTTP 4xx responses are not retried. `POST /org/webhooks/deliveries/{id}/retry` starts a new bounded cycle for a failed delivery.
