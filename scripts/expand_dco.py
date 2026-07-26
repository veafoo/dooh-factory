#!/usr/bin/env python3
"""Expand DCO axes into one sheet row per combination.

This is the part that makes it a factory rather than a renderer. You describe
the axes a campaign varies along - market, audience, offer, daypart - and which
rush or param each axis value selects; this writes the cartesian product as rows
ready to paste into the `mode1` or `mode2` tab.

    python scripts/expand_dco.py campaign.yml -o rows.csv
    python scripts/expand_dco.py campaign.yml --dry-run     # just count and preview

Recipe (YAML):

    mode: mode1
    base:                       # applied to every row
      campaign: "Soldes Été"
      advertiser: "Demo Brand"
      channel: dooh
      format: "1080x1920"
      naming_template: "{campaign}_{market}_{audience}_{offer}_{version}"
      version: a
    axes:                       # the cartesian product is taken over these
      market: [FR, BE]
      audience: [young, family]
      offer: ["-30%", "-50%"]
    slots:                      # cells built from the axis values of each row
      clip_1: "intro_{market}.mp4"
      clip_2: "product_{audience}.mp4"
      clip_3: "cta_{offer|slug}.mp4"
    exclude:                    # combinations that must not be produced
      - {market: BE, offer: "-50%"}

`{axis}` interpolates the raw value, `{axis|slug}` a filename-safe one
(`-50%` -> `50pct`). Rows get a `creative_id` built from the axis values unless
`base` sets one.

The combination count is printed before anything is written: axes multiply fast,
and 4 x 3 x 5 x 2 is 120 renders, which is a decision, not a detail.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import naming, sheet_model  # noqa: E402

_FIELD = re.compile(r"\{([a-zA-Z0-9_]+)(\|slug)?\}")


def interpolate(template: str, values: dict) -> str:
    """`{axis}` -> raw value, `{axis|slug}` -> filename-safe value."""
    def repl(m: re.Match) -> str:
        raw = str(values.get(m.group(1), "") or "")
        return naming.sanitize(raw) if m.group(2) else raw

    return _FIELD.sub(repl, template)


def combinations(axes: dict, exclude: list[dict] | None = None) -> list[dict]:
    """Cartesian product of the axes, minus the excluded combinations."""
    if not axes:
        return [{}]
    names = list(axes)
    rows = [dict(zip(names, combo)) for combo in itertools.product(*(axes[n] for n in names))]
    for rule in exclude or []:
        rows = [r for r in rows if not all(str(r.get(k)) == str(v) for k, v in rule.items())]
    return rows


def expand(recipe: dict) -> tuple[list[str], list[list[str]]]:
    """-> (header, rows) for the recipe's mode."""
    mode = recipe.get("mode", "mode1")
    if mode not in ("mode1", "mode2"):
        raise ValueError(f"mode must be mode1 or mode2, got {mode!r}")
    header = sheet_model.TABS[mode]["header"]

    base = recipe.get("base") or {}
    axes = recipe.get("axes") or {}
    slots = recipe.get("slots") or {}
    id_col = "creative_id" if mode == "mode1" else "variant_id"

    unknown = [c for c in list(base) + list(slots) if c not in header]
    if unknown and mode == "mode1":
        # mode2 injects unknown columns into the creative on purpose; mode1 does not.
        raise ValueError(f"columns not in the {mode} model: {unknown}")

    out_rows = []
    seen: dict[str, dict] = {}
    for combo in combinations(axes, recipe.get("exclude")):
        values = {**base, **combo}
        for column, template in slots.items():
            values[column] = interpolate(str(template), values)
        if not values.get(id_col):
            values[id_col] = naming.sanitize("-".join(str(combo[a]) for a in axes)) or "1"
        if values.get("status", "") == "":
            values["status"] = "pending"

        rid = values[id_col]
        if rid in seen:
            raise ValueError(
                f"two combinations produce the same {id_col} '{rid}' - add an axis to "
                f"the id or set a distinct naming_template")
        seen[rid] = values

        extra = [c for c in values if c not in header]
        row_header = header + [c for c in extra if c not in header]
        out_rows.append([str(values.get(c, "")) for c in row_header])
        header = row_header  # mode2 may legitimately add param columns

    return header, [r + [""] * (len(header) - len(r)) for r in out_rows]


def main() -> int:
    ap = argparse.ArgumentParser(description="Expand DCO axes into sheet rows")
    ap.add_argument("recipe", help="YAML recipe")
    ap.add_argument("-o", "--out", help="CSV to write (default: stdout)")
    ap.add_argument("--dry-run", action="store_true", help="count and preview only")
    ap.add_argument("--max", type=int, default=2000,
                    help="refuse to expand beyond this many rows (default 2000)")
    args = ap.parse_args()

    import yaml

    with open(args.recipe) as f:
        recipe = yaml.safe_load(f) or {}

    axes = recipe.get("axes") or {}
    total = 1
    for values in axes.values():
        total *= len(values)
    print(f"axes: {', '.join(f'{k}({len(v)})' for k, v in axes.items()) or 'none'}")

    header, rows = expand(recipe)
    print(f"combinations: {total} -> {len(rows)} rows after exclusions")
    if len(rows) > args.max:
        raise SystemExit(f"{len(rows)} rows exceeds --max {args.max}; that is "
                         f"{len(rows)} renders - raise the limit deliberately")

    if args.dry_run:
        idx = header.index("creative_id" if recipe.get("mode", "mode1") == "mode1"
                           else "variant_id")
        for row in rows[:10]:
            filled = {h: v for h, v in zip(header, row) if v}
            print(f"  {row[idx]}: {filled}")
        if len(rows) > 10:
            print(f"  ... and {len(rows) - 10} more")
        return 0

    out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    try:
        w = csv.writer(out)
        w.writerow(header)
        w.writerows(rows)
    finally:
        if args.out:
            out.close()
            print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
