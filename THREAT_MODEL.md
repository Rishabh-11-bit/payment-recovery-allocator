# THREAT_MODEL

What breaks in production that does not break here.

**Naming these precisely is the point.** Every item below is a way the system could
be wrong that the current evidence cannot detect — either because the simulator does
not model it, or because the property tests assert something narrower than the claim
they appear to support. None of them has a proposed fix here; a mitigation written
next to a threat tends to get read as the threat being handled.

> **DRAFT — inference markers.** Items marked **[INFERRED]** are my reconstruction of
> the failure mode rather than something stated in the repo. The mechanism may be
> wrong even where the concern is real.

---

## 1. The late-authorisation guard is tested against the simulator, not against Razorpay

**The most important item here, and the one the whole safety argument rests on.**

The invariant is *never create a payment obligation outside the original order's
attempt chain while that chain is within its late-authorisation window.* The guard
that enforces it refuses to decide from a webhook payload and re-fetches authoritative
state first.

Every test of that guard uses `SimulatedGateway`. A test sets the payload to `failed`,
sets the authoritative state to `authorized`, and asserts no decision is recorded.
**That proves the wiring is correct. It does not prove Razorpay behaves as documented.**

Specifically unverified:

- that a `failed` payment can in fact become `authorized` up to ~3 days later
- that the Orders API clubs attempts against one order as described
- that a late-authorised duplicate is refunded automatically rather than captured
- that `reference_id` on a recovery Payment Link reconciles as expected

All four are read from documentation. If any is wrong, the 5,000-ordering clean search
is a clean search of the wrong model.

## 2. Clock skew across workers

Every timestamp in the system is `datetime.now(timezone.utc)` taken locally. The
simulator runs one process, so its clock is trivially consistent.

With more than one worker:

- **Guard decisions become clock-dependent.** The PDN lead-time check compares
  `decided_at` against a deadline. A worker running two minutes fast admits an
  execution a slow worker would refuse — and the 23:50 PDN cutoff makes this a cliff,
  not a gradient.
- **Audit sequence stops matching causal order.** `audit_events.seq` is a SQLite
  autoincrement so it is globally ordered, but the `at` timestamps written alongside
  it can go backwards relative to it. A trace that reads correctly by `seq` may read
  as time-travelling by clock.
- **Job reclaim can fire early.** `claim_jobs` reclaims work whose `claimed_at` is
  older than the timeout. A fast worker's clock makes another worker's live job look
  abandoned, and the same event gets processed twice — which the idempotency key
  survives, but the wasted work and the doubled gateway fetches are real.

**[INFERRED]** — the specific reclaim interaction is my reading of the code, not a
failure anyone has observed.

## 3. Webhook replay after a long outage

Delivery is at-least-once and Razorpay retries with exponential backoff for 24h before
disabling a webhook. Dedup is on `x-razorpay-event-id` and is permanent, because
`raw_events` is append-only and never pruned.

The untested shape is the *long* one:

- A burst of a day's held events arriving at once, all with `created_at` timestamps
  now well in the past. The staleness guard compares against the case's
  `last_event_created_at`, so a genuine older event and a delayed newer one are
  distinguished by Razorpay's timestamp — but nothing verifies that timestamp is
  assigned at generation rather than at delivery.
- Events arriving *after* the order has expired or the payment has settled. The guard
  blocks the resulting obligation, but the case may already have been finalised, and
  a re-opened case is not a state the model has.
- **The `raw_events` table growing without bound.** Nothing prunes it. At real volume
  the dedup lookup is still an indexed primary key, but the storage is not modelled at
  all.

**[INFERRED]** — the "timestamp assigned at generation vs delivery" concern is mine;
the docs consulted do not say either way.

## 4. Partial refund mid-recovery

The system models a payment as failed or settled. It does not model a *partially*
refunded one.

`PaymentStatus.REFUNDED` is treated as resolved, so the guard blocks further
obligations. But a partial refund against a captured payment leaves an amount
outstanding that is neither zero nor the original — and the allocator reasons about
`amount_paise` as a fixed quantity for the life of the case.

What is unclear, and unmodelled: whether a partially refunded subscription payment
should be recovered for the balance, and whether doing so is a new obligation on the
same chain or a different one entirely.

