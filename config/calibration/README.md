# Calibration profiles

A profile is the seam between published figures and simulator parameters. It names a
failure-class mix and records which figures it derives from, so `worlds.yaml` carries a
profile name rather than inline constants.

Select one in `config/worlds.yaml`:

```yaml
batch:
  calibration_profile: uncalibrated
```

## Fields

| Field | Meaning |
|---|---|
| `name` | Profile identifier; matches the filename |
| `status` | `UNCALIBRATED` means the numbers are chosen, not derived. Anything else asserts they are derived |
| `description` | What this profile represents |
| `class_mix` | `[low, high]` range per failure class. Sampled and normalised per world |
| `derives_from` | One entry per published figure the mix leans on |
| `interpretation` | **Required for a calibrated profile.** What had to be assumed to get from the published figures to four classes |
| `notes` | Anything else worth recording |

### `derives_from` entries

```yaml
derives_from:
  - source_file: razorpay-revenue-protect.pdf
    figure: "Around 20% of subsequent debits fail due to insufficient balance,
             bank downtime, or cancelled mandates"
    provenance: secondary     # primary = the publisher measured it; secondary = citing others
    used_for: "the combined LIQUIDITY + INFRASTRUCTURE + TERMINAL weight"
```

`provenance` is per figure, not per file. Razorpay is primary for claims about its own
stack and secondary where it cites unattributed industry data, and the same PDF contains
both.

## Why `interpretation` is a required field on a calibrated profile

Published data does not split along INFRASTRUCTURE / LIQUIDITY / ATTENTION / TERMINAL.
Razorpay's ~20% figure covers three of the four in one number and never mentions the
fourth. Any four-way split therefore involves a judgement that is not in the source.

The field exists so that judgement is written down next to the numbers it produced. A
profile that cites sources without stating what it inferred from them is the failure mode
this whole mechanism is meant to prevent: **an interpreted number presented as sourced is
worse than an honest invention**, because an invention is labelled.
