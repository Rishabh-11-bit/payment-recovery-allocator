"""C9 -- calibration sources, their inventory, and named profiles.

Two jobs, deliberately separated:

**Inventory** reads the files in `data/` and reports what figures they actually
contain. Mechanical, no interpretation: it says a workbook has a bank name, an
incident count and a downtime duration, not what any of that implies.

**Profiles** are the seam between published figures and simulator parameters. A
profile names a failure-class mix and records which figures it derives from, so
`worlds.yaml` carries a profile name rather than inline constants and any
reported mix can be traced to the sources behind it -- or, where there are none,
to a profile that says so.

## Why the mapping is not here

Published data does not split along INFRASTRUCTURE / LIQUIDITY / ATTENTION /
TERMINAL, because nobody reports it that way. Razorpay's own figure lumps three
of the four into one number: "around 20% of subsequent debits fail due to
insufficient balance, bank downtime, or cancelled mandates." Deriving four
classes from that requires interpretation, and **an interpreted number presented
as sourced is worse than an honest invention** -- an invention is labelled.

So a profile's `derives_from` records the figures behind it, and a profile whose
`status` is UNCALIBRATED says plainly that its numbers are chosen, not derived.
The mapping itself is hand-authored.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import yaml

from recovery import xlsx

DATA_DIR = pathlib.Path("data")
PROFILE_DIR = pathlib.Path("config/calibration")

DURATION = re.compile(r"^(\d+):(\d{2})$")
MONTH_DAYS = {
    "January": 31, "February": 28, "March": 31, "April": 30,
    "May": 31, "June": 30, "July": 31, "August": 31,
    "September": 30, "October": 31, "November": 30, "December": 31,
}


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SheetInventory:
    name: str
    columns: tuple[str, ...]
    data_rows: int


@dataclass(frozen=True)
class FileInventory:
    path: pathlib.Path
    kind: str
    sheets: tuple[SheetInventory, ...] = ()
    machine_read: bool = True
    note: str = ""

    @property
    def name(self) -> str:
        return self.path.name


def inventory(directory: pathlib.Path | str = DATA_DIR) -> list[FileInventory]:
    """What each file contains. Structure only -- no interpretation."""
    directory = pathlib.Path(directory)
    found: list[FileInventory] = []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() == ".xlsx":
            sheets = []
            for sheet in xlsx.read(path):
                rows = sheet.non_empty_rows()
                header = tuple(rows[0]) if rows else ()
                sheets.append(
                    SheetInventory(
                        name=sheet.name,
                        columns=header,
                        data_rows=max(0, len(rows) - 1),
                    )
                )
            found.append(FileInventory(path=path, kind="xlsx", sheets=tuple(sheets)))
        elif path.suffix.lower() == ".pdf":
            found.append(
                FileInventory(
                    path=path,
                    kind="pdf",
                    machine_read=False,
                    note=(
                        "Not machine-parsed. Reading it needs a PDF dependency that is "
                        "not pinned, and the figures are prose rather than tabular, so "
                        "they are transcribed into the sidecar by hand with quotes."
                    ),
                )
            )
    return found


# --------------------------------------------------------------------------- #
# Downtime structure -- mechanical summary of the NPCI workbooks
# --------------------------------------------------------------------------- #

# Bank names are inconsistently spelled across months in the published files:
# "Ltd" vs "Limited", "Grameen" vs "Grameena", a stray lowercase word. Counting
# distinct banks without normalising overstates the count and, worse, makes a
# bank that appeared every month look intermittent.
_SUFFIX = re.compile(r"\b(limited|ltd\.?)\b")
_PARENS = re.compile(r"\(.*?\)")


def normalise_bank(name: str) -> str:
    text = _PARENS.sub(" ", name.strip().lower())
    text = _SUFFIX.sub("ltd", text)
    text = re.sub(r"\bgrameena\b", "grameen", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class BankMonth:
    month: str
    bank: str
    normalised: str
    incidents: int
    downtime_hours: float

    def share_of_month(self, days: int) -> float:
        return self.downtime_hours / (days * 24)


def parse_duration_hours(text: str) -> float | None:
    match = DURATION.match(text.strip())
    if not match:
        return None
    return int(match.group(1)) + int(match.group(2)) / 60


def read_downtime(directory: pathlib.Path | str = DATA_DIR) -> list[BankMonth]:
    """Every (month, bank) row across the downtime workbooks."""
    directory = pathlib.Path(directory)
    entries: list[BankMonth] = []
    for path in sorted(directory.glob("*downtime*.xlsx")):
        match = re.search(r"downtime-([A-Za-z]+)-(\d{4})", path.name)
        month = match.group(1) if match else path.stem
        for row in xlsx.read(path)[0].non_empty_rows()[1:]:
            if len(row) < 4:
                continue
            hours = parse_duration_hours(row[3])
            if hours is None:
                continue
            entries.append(
                BankMonth(
                    month=month,
                    bank=row[1].strip(),
                    normalised=normalise_bank(row[1]),
                    incidents=int(row[2]) if row[2].strip().isdigit() else 0,
                    downtime_hours=hours,
                )
            )
    return entries


@dataclass(frozen=True)
class DowntimeSummary:
    months: tuple[str, ...]
    per_month: Mapping[str, dict[str, float]]
    distinct_banks_raw: int
    distinct_banks_normalised: int
    appear_once: int
    appear_every_month: tuple[str, ...]
    merged_spellings: tuple[tuple[str, ...], ...]
    worst: tuple[tuple[float, str, str, float], ...]


def summarise_downtime(entries: Sequence[BankMonth]) -> DowntimeSummary:
    months = tuple(sorted({e.month for e in entries}, key=_month_order))
    per_month: dict[str, dict[str, float]] = {}
    worst: list[tuple[float, str, str, float]] = []

    for month in months:
        rows = [e for e in entries if e.month == month]
        days = MONTH_DAYS.get(month, 30)
        shares = [e.share_of_month(days) * 100 for e in rows]
        per_month[month] = {
            "banks": float(len(rows)),
            "incidents": float(sum(e.incidents for e in rows)),
            "total_hours": sum(e.downtime_hours for e in rows),
            "mean_share_pct": sum(shares) / len(shares) if shares else 0.0,
            "min_share_pct": min(shares) if shares else 0.0,
            "max_share_pct": max(shares) if shares else 0.0,
        }
        worst.extend(
            (e.share_of_month(days) * 100, month, e.bank, e.downtime_hours) for e in rows
        )

    counts: dict[str, set[str]] = {}
    spellings: dict[str, set[str]] = {}
    for entry in entries:
        counts.setdefault(entry.normalised, set()).add(entry.month)
        spellings.setdefault(entry.normalised, set()).add(entry.bank)

    return DowntimeSummary(
        months=months,
        per_month=per_month,
        distinct_banks_raw=len({e.bank for e in entries}),
        distinct_banks_normalised=len(counts),
        appear_once=sum(1 for seen in counts.values() if len(seen) == 1),
        appear_every_month=tuple(
            sorted(name for name, seen in counts.items() if len(seen) == len(months))
        ),
        merged_spellings=tuple(
            tuple(sorted(names)) for names in spellings.values() if len(names) > 1
        ),
        worst=tuple(sorted(worst, reverse=True)),
    )


def _month_order(name: str) -> int:
    order = list(MONTH_DAYS)
    return order.index(name) if name in order else 99


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DerivedFigure:
    """One published figure a profile leans on, and how directly."""

    source_file: str
    figure: str
    provenance: str  # "primary" | "secondary"
    used_for: str = ""


@dataclass(frozen=True)
class CalibrationProfile:
    name: str
    status: str
    description: str
    class_mix: Mapping[str, tuple[float, float]]
    derives_from: tuple[DerivedFigure, ...] = ()
    notes: str = ""
    interpretation: str = ""
    sampling: str = "independent"
    named_classes: tuple[str, ...] = ()
    named_bounds: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    attention_share: tuple[float, float] | None = None
    issuer_outage: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_calibrated(self) -> bool:
        return self.status.upper() != "UNCALIBRATED"

    def sample_mix(self, rng) -> dict[str, float]:
        """Draw one failure-class mix.

        `independent` draws each class from its own range and normalises. That
        is the original behaviour and is retained exactly, RNG draw order
        included, so switching a profile does not silently move a result.

        `simplex` draws the residual class from its bounded range and splits the
        remaining mass uniformly over the simplex across the named classes.
        Independent ranges then normalised would concentrate near the middle of
        the box -- a point estimate wearing a range's clothes -- which defeats
        the purpose of sweeping a split nobody has published.
        """
        if self.sampling != "simplex":
            drawn = {
                key: (low if low == high else rng.uniform(low, high))
                for key, (low, high) in self.class_mix.items()
            }
            total = sum(drawn.values())
            if total <= 0:
                raise CalibrationError(f"{self.name}: class_mix sums to {total}")
            return {key: value / total for key, value in drawn.items()}

        if not self.named_classes or self.attention_share is None:
            raise CalibrationError(
                f"{self.name}: simplex sampling needs `named_classes` and "
                "`attention_share`"
            )

        low, high = self.attention_share
        residual = low if low == high else rng.uniform(low, high)
        remaining = 1.0 - residual

        # Uniform over the simplex: exponential draws, normalised. Rejection
        # against the clamps rather than clipping, because clipping piles mass
        # onto the bounds and would make the excluded corners look common.
        for _ in range(200):
            weights = [rng.expovariate(1.0) for _ in self.named_classes]
            total = sum(weights)
            shares = {
                name: weight / total * remaining
                for name, weight in zip(self.named_classes, weights)
            }
            if all(
                self.named_bounds.get(name, (0.0, 1.0))[0]
                <= share / remaining
                <= self.named_bounds.get(name, (0.0, 1.0))[1]
                for name, share in shares.items()
            ):
                break
        shares["ATTENTION"] = residual
        return shares

    def provenance_summary(self) -> str:
        if not self.derives_from:
            return "no published figure behind these numbers"
        primary = sum(1 for f in self.derives_from if f.provenance == "primary")
        secondary = len(self.derives_from) - primary
        return f"{primary} primary, {secondary} secondary figure(s)"


class CalibrationError(ValueError):
    pass


def load_profile(
    name: str, *, directory: pathlib.Path | str = PROFILE_DIR
) -> CalibrationProfile:
    path = pathlib.Path(directory) / f"{name}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in pathlib.Path(directory).glob("*.yaml"))
        raise CalibrationError(
            f"calibration profile {name!r} not found in {directory}. Available: {available}"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise CalibrationError(f"{path} is not a mapping")

    mix = raw.get("class_mix") or {}
    if not mix:
        raise CalibrationError(f"{path} declares no class_mix")

    status = str(raw.get("status", "UNCALIBRATED"))
    if status.upper() != "UNCALIBRATED" and not str(raw.get("interpretation", "")).strip():
        raise CalibrationError(
            f"{path} claims status {status!r} but records no `interpretation`. A profile "
            "that cites sources without stating what it inferred from them is exactly the "
            "failure this field exists to prevent."
        )

    return CalibrationProfile(
        name=raw.get("name", name),
        status=status,
        description=str(raw.get("description", "")),
        class_mix={
            key: (float(value[0]), float(value[1])) for key, value in mix.items()
        },
        sampling=str(raw.get("sampling", "independent")),
        named_classes=tuple(raw.get("named_classes") or ()),
        named_bounds={
            key: (float(value[0]), float(value[1]))
            for key, value in (raw.get("named_bounds") or {}).items()
        },
        attention_share=(
            (float(raw["attention_share"][0]), float(raw["attention_share"][1]))
            if raw.get("attention_share")
            else None
        ),
        issuer_outage=raw.get("issuer_outage") or {},
        derives_from=tuple(
            DerivedFigure(
                source_file=str(item.get("source_file", "")),
                figure=str(item.get("figure", "")),
                provenance=str(item.get("provenance", "secondary")),
                used_for=str(item.get("used_for", "")),
            )
            for item in (raw.get("derives_from") or [])
        ),
        notes=str(raw.get("notes", "")),
        interpretation=str(raw.get("interpretation", "")),
    )


def available_profiles(directory: pathlib.Path | str = PROFILE_DIR) -> list[str]:
    return sorted(p.stem for p in pathlib.Path(directory).glob("*.yaml"))


# --------------------------------------------------------------------------- #
# Sidecars
# --------------------------------------------------------------------------- #


def sidecar_path(path: pathlib.Path) -> pathlib.Path:
    return path.with_suffix(path.suffix + ".source.md")


def retrieved_date(path: pathlib.Path) -> str:
    """Filesystem mtime. Recorded as such -- it is when the file arrived here,
    not necessarily when it was published or downloaded."""
    return dt.date.fromtimestamp(path.stat().st_mtime).isoformat()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _format_inventory() -> str:
    lines: list[str] = ["Calibration sources in data/\n"]
    for item in inventory():
        lines.append(f"  {item.name}")
        if not item.machine_read:
            lines.append(f"    [{item.kind}] {item.note}")
            continue
        for sheet in item.sheets:
            lines.append(
                f"    sheet {sheet.name!r}: {sheet.data_rows} data rows, "
                f"columns {list(sheet.columns)}"
            )
    return "\n".join(lines)


def _format_downtime() -> str:
    entries = read_downtime()
    if not entries:
        return "\nNo downtime workbooks found."
    summary = summarise_downtime(entries)
    lines = ["\n\nNPCI downtime workbooks -- mechanical summary\n"]
    lines.append(
        f"  {'month':<8}{'banks':>7}{'incidents':>11}{'hours':>9}"
        f"{'mean %':>9}{'min %':>8}{'max %':>8}"
    )
    for month in summary.months:
        stats = summary.per_month[month]
        lines.append(
            f"  {month:<8}{int(stats['banks']):>7}{int(stats['incidents']):>11}"
            f"{stats['total_hours']:>9.1f}{stats['mean_share_pct']:>9.2f}"
            f"{stats['min_share_pct']:>8.2f}{stats['max_share_pct']:>8.2f}"
        )
    lines.append(
        f"\n  distinct banks: {summary.distinct_banks_raw} as spelled, "
        f"{summary.distinct_banks_normalised} after normalising spelling variants"
    )
    lines.append(f"  appear in exactly one month: {summary.appear_once}")
    lines.append(
        f"  appear in every month: {', '.join(summary.appear_every_month) or 'none'}"
    )
    if summary.merged_spellings:
        lines.append("\n  spelling variants merged (same bank, different spelling):")
        for names in summary.merged_spellings:
            lines.append(f"    {list(names)}")
        lines.append(
            "    Counting these as distinct banks overstates the total and makes a bank"
        )
        lines.append("    that appeared every month look intermittent.")
    lines.append("\n  worst single bank-months, as share of the month:")
    for share, month, bank, hours in summary.worst[:5]:
        lines.append(f"    {share:>5.2f}%  {month:<6} {bank:<40} {hours:>6.2f}h")
    return "\n".join(lines)


def _format_profiles() -> str:
    lines = ["\n\nCalibration profiles\n"]
    for name in available_profiles():
        profile = load_profile(name)
        marker = "" if profile.is_calibrated else "   <- numbers are chosen, not derived"
        lines.append(f"  {profile.name}  [{profile.status}]{marker}")
        lines.append(f"    {profile.provenance_summary()}")
        for figure in profile.derives_from:
            lines.append(
                f"      {figure.provenance:<10} {figure.source_file}: {figure.figure[:70]}"
            )
    return "\n".join(lines)


def main() -> None:
    print(_format_inventory())
    print(_format_downtime())
    print(_format_profiles())
    print(
        "\n\nWhat a four-way split would need that is not in these files:\n"
        "  Razorpay's ~20% figure covers insufficient balance, bank downtime and\n"
        "  cancelled mandates in ONE number -- LIQUIDITY, INFRASTRUCTURE and TERMINAL\n"
        "  combined. ATTENTION is not mentioned at all. The NPCI workbooks give bank\n"
        "  outage hours but no failure counts, so they cannot supply the split either.\n"
        "  Any four-way mix is therefore interpretation, and belongs in a profile's\n"
        "  `interpretation` field rather than being presented as sourced."
    )


if __name__ == "__main__":
    main()
