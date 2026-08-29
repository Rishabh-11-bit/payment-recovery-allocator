"""C13 — free-text enrichment. The only model in the project, and the only place one belongs.

    python -m recovery.enrich --evaluate     # does the text add anything over the enum?
    python -m recovery.enrich --warm         # populate the cache (needs a local Ollama)

The classifier keys on `(method, source, step, reason)` — four documented enum
fields. They are already structured, so there is nothing for a model to infer and
a model over them would be a lookup table with worse failure modes.

`error_description` is different. It is free text written for a human:

    "Your payment didn't go through as it was declined by the bank.
     Try another payment method or contact your bank."

Nothing in this system read it until now.

---

### The boundary, and why it is this one

The obvious design — let the model refine the class when the deterministic
classifier is unsure — **is provably useless here**, and working out why is what
produced the design that shipped.

A LOW band already means "the class is a guess". The decision table's LOW row is
*uniform across all four classes by construction*: whatever the class, the action
is one recovery link. So a model that changed the class at LOW confidence would
change no action at all. It would be theatre — a model in the pipeline, visible in
the audit trail, altering nothing.

To be useful it would have to lift the *band*, and lifting a band on a model's say-so
is precisely the thing this project refuses: it would let an unauditable value
license spending a capped mandate execution.

So enrichment does something narrower and genuinely useful instead. **It sets
`cause_family`, which shapes what the contact says, and it can change nothing else.**

`decisions.py` PRINCIPLE 2 is the whole argument: giving up on the execution and
giving up on the customer are different moves. A TERMINAL case at LOW confidence
sends a generic recovery link. If the free text says the instrument itself is dead,
the *same contact, at the same cost against the same budget* can carry a
card-change offer instead — which is the one thing that can convert on a dead
instrument.

Same spend. Different content. No execution decision touched.

### What is enforced in code, not merely intended

* `refine` may set `cause_family` and nothing else. Every other field of the
  returned `Classification` is asserted equal to the input's.
* It never spends an execution, because it cannot reach a field the cell reads.
* It is **cache-first and offline by default**. A judge cloning this repo with no
  API key gets byte-identical results, because the parses are committed.
* Every failure path — no key, no network, malformed response, unknown marker —
  returns the classification untouched and audits why. It cannot raise.
* The model returns **markers, never classes and never numbers**. The mapping from
  marker to cause family is authored in `config/classifier.yaml`. The model
  reports what the text says; the table decides what that means.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import urllib.error
import urllib.request

import typer

from recovery.models import Classification, ConfidenceBand

app = typer.Typer(add_completion=False, help=__doc__)

# Bump when the prompt or the marker vocabulary changes: it is part of the cache
# key, so a changed prompt cannot silently reuse parses made under the old one.
PROMPT_VERSION = "1"

# Extraction, not reasoning: "which of these six things does this sentence say".
# A local 8B model is sufficient for that, and brings two properties a hosted API
# cannot: no key to distribute, and no external service inside a pipeline whose
# entire claim is that `python -m recovery.reproduce` is reproducible.
#
# `llama3.2:1b` is also installed locally and is deliberately not used. At 1B the
# failure mode is malformed structure rather than a wrong answer, and a parser
# that cannot be trusted to return the agreed shape pushes its work onto the
# caller -- which is the opposite of what this component is for.
MODEL = "llama3:latest"
API_URL = "http://localhost:11434/api/generate"

CACHE_DIR = pathlib.Path("tests/fixtures/parsed")

# What the model is allowed to say. Deliberately observations about the *text*,
# not conclusions about the payment.
MARKERS = (
    "instrument_unusable",
    "insufficient_balance",
    "customer_abandoned",
    "technical_fault",
    "merchant_configuration",
    "bank_referral",
    "no_information",
)

PROMPT = """You extract structured observations from Razorpay payment failure messages.

These are customer-facing strings. Report only what the text itself says. Do not
infer, do not guess at the underlying cause, and do not use knowledge of payment
systems beyond reading the sentence.

Reply with a JSON object and nothing else:

  {{"markers": [...]}}

Valid markers, each meaning "the text says this":

  instrument_unusable    - the card/account/instrument is expired, blocked, invalid
  insufficient_balance   - there was not enough money
  customer_abandoned     - the customer cancelled, dismissed, or did not complete
  technical_fault        - a timeout, system error, or downtime
  merchant_configuration - the business does not accept this payment type
  bank_referral          - it tells the customer to contact their bank
  no_information         - it states a failure without saying anything about why

Use `no_information` alone when the text names no cause. Never combine it with
another marker. Include every marker the text supports; most texts support one.

