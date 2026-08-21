# Fixtures — real test-mode payloads only

**Do not hand-write fixtures in this directory.** Invented payloads get the field shapes
wrong, and `(method, source, step, reason)` is the classifier key (C2) — a fixture with a
plausible-looking but fictional `error_step` produces a classifier that passes its tests and
fails on contact with the real API.

## Two different artefacts, both needed

C1 consumes a **webhook envelope**; C2 consumes a **payment entity**. They are not the same
shape, and the piece C1 dedupes on does not exist in the entity at all.

| Artefact | Directory | Contains | How to get it |
|---|---|---|---|
| Payment entity | `payments/` | `error_code`, `error_description`, `error_source`, `error_step`, `error_reason` | `scripts/capture_fixtures.py` — API fetch, needs only the key pair |
| Webhook envelope | `webhooks/` | `x-razorpay-event-id` header, `event`, `payload.payment.entity`, signature header | Manual capture — requires a reachable endpoint |

**`x-razorpay-event-id` is a header, not a body field.** It is the dedup key for C1, so the
envelope must be captured with its headers intact or the fixture cannot exercise dedup.

## Generating failures in test mode

Razorpay test mode provides deterministic failure instruments:

| Rail | Instrument | Use for |
|---|---|---|
| UPI | VPA `failure@razorpay` | UPI failure steps — `payment_debit_response`, `payment_authentication` |
| Card | `4000 0000 0000 0002` | Card decline. Any future expiry, any CVV |

1. Dashboard → **switch to Test Mode** (the toggle, top of the sidebar)
2. Settings → API Keys → generate a test key pair. The secret is shown **once**
3. Create a Payment Link for a small amount, open it, and pay with a failure instrument above
4. Repeat two or three times, varying rail and instrument — the goal is *distinct*
   `(source, step, reason)` combinations, not volume

Then:

```powershell
$env:RAZORPAY_KEY_ID     = "rzp_test_..."
$env:RAZORPAY_KEY_SECRET = "..."
python scripts/capture_fixtures.py
```

The script prints a `source / step / reason` column per captured payment. **Check those are
populated before committing.** An empty `step` column means the fixture is not useful for C2.

## Capturing the webhook envelope

The entity fetch above does not produce an envelope. For that:

1. Dashboard → Settings → Webhooks → add a URL, subscribe to `payment.failed`
2. Point it at a request-capture endpoint you control. A throwaway inspector service works,
   but it is a third party — **use test mode only, and send nothing from live mode there**
3. Trigger a failure as above
4. Save the full request — **headers included** — to `webhooks/payment_failed_<n>.json`

Store headers and body separately so the header case is preserved:

```json
{
  "headers": {"x-razorpay-event-id": "...", "x-razorpay-signature": "..."},
  "body": { }
}
```

## Hygiene

- Test mode only. `capture_fixtures.py` refuses any key not prefixed `rzp_test_`
- Never commit the key pair. `.env` is gitignored; keys belong in the environment
- The capture script replaces `email` / `contact` / `customer_id` values with placeholders of
  the same type, preserving shape. `--raw` disables this — do not use it for committed fixtures
- The signature header is derived from your **webhook secret**. It cannot be re-verified by
  anyone else, so signature-verification tests must use a locally-generated secret, not this one
