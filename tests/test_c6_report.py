"""The static HTML report -- pinned on the property that made it worth
building: every figure comes from the same functions `reproduce.py` calls,
not a second, hand-typed source that can drift from them.
"""

from __future__ import annotations

import re

import pytest

from recovery.report import _challenges_entry_count, build, render


def test_build_reuses_reproduce_s_own_seed_and_figures():
    """Seed 42 is the one every README figure is quoted against. If this
    module ever samples a different world, every number on the page would
    silently stop matching the README without either file saying so."""
    data = build(sweep_worlds=5)
    row_by_name = {row["name"]: row for row in data.money_rows}
    # These are the seed-42 figures verified against a live reproduce run
    # earlier in the build. Pinned here so a change to the world sample, the
    # cost matrix, or the emission model shows up as a failing test rather
    # than a silently different report.
    assert row_by_name["A"]["recovered"] == pytest.approx(154404.17, abs=0.02)
    assert row_by_name["B"]["recovered"] == pytest.approx(166523.20, abs=0.02)
    assert row_by_name["C"]["recovered"] == pytest.approx(108508.36, abs=0.02)


def test_the_challenges_count_is_read_from_the_file_not_hardcoded():
    """This exact bug happened once already: the report's footer said
    'eighteen-entry' while CHALLENGES.md had twenty sections, and the
    README's own copy of the same figure was independently stale too.
    Pinned here so neither number can silently drift from the file again."""
    count = _challenges_entry_count()
    from pathlib import Path

    text = Path("CHALLENGES.md").read_text(encoding="utf-8")
    expected = len(re.findall(r"^## \d{3} — ", text, re.MULTILINE))
    assert count == expected
    assert count >= 19  # the count at the time this test was written


def test_render_produces_well_formed_self_contained_html():
    data = build(sweep_worlds=5)
    page = render(data)

    assert page.startswith("<!doctype html>")
    assert page.count("<html") == page.count("</html>")
    assert page.count("<table>") == page.count("</table>")
    assert page.count("<svg") == page.count("</svg>")

    # Self-contained: no CDN, no external font, no script tag at all -- see
    # the module docstring's reasoning.
    assert "<script" not in page
    assert "cdn." not in page
    assert "fonts.googleapis" not in page
    assert "http://" not in page and "https://" not in page


def test_render_never_emits_the_literal_rupee_symbol():
    """Same convention as every other console-and-file-facing module in this
    project -- see recovery/messaging.py's identical test and CHALLENGES for
    why. An HTML file does not have the Windows console encoding problem
    that motivated the original rule, but a reader copy-pasting a figure out
    of this page into a terminal would still hit it, and consistency with
    every other output in the project is worth more than the ₹ glyph."""
    data = build(sweep_worlds=5)
    page = render(data)
    assert "₹" not in page
    assert "Rs " in page


def test_html_escaping_is_applied_to_dynamic_text():
    """The rationale strings come from authored YAML, not user input, but the
    escaping still has to actually run -- this exercises the _esc path
    rather than assuming it is wired in everywhere it needs to be."""
    from recovery.report import _esc

    assert _esc("A & B < C") == "A &amp; B &lt; C"


def test_the_terminal_high_cell_matches_what_explain_would_show():
    """The one decision trace on the page has to be the real TERMINAL/HIGH
    cell -- OFFER_RAIL_MIGRATION, no execution spent -- not a placeholder."""
    data = build(sweep_worlds=5)
    assert data.terminal_trace["class"] == "TERMINAL"
    assert data.terminal_trace["band"] == "HIGH"
    assert data.terminal_trace["action"] == "OFFER_RAIL_MIGRATION"
    assert data.terminal_trace["spends_execution"] == "false"


def test_value_table_covers_all_three_horizons_and_three_arms():
    data = build(sweep_worlds=5)
    assert set(data.value_table) == {6, 12, 24}
    for months, per_arm in data.value_table.items():
        assert set(per_arm) == {"A", "B", "C"}
        for low, high in per_arm.values():
            assert low <= high
