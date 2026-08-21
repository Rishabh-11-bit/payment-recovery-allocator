"""Capture real test-mode failure payloads from the Razorpay Payments API.

Fixtures must come from the live test-mode API, not from hand-written JSON:
invented payloads get the field shapes wrong (`error_source`, `error_step` and
`error_reason` in particular), and that error surfaces at integration rather
than at authoring time.

Stdlib only, deliberately -- this runs before any dependency is pinned.

Usage (PowerShell):

    $env:RAZORPAY_KEY_ID     = "rzp_test_..."
    $env:RAZORPAY_KEY_SECRET = "..."
    python scripts/capture_fixtures.py

Reads credentials from the environment only. Never pass them as arguments --
they land in shell history. See tests/fixtures/README.md for how to generate
the failures this script then captures.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

API = "https://api.razorpay.com/v1/payments"
OUT = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "payments"

# Field shape is what we are preserving; the value is not. Test-mode payloads
# are synthetic, but a real address still reaches these fields if you type one
# into the checkout form.
REDACT = ("email", "contact", "customer_id")
PLACEHOLDER = {"email": "void@example.com", "contact": "+919999999999"}


def fetch(key_id: str, key_secret: str, count: int) -> list[dict]:
    token = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
    req = urllib.request.Request(
        f"{API}?count={count}",
        headers={"Authorization": f"Basic {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()).get("items", [])
    except urllib.error.HTTPError as exc:
        # Never echo the body blindly -- it can restate the key id.
        sys.exit(f"API returned {exc.code} {exc.reason}. Check the test-mode key pair.")
    except urllib.error.URLError as exc:
        sys.exit(f"Could not reach the Razorpay API: {exc.reason}")


def redact(payment: dict) -> dict:
    out = dict(payment)
    for field in REDACT:
        if out.get(field):
            out[field] = PLACEHOLDER.get(field, f"redacted_{field}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=50, help="payments to scan (default 50)")
    ap.add_argument("--raw", action="store_true", help="skip contact-field redaction")
    args = ap.parse_args()

    key_id = os.environ.get("RAZORPAY_KEY_ID", "")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not key_id or not key_secret:
        sys.exit("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in the environment first.")
    if not key_id.startswith("rzp_test_"):
        sys.exit(f"Refusing to run: {key_id[:8]}... is not a test-mode key.")

    failed = [p for p in fetch(key_id, key_secret, args.count) if p.get("status") == "failed"]
    if not failed:
        print("No failed payments found. Generate some first -- see tests/fixtures/README.md.")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    print(f"{'payment_id':<22} {'method':<12} {'source':<14} {'step':<26} reason")
    print("-" * 92)
    for payment in failed:
        record = payment if args.raw else redact(payment)
        (OUT / f"{payment['id']}.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(
            f"{payment['id']:<22} {payment.get('method', '-'):<12} "
            f"{payment.get('error_source') or '-':<14} "
            f"{payment.get('error_step') or '-':<26} "
            f"{payment.get('error_reason') or '-'}"
        )

    print(f"\nWrote {len(failed)} fixture(s) to {OUT}")
    print("Check the source/step/reason columns above are populated before committing --")
    print("an empty step column means the fixture cannot exercise C2's classifier key.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
