\# Prior Art



What Razorpay already ships in the payment-recovery space, and where this project sits.



Written before any code. The single most likely way to fail this track is to build

something Razorpay already has and pitch it back to them.



\---



\## 1. Optimizer / Smart Router — in-session gateway routing



Optimizer routes transactions across multiple payment gateways and aggregators through one

integration. Smart Router automatically sends payments to providers with a higher probability

of success, and priority-based routing creates temporary 20-minute downtimes when a gateway's

success rate drops below threshold before falling through to the next priority.



Routing parameters: channel, payment method, BIN, card type, card brand, card issuer, bank,

and amount.



\*\*What it does not do:\*\* failure reason is not among the routing parameters. Optimizer decides

which gateway carries a transaction. It does not decide whether a failed payment is worth

attempting again.



It is also an on-demand feature requiring a support request, and dashboard routing is

Live-mode only.



\## 2. Subscriptions retry — the documented baseline



Razorpay documents an automatic retry schedule for failed subscription auto-charges:



| Rail | Behaviour |

|---|---|

| Card | T+1, T+2, T+3 daily, then `halted` |

| UPI | T+1, T+2, T+3 daily, then `halted` |

| Emandate | Async — retry only on confirmation or rejection of the prior attempt, which can exceed 24h. Charge day shifts for bank holidays (T→T−1, or T→T−3 if both are holidays) |



The documented failure causes are expired card, bank-blocked card, insufficient balance, and

cancelled mandate.



\*\*The retry model does not reference the failure reason.\*\* All four causes receive identical

treatment. This is not a criticism of Razorpay's engineering — it is the observation this

project is built on, and it is the baseline arm in the evaluation, reimplemented from their

documentation rather than invented as a strawman.



\## 3. Intelligent Retry Engine — the closest existing product



Introduced in beta at FTX 2026 as part of the Intelligent Revenue-Protect stack for UPI

Autopay. Merchants configure their own retry strategies, defining retry cadence and choosing

predefined or custom templates. It is paired with WhatsApp-led retention: branded recovery

links for registration drop-off, mandate cancellation win-back, and failed-debit recovery.



Scope is recurring only. Every component is mandate-bound — registration, debit, cancellation.

One-time checkout failures are not covered by this stack.



\*\*What it is:\*\* a configuration surface. Razorpay moved the decision to the merchant, which is

a defensible product choice — merchants know their own business. A merchant selecting a

template still gets a static cadence applied uniformly across failure causes.



\*\*What it is not:\*\* a per-failure decision engine.



\---



\## Where this project sits



Not competing with any of the above. Two gaps survive the audit.



\### Gap 1 — allocation under a capped budget



NPCI guidelines effective 1 August 2025 permit one initial execution plus three retries per

mandate. Executions are barred from peak hours (10:00–13:00 and 17:00–21:30 IST). Every

attempt requires a pre-debit notification at least 24 hours in advance, and if the PDN fails

the debit fails.



Under those constraints there is almost no timing freedom. Razorpay's fixed daily schedule is

not naive — it is close to the regulatory floor. What remains open is not \*when\* to retry but

\*\*whether to spend an attempt at all\*\*, and the cause-blind baseline spends attempts on

failures that cannot succeed.



\### Gap 2 — the aggregation point



Every product above optimises for a single merchant. NPCI's stated rationale for the attempt

cap is network congestion, and PSPs are directed to initiate executions at moderated TPS with

rate limiters to avoid spikes. Retry admission control across merchants is visible only at the

gateway, and nothing shipped models it.



\---



\## The one-sentence boundary



Optimizer picks the gateway. The Intelligent Retry Engine picks the schedule. Neither picks which failures are worth an attempt — that's the gap this fills.

\---



\## Sources



\- Razorpay Docs — Subscriptions › Payment Retries

\- Razorpay Docs — Optimizer › About Optimizer, Dynamic Routing

\- Razorpay Docs — Payments › Late Authorisation

\- Razorpay Docs — Errors › Payment Method Error Parameters

\- Razorpay Blog — UPI Autopay with Intelligent Revenue-Protect (FTX 2026)

\- NPCI guidelines effective 1 Aug 2025, per press release dated 21 May 2025 (accessed via

&#x20; secondary sources; primary circular not publicly indexed)



---

## In-Session Retries (July 2026)

Razorpay shipped in-session retries: a **card** payment that fails while the customer
is still on the page is retried immediately, and the retries stay on the **same
Payment ID**.

**Boundary.** Every axis is the opposite of this project's:

| | In-Session Retries | This layer |
|---|---|---|
| Session | Customer present | Customer gone |
| Rail | Card | Mandate rails, UPI-weighted |
| Timing | Immediate, in-session | >=25h ahead, non-peak window |
| Identity | Same Payment ID | New payment, same order |
| Budget | Not NPCI-capped | 1 initial + 3 retries, ever |

It also sharpens the C10 boundary. In-session retries and Optimizer both operate while
the customer is on the page; C10 shapes the recovery link sent *after* they have left.
There is no overlap to negotiate — the two never see the same moment.

**What it does mean:** a card failure that reaches this system has already survived
in-session retry. It is a residual failure, not a first attempt, which makes the
population arriving here harder than the raw failure population.