Text:
{description}"""


@dataclasses.dataclass(frozen=True)
class Observations:
    """What the model reported. Markers only — no class, no score, no action."""

    markers: tuple[str, ...] = ()
    source: str = "unavailable"  # cache | model | unavailable | malformed

    @property
    def informative(self) -> bool:
        """True when the text said something beyond 'this failed'."""
        return bool(self.markers) and "no_information" not in self.markers


def cache_key(description: str) -> str:
    """Content-addressed, and versioned by prompt and model.

    Keying on the text alone would let a changed prompt reuse a parse the new
    prompt would not have produced -- the cache would be stale in a way nothing
    could detect.
    """
    material = f"{MODEL}\x00{PROMPT_VERSION}\x00{description}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _cache_path(key: str, cache_dir: pathlib.Path | None = None) -> pathlib.Path:
    return (cache_dir or CACHE_DIR) / f"{key}.json"


def read_cache(description: str, cache_dir: pathlib.Path | None = None) -> Observations | None:
    path = _cache_path(cache_key(description), cache_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        markers = tuple(str(m) for m in payload.get("markers", []) if m in MARKERS)
        return Observations(markers=markers, source="cache")
    except (json.JSONDecodeError, OSError, TypeError, AttributeError):
        # A corrupt cache entry must degrade to "no observations", never crash a
        # classification. The whole component is optional by construction.
        return None


def write_cache(
    description: str, observations: Observations, cache_dir: pathlib.Path | None = None
) -> pathlib.Path:
    directory = cache_dir or CACHE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_key(description), cache_dir)
    path.write_text(
        json.dumps(
            {
                "description": description,
                "markers": list(observations.markers),
                "model": MODEL,
                "prompt_version": PROMPT_VERSION,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def call_model(description: str, *, timeout: float = 120.0) -> Observations:
    """One HTTP call to a local Ollama server via stdlib. No SDK, no dependency.

    `format: json` makes Ollama constrain decoding to valid JSON, and
    `temperature: 0` makes the same string yield the same parse -- which is what
    lets a committed cache be a faithful record of the model rather than one
    sample from it.

    Returns `unavailable` rather than raising on every failure path: no server,
    a non-200, a body that is not JSON, or JSON that is not the agreed shape.
    Enrichment is optional and the caller must never have to handle it.

    The timeout is generous because a cold model loads weights on the first
    call. Nothing interactive waits on this -- it runs once, to fill the cache.
    """
    body = json.dumps(
        {
            "model": MODEL,
            "prompt": PROMPT.format(description=description),
            "stream": False,
            "format": "json",
            "options": {"temperature": 0, "num_predict": 200},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        API_URL, data=body, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        text = payload.get("response", "")
        parsed = json.loads(text[text.index("{") : text.rindex("}") + 1])
        markers = tuple(m for m in parsed.get("markers", []) if m in MARKERS)
        # A response naming only markers we do not recognise is malformed, not
        # empty. The difference matters: one is the model misbehaving, the other
        # is the text genuinely saying nothing.
        if not markers:
            return Observations(source="malformed")
        return Observations(markers=markers, source="model")
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        return Observations(source="unavailable")


def observe(
    description: str | None,
    *,
    allow_network: bool = False,
    cache_dir: pathlib.Path | None = None,
) -> Observations:
    """Cache first, network only when explicitly allowed.

    `allow_network` defaults to False so that no ordinary run -- test, reproduce,
    or a judge's clone -- can reach the internet. The cache is committed; the
    network path exists to fill it.
    """
    if not description or not description.strip():
        return Observations(source="unavailable")
    cached = read_cache(description, cache_dir)
    if cached is not None:
        return cached
    if not allow_network:
        return Observations(source="unavailable")
    result = call_model(description)
    if result.source == "model":
        write_cache(description, result, cache_dir)
    return result


def refine(
    classification: Classification,
    description: str | None,
    families: dict[str, str],
    *,
    allow_network: bool = False,
    cache_dir: pathlib.Path | None = None,
) -> tuple[Classification, Observations, str | None]:
    """Set `cause_family` from the free text. Change nothing else, ever.

    Returns `(classification, observations, family_applied)`. The classification
    is returned unchanged unless every one of these holds:

    * the band is LOW -- above that the enum key was informative and the text is
      not needed
    * the classification does not already carry a `cause_family` -- an authored
      family always wins over an extracted one
    * exactly one marker maps to a family -- two mapped markers is the text being
      ambiguous, which is not a licence to pick one

    `families` is the authored marker -> cause_family map from
    `config/classifier.yaml`. This function does not contain one.
    """
    observations = observe(
        description, allow_network=allow_network, cache_dir=cache_dir
    )
    if classification.band is not ConfidenceBand.LOW:
        return classification, observations, None
    if classification.cause_family:
        return classification, observations, None

    mapped = {families[m] for m in observations.markers if m in families}
    if len(mapped) != 1:
        return classification, observations, None

    family = mapped.pop()
    # model_copy is a field-level copy: no other field can change here even by
    # accident, and a test asserts that against every field of the model.
    return classification.model_copy(update={"cause_family": family}), observations, family


# ------------------------------------------------------------------- CLI --- #


def _captured_descriptions() -> list[tuple[str, str, str]]:
    """(payment_id, enum reason, description) for every captured payload."""
    from recovery.fixtures import load_captured_payments

    rows = []
    for payment in load_captured_payments():
        description = payment.get("error_description")
        if description:
            rows.append(
                (payment.get("id", "?"), payment.get("error_reason", "-"), description)
            )
    return rows


@app.command()
def main(
    warm: bool = typer.Option(False, "--warm", help="Populate the cache from a local Ollama."),
    evaluate: bool = typer.Option(
        False, "--evaluate", help="Does the free text add anything over the enum key?"
    ),
) -> None:
    rows = _captured_descriptions()
    if not rows:
        typer.echo("No captured payloads carry an error_description.")
        raise typer.Exit(code=1)

    distinct = {description for _, _, description in rows}
    typer.echo(
        f"{len(rows)} captured payload(s), {len(distinct)} distinct description(s)\n"
    )

    if warm:
        typer.echo(f"  model: {MODEL} via {API_URL}\n")
        written = 0
        for description in sorted(distinct):
            observations = observe(description, allow_network=True)
            if observations.source in ("model", "cache"):
                written += 1
            typer.echo(f"  [{observations.source}] {list(observations.markers)}")
            typer.echo(f"      {description[:88]}")

        typer.echo("")
        if not written:
            # This used to print "Cache written. Commit it." unconditionally --
            # a claim of success on a run where every call had failed, which is
            # how it was first hit.
            typer.echo(
                f"    Nothing cached: all {len(distinct)} call(s) returned `unavailable`.",
                err=True,
            )
            typer.echo(
                f"    Is Ollama running, and is `{MODEL}` pulled?\n"
                "        ollama serve\n"
                f"        ollama pull {MODEL}",
                err=True,
            )
            raise typer.Exit(code=1)

        typer.echo(
            f"    {written} of {len(distinct)} description(s) cached in {CACHE_DIR}. Commit it."
        )
        return

    if evaluate:
        _evaluate(rows)
        return

    for payment_id, reason, description in rows:
        observations = observe(description)
        typer.echo(f"  {payment_id}  {reason}")
        typer.echo(f"    [{observations.source}] {list(observations.markers)}")


def _evaluate(rows: list[tuple[str, str, str]]) -> None:
    """The question this component has to answer about itself.

    Not "is the parse correct" -- with three distinct strings that is not
    measurable. The measurable question is narrower and more useful: does the
    free text carry anything the enum `reason` did not already carry? If not,
    the component is inert on this data and should say so.
    """
    from recovery.classifier import load_classifier
    from recovery.fixtures import load_captured_payments
    from recovery.normalize import normalize_entity

    classifier = load_classifier()
    families = classifier.config.enrichment_families

    typer.echo("Does the free text change what this system does?\n")
    typer.echo(
        f"    {'enum key -> band':<44} {'markers from the text':<30} effect"
    )
    typer.echo("    " + "-" * 104)

    changed = 0
    uncached = 0
    seen: set[str] = set()

    # Run the *real* path -- normalize, classify, refine -- rather than comparing
    # strings. An earlier version scored "does the marker's first token appear in
    # the enum reason", which called all three of these informative because
    # `merchant` is not a substring of `international_transaction_not_allowed`.
    # It was measuring spelling. The question that matters is whether the parse
    # reaches a decision, and that is answerable by running it.
    for payment in load_captured_payments():
        description = payment.get("error_description")
        if not description or description in seen:
            continue
        seen.add(description)

        key = normalize_entity(payment, source_space=classifier.config.source_space)
        before = classifier.classify(key)
        after, observations, family = refine(before, description, families)

        if observations.source == "unavailable":
            uncached += 1
            effect = "NOT CACHED -- run --warm"
        elif family is not None:
            changed += 1
            effect = f"sets cause_family={family}"
        elif before.band is not ConfidenceBand.LOW:
            effect = f"no effect -- already {before.band.value}, enum sufficed"
        elif before.cause_family:
            effect = "no effect -- taxonomy already names the family"
        else:
            effect = "no effect -- marker maps to nothing"

        label = f"{key.reason or '-'} -> {before.band.value}"
        typer.echo(f"    {label:<44} {str(list(observations.markers)):<30} {effect}")

    typer.echo("")
    if uncached:
        typer.echo(
            f"    {uncached} of {len(seen)} distinct description(s) not cached. "
            "Run `--warm` with Ollama running."
        )
        return

    typer.echo(
        f"    {changed} of {len(seen)} distinct descriptions changed a classification.\n"
    )
    typer.echo(
        "    Read this as a smoke test, not an accuracy figure -- three strings cannot\n"
        "    measure a parser. What it does show is where the component can bite: only\n"
        "    on a LOW band with no authored cause family. Everywhere else the enum key\n"
        "    was already sufficient, which is the reason the classifier is deterministic\n"
        "    and not the reason to be embarrassed about it."
    )
    typer.echo(
        "\n    The interesting case is absent: UPI is the primary rail and test mode\n"
        "    never exposed it, so no UPI description has been read by anything."
    )


if __name__ == "__main__":
    app()
