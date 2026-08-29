"""A single self-contained HTML page, generated from the same functions
`reproduce.py` calls -- not a second source of the numbers.

    python -m recovery.report          # writes reports/report.html, gitignored

Every figure on the page comes from `run_comparison`, `horizon_sweep`,
`sweep`, and `table_rows` -- the identical calls `recovery/reproduce.py`
makes, with the identical default seed and world count. There is no
templated-in constant anywhere in this module: if a figure changes, this
page changes with it on the next run, the same guarantee `reproduce.py`
already makes for the README.

**Self-contained by construction.** No CDN, no external font, no JavaScript
charting library -- inline `<style>` only, and the one chart on the page is
hand-built SVG from the same numbers the table above it prints. It opens
correctly with no network connection, which matters for the same reason
every other network-touching component in this project defaults to off: a
judge's clone should not depend on anything outside the repo to render what
it already computed.

This is presentation, not a new source of truth -- see `NOT_BUILT.md`,
"static HTML report", first on the cut list for exactly that reason. It
existed as an idea before it existed as code; this is what it looks like
built against the arm-C build that landed after that list was written.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import html
import pathlib

import typer

from allocator.arm_c import ArmC
from allocator.decisions import table_rows
from recovery.classifier import DEFAULT_CLASSIFIER_PATH, load_classifier
from recovery.config import DEFAULT_CONFIG_PATH, load_config
from recovery.sim.arms import ArmA, ArmB
from recovery.sim.calendar import calendar_from_config
from recovery.sim.environment import CaseOutcome, CaseView
from recovery.sim.horizon import horizon_sweep
from recovery.sim.run import run_comparison
from recovery.sim.sweep import SweepReport, stress_config, sweep
from recovery.sim.world import load_world_config, mandate_hazard_range, sample_world

app = typer.Typer(add_completion=False, help=__doc__)

SIM_SEED = 42  # matches recovery.reproduce.SIM_SEED -- the seed every README figure uses


def _challenges_entry_count() -> int:
    """Counted from the file, not hand-typed -- a hardcoded count is exactly
    the kind of figure this whole project exists to catch drifting. The
    README's own copy of this number was stale (said 18, file had 20) at the
    moment this function was written to replace it."""
    import re

    path = pathlib.Path("CHALLENGES.md")
    if not path.is_file():
        return 0
    return len(re.findall(r"^## \d{3} — ", path.read_text(encoding="utf-8"), re.MULTILINE))


@dataclasses.dataclass(frozen=True)
class ReportData:
    generated_at: str
    sweep_worlds: int
    challenges_count: int
    cells: list[tuple[str, str, str, bool, str]]
    money_rows: list[dict]
    uplift_ab: float
    uplift_bc: float
    crossover_b: str
    crossover_a: str
    value_table: dict[int, dict[str, tuple[float, float]]]
    nominal_breaks: dict[str, list[str]]
    terminal_trace: dict[str, str]


def _esc(text: object) -> str:
    return html.escape(str(text))


def build(sweep_worlds: int = 100) -> ReportData:
    """Compute everything the page shows, by calling exactly what
    `reproduce.py` calls -- see the module docstring."""
    config = load_config(DEFAULT_CONFIG_PATH)
    classifier = load_classifier(DEFAULT_CLASSIFIER_PATH)
    calendar = calendar_from_config(config.regulatory)
    arms = [ArmA(calendar), ArmB(calendar), ArmC(calendar, classifier, config)]

    raw = load_world_config()
    world = sample_world(seed=SIM_SEED, raw=raw)
    result = run_comparison(arms, world, calendar, costs=classifier.config.costs)
    hazard_range = mandate_hazard_range(load_world_config())

    money_rows = []
    for name in ("A", "B", "C"):
        row = result.row(name)
        money_rows.append(
            {
                "name": name,
                "recovered": row["money_recovered_inr"],
                "attempts": row["attempts_spent"],
                "contacts": row["contacts_sent"],
                "wasted_att": row["terminal_attempts_wasted"],
                "wasted_con": row["terminal_contacts_sent"],
            }
        )

    value_sweep = horizon_sweep(arms, world, calendar, hazard_range, costs=classifier.config.costs)
    value_table: dict[int, dict[str, tuple[float, float]]] = {}
    for months in (6, 12, 24):
        value_table[months] = {
            name: value_sweep.value_band_inr(name, months) for name in ("A", "B", "C")
        }

    # C8, nominal range set only -- the calibrated claim, not the stress edge.
    # Same call sweep() and stress_config() reproduce.py makes for this section.
    raw8 = dict(load_world_config())
    raw8["batch"] = {**raw8["batch"], "size": 200}
    outcomes = sweep(arms, calendar, raw8, worlds=sweep_worlds, costs=classifier.config.costs)
    report8 = SweepReport(label="nominal", worlds=sweep_worlds, outcomes=outcomes)
    nominal_breaks: dict[str, list[str]] = {}
    for incumbent in ("A", "B"):
        points = report8.breaking(incumbent)
        nominal_breaks[incumbent] = [p.describe() for p in points] or [
            "no parameter condition separates wins from losses"
        ]

    # The TERMINAL/HIGH cell -- built directly from ArmC.plan, the same unit
    # every allocator test exercises, rather than the full ingest pipeline
    # reproduce.py's C6 section runs. Same code path, lighter construction.
    view = CaseView(
        case_id="report_demo",
        rail="card",
        amount_paise=49900,
        failed_at=dt.datetime.now(dt.timezone.utc),
        observed={
            "method": "card",
            "error_source": "issuer_bank",
            "error_step": "payment_authorization",
            "error_reason": "payment_expired_card",
        },
        attempts_used=1,
        contacts_used=0,
        outcome=CaseOutcome.OPEN,
        attempt_pending=False,
        last_attempt_resolved_at=None,
    )
    plan = ArmC(calendar, classifier, config).plan(view, dt.datetime.now(dt.timezone.utc))
    terminal_trace = {
        "class": plan.classification.failure_class.value,
        "band": plan.classification.band.value,
        "confidence": f"{plan.classification.confidence:.2f}",
        "action": plan.action.value,
        "spends_execution": str(plan.spends_execution).lower(),
        "rationale": plan.cell.rationale if plan.cell else "",
    }

    return ReportData(
        generated_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        sweep_worlds=sweep_worlds,
        challenges_count=_challenges_entry_count(),
        cells=table_rows(),
        money_rows=money_rows,
        uplift_ab=result.uplift("B", "A"),
        uplift_bc=result.uplift("C", "B"),
        crossover_b=value_sweep.crossover("C", "B").describe(),
        crossover_a=value_sweep.crossover("C", "A").describe(),
        value_table=value_table,
        nominal_breaks=nominal_breaks,
        terminal_trace=terminal_trace,
    )


def _cells_table(cells: list[tuple[str, str, str, bool, str]]) -> str:
    rows = []
    for failure_class, band, action, spends_execution, rationale in cells:
        badge = (
            '<span class="badge exec">spends execution</span>'
            if spends_execution
            else '<span class="badge contact">contact only</span>'
        )
        rows.append(
            f"<tr><td><code>{_esc(failure_class)}</code></td>"
            f"<td><code>{_esc(band)}</code></td>"
            f"<td><code>{_esc(action)}</code></td>"
            f"<td>{badge}</td>"
            f"<td class='rationale'>{_esc(rationale)}</td></tr>"
        )
    return "\n".join(rows)


def _money_table(rows: list[dict]) -> str:
    out = []
    for row in rows:
        emph = ' class="hero"' if row["name"] == "C" else ""
        out.append(
            f"<tr{emph}><td>{_esc(row['name'])}</td>"
            f"<td>Rs {row['recovered']:,.2f}</td>"
            f"<td>{row['attempts']:,}</td>"
            f"<td>{row['contacts']:,}</td>"
            f"<td>{row['wasted_att']:,}</td>"
            f"<td>{row['wasted_con']:,}</td></tr>"
        )
    return "\n".join(out)


def _value_chart_svg(value_table: dict[int, dict[str, tuple[float, float]]]) -> str:
    """Grouped bars, one group per horizon, built directly from `value_table`.
    No charting library -- these coordinates are computed from the same
    numbers the table above prints, not decoration."""
    months = sorted(value_table)
    arms = ("A", "B", "C")
    colors = {"A": "#94a3b8", "B": "#64748b", "C": "#2563eb"}
    all_highs = [value_table[m][name][1] for m in months for name in arms]
    max_val = max(all_highs) if all_highs else 1
    width, height = 640, 260
    margin_left, margin_bottom = 70, 30
    plot_w = width - margin_left - 20
    plot_h = height - margin_bottom - 20
    group_w = plot_w / len(months)
    bar_w = group_w / (len(arms) + 1)

    bars = []
    for gi, m in enumerate(months):
        group_x = margin_left + gi * group_w
        for ai, name in enumerate(arms):
            low, high = value_table[m][name]
            bar_h_low = (low / max_val) * plot_h
            bar_h_high = (high / max_val) * plot_h
            x = group_x + ai * bar_w + bar_w * 0.3
            y_high = 20 + plot_h - bar_h_high
            bars.append(
                f'<rect x="{x:.1f}" y="{y_high:.1f}" width="{bar_w * 0.8:.1f}" '
                f'height="{bar_h_high - bar_h_low + 4:.1f}" fill="{colors[name]}" rx="2">'
                f"<title>{name} at {m}mo: Rs {low:,.0f}-{high:,.0f}</title></rect>"
            )
        bars.append(
            f'<text x="{group_x + group_w / 2:.1f}" y="{height - 6}" '
            f'text-anchor="middle" class="axis-label">{m} mo</text>'
        )

    legend = "".join(
        f'<rect x="{20 + i * 90}" y="4" width="10" height="10" fill="{colors[a]}"/>'
        f'<text x="{34 + i * 90}" y="13" class="axis-label">{a}</text>'
        for i, a in enumerate(arms)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
        f'aria-label="Batch value by remaining lifetime, arms A B C">'
        f"{legend}{''.join(bars)}"
        f'<line x1="{margin_left}" y1="{20 + plot_h}" x2="{width - 20}" '
        f'y2="{20 + plot_h}" stroke="#cbd5e1"/></svg>'
    )


def render(data: ReportData) -> str:
    breaks_a = "".join(f"<li>{_esc(b)}</li>" for b in data.nominal_breaks["A"])
    breaks_b = "".join(f"<li>{_esc(b)}</li>" for b in data.nominal_breaks["B"])

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Payment Recovery Allocator -- Report</title>
<style>
  :root {{
    --ink: #1e293b; --muted: #64748b; --bg: #ffffff; --panel: #f8fafc;
    --border: #e2e8f0; --accent: #2563eb; --accent-bg: #eff6ff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    color: var(--ink); background: var(--bg); margin: 0; line-height: 1.55;
  }}
  .wrap {{ max-width: 900px; margin: 0 auto; padding: 40px 24px 80px; }}
  h1 {{ font-size: 26px; margin: 0 0 4px; }}
  h2 {{ font-size: 18px; margin: 40px 0 12px; border-top: 1px solid var(--border);
        padding-top: 24px; }}
  .subtitle {{ color: var(--muted); font-size: 14px; margin-bottom: 28px; }}
  .meta {{ color: var(--muted); font-size: 12px; font-family: ui-monospace, monospace; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
  th, td {{ text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border); }}
  th {{ color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase;
        letter-spacing: 0.03em; }}
  tr.hero td {{ font-weight: 700; background: var(--accent-bg); }}
  td.rationale {{ color: var(--muted); font-size: 12.5px; }}
  code {{ background: var(--panel); padding: 1px 5px; border-radius: 3px; font-size: 12px; }}
  .badge {{ font-size: 11px; padding: 2px 8px; border-radius: 10px; white-space: nowrap; }}
  .badge.exec {{ background: #fef3c7; color: #92400e; }}
  .badge.contact {{ background: #dbeafe; color: #1e40af; }}
  .callout {{ background: var(--accent-bg); border-left: 3px solid var(--accent);
              padding: 14px 18px; margin: 16px 0; font-size: 14px; }}
  .callout code {{ background: #ffffff; }}
  .trace {{ background: var(--panel); border: 1px solid var(--border); border-radius: 6px;
            padding: 16px 20px; font-family: ui-monospace, monospace; font-size: 12.5px; }}
  .trace dt {{ color: var(--muted); float: left; width: 130px; }}
  .trace dd {{ margin: 0 0 6px 130px; }}
  .chart {{ width: 100%; height: auto; }}
  .axis-label {{ font-size: 10px; fill: var(--muted); }}
  ul.breaks {{ font-size: 13.5px; margin: 4px 0; padding-left: 20px; }}
  footer {{ margin-top: 48px; padding-top: 20px; border-top: 1px solid var(--border);
            color: var(--muted); font-size: 12px; }}
  footer a {{ color: var(--accent); }}
</style>
</head>
<body>
<div class="wrap">

  <h1>Payment Recovery Allocator</h1>
  <div class="subtitle">Mandate retry sequencer -- Razorpay AI Builder Internship 2026, Track 03</div>
  <div class="meta">Generated {_esc(data.generated_at)} by <code>python -m recovery.report</code>
    &middot; seed {SIM_SEED} &middot; {data.sweep_worlds} sampled worlds &middot;
    every figure below is computed by the same calls <code>reproduce.py</code> makes</div>

  <h2>The decision table -- C3</h2>
  <table>
    <tr><th>Class</th><th>Band</th><th>Action</th><th></th><th>Rationale</th></tr>
    {_cells_table(data.cells)}
  </table>

  <h2>Money recovered across the batch -- one cycle, seed {SIM_SEED}</h2>
  <table>
    <tr><th>Arm</th><th>Recovered</th><th>Attempts</th><th>Contacts</th>
        <th>Wasted attempts</th><th>Wasted contacts</th></tr>
    {_money_table(data.money_rows)}
  </table>
  <div class="callout">
    A&rarr;B contact uplift: Rs {data.uplift_ab:,.2f}. B&rarr;C cause-awareness uplift:
    Rs {data.uplift_bc:,.2f}. Arm C recovers less in one cycle, on purpose -- see the
    horizon below.
  </div>

  <h2>Batch value by remaining lifetime</h2>
  {_value_chart_svg(data.value_table)}
  <div class="callout">
    {_esc(data.crossover_b)}<br>{_esc(data.crossover_a)}
  </div>

  <h2>The cell the thesis rests on</h2>
  <div class="trace">
    <dt>class</dt><dd>{_esc(data.terminal_trace['class'])} at {_esc(data.terminal_trace['band'])}
      (confidence {_esc(data.terminal_trace['confidence'])})</dd>
    <dt>action</dt><dd>{_esc(data.terminal_trace['action'])}</dd>
    <dt>spends_execution</dt><dd>{_esc(data.terminal_trace['spends_execution'])}</dd>
    <dt>rationale</dt><dd>{_esc(data.terminal_trace['rationale'])}</dd>
  </div>

  <h2>Where it breaks -- C8, nominal range, {data.sweep_worlds} worlds</h2>
  <p style="font-size:13.5px">Against Arm A:</p>
  <ul class="breaks">{breaks_a}</ul>
  <p style="font-size:13.5px">Against Arm B:</p>
  <ul class="breaks">{breaks_b}</ul>

  <footer>
    Full reasoning, the {data.challenges_count}-entry build log, and every source: see
    <code>README.md</code>, <code>CHALLENGES.md</code>, <code>ASSUMPTIONS.md</code> in the
    repository this page was generated from. Reproduce these exact figures with
    <code>python -m recovery.reproduce</code>.
  </footer>

</div>
</body>
</html>
"""


@app.command()
def main(
    out: pathlib.Path = typer.Option(pathlib.Path("reports/report.html"), "--out"),
    sweep_worlds: int = typer.Option(100, "--sweep-worlds"),
) -> None:
    typer.echo(f"Computing report data ({sweep_worlds} sampled worlds for C8)...")
    data = build(sweep_worlds=sweep_worlds)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data), encoding="utf-8")
    typer.echo(f"Wrote {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    app()