**[INFERRED]** — this whole item is reconstructed from the absence of handling rather
than from a documented behaviour. It may not even be a real scenario for mandate
debits; worth checking before it goes in a threat model as fact.

## 5. A merchant disables a rail under a scheduled attempt

Execution is decided ≥24h in advance. In that window the merchant can change their
configuration — turn off UPI, drop a card network, change their Payment Link defaults.

- A scheduled mandate execution against a rail the merchant has since disabled will
  fail, and it will fail *having consumed one of four capped executions*.
- Worse for C10: an `EXCLUDE_INSTRUMENT` shaping built at decision time may, by
  execution time, have excluded the only method the merchant still accepts —
  producing a recovery link with nothing on it.

Nothing re-validates the shaping against merchant configuration between decision and
execution, and the guard has no input for merchant config at all.

## 6. The ledger and the gateway disagreeing

The ledger is append-only and authoritative *about our decisions*. The gateway is
authoritative *about payments*. Nothing reconciles them.

Divergence modes:

- A decision recorded, the execution submitted, and the gateway call failing after the
  obligation was written. The ledger says we acted; the payment does not exist.
- The reverse: an execution that reached Razorpay while our write failed. The mandate
  budget is spent and our count does not know it — and this is the direction that
  breaches the NPCI cap, because the next decision believes it has an execution left.
- The two-counter question (`CHALLENGES.md` 008) makes this sharper. If the counter is
  fed from `order.attempts` it includes customer link taps, so ledger and gateway are
  counting different populations and will disagree *by design* rather than by fault.

There is no reconciliation loop, no periodic comparison, and no alarm on divergence.

## 7. Running alongside Optimizer, with both acting

C10 is scoped to the out-of-session case precisely to avoid overlapping with
Optimizer's in-session routing. That boundary is clean in the architecture and is not
enforced anywhere at runtime.

If a merchant runs both:

- Optimizer may retry in-session against a rail this layer has just excluded on a
  recovery link, or route to one this layer diagnosed as degraded.
- Both may act on the same `payment.failed`. Optimizer's in-session retry is a new
  payment against the same order — which the system will observe as another attempt on
  the chain, without knowing it did not initiate it.
- **That is exactly the counter conflation in `CHALLENGES.md` 008**, arriving from a
  second direction: a payment on the chain that our system did not cause and cannot
  distinguish from one that it did.

No coordination protocol exists. The one signal that would resolve it — an initiator
tag on each attempt — is the same thing 008 says is needed and is not yet recorded.

## 8. The simulator's arms are not the production system

Arms A and B are reimplementations. Arm C shares its decision table with the real
allocator, but the environment it runs in is not the guard-plus-worker path that would
run in production — the simulator calls `Guard` directly, while production goes through
ingest, a worker, and a decider seam.

Any claim of the form "Arm C recovers X" is a claim about the simulator's Arm C. The
holdout harness (C12) exists because that gap cannot be closed by more simulation.

## 9. Failure modes with no observability

Listed because absence of an alarm is itself a threat:

- **A classifier row that silently becomes wrong.** If Razorpay changes an
  `error_reason` string, the affected key stops matching and falls to the LOW row. That
  is safe and completely silent — recovery quality degrades with no error anywhere.
  `failure.unmapped` is audited, but nothing watches the *rate*.
- **A guard check that stops firing.** Every guard test asserts a block happens under a
  condition. None asserts a block *rate* stays plausible in production.
- **The storm governor with too little traffic.** Below `min_observations` an issuer
  reports no measurement, and `state_of` returns HEALTHY. A low-volume merchant may
  never accumulate enough observations for the governor to act at all.

**[INFERRED]** — all three are my reading of what the code does not do, rather than
observed failures.

---

## What would change the shape of this document

Two things, either of which would move several items from "unverified" to "verified or
refuted":

1. **A real UPI Autopay failure capture.** Items 1 and 9 both rest on documented
   behaviour with no captured evidence behind them on the project's primary rail.
2. **Any production traffic at all, through C12.** Items 2, 3, 6 and 7 are all
   multi-worker or multi-actor failure modes that a single-process simulator is
   structurally incapable of producing.
