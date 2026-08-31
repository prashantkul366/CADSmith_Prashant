"""Cross-check our dimension tables against BOLTS.

``app/catalog/standards.py`` was transcribed by hand, and it says so: the
numbers are load-bearing for real engineering and a typo in them would
propagate silently through the plan, the code and the Judge together. This
compares every value against BOLTS, an independent open library of technical
specifications, so the tables are checked rather than trusted.

    git clone https://github.com/boltsparts/boltsparts.git /tmp/bolts
    .venv/bin/python -m app.tools.check_standards --bolts /tmp/bolts

**Nothing is copied.** BOLTS' data files are LGPL-2.1+ and its tooling is
GPL-3.0; this repository has neither in it and does not depend on either.
The tool reads a checkout you supply and reports agreement or disagreement.
A dimension is a fact and cannot be copyrighted; a compilation of them can
be, which is why this verifies rather than vendors.

Where the two disagree, neither is automatically right - BOLTS cites its own
sources per table and is itself a transcription. A disagreement means go and
read the standard.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.catalog import standards  # noqa: E402

TOLERANCE = 0.001


def _load(path: Path, filename: str, class_id: str):
    import yaml

    document = yaml.safe_load((path / "data" / filename).read_text())
    for entry in document["classes"]:
        if entry["id"] != class_id:
            continue
        tables = entry["parameters"].get("tables")
        if isinstance(tables, list):
            tables = tables[0]
        return tables
    raise KeyError(f"{filename}: no class '{class_id}'")


def _column(table, name: str) -> dict:
    """One column of a BOLTS table, keyed by its row index."""
    index = table["columns"].index(name)
    out = {}
    for key, row in table["data"].items():
        value = row[index]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[str(key)] = float(value)
    return out


#: Our table -> where the same number lives in BOLTS.
#: (label, our dict, attribute, blt file, class id, column)
CHECKS = [
    ("ISO 4762 cap screw head diameter", standards.ISO_4762, "head_diameter",
     "hex_socket.blt", "hexsocketheadcap", "d2"),
    ("ISO 4762 cap screw head height", standards.ISO_4762, "head_height",
     "hex_socket.blt", "hexsocketheadcap", "k"),
    ("ISO 4762 cap screw socket size", standards.ISO_4762,
     "socket_across_flats", "hex_socket.blt", "hexsocketheadcap", "s"),
    ("ISO 4014 hex bolt across flats", standards.ISO_4014, "across_flats",
     "hex.blt", "hexbolt2", "s"),
    ("ISO 4014 hex bolt head height", standards.ISO_4014, "height",
     "hex.blt", "hexbolt2", "k"),
    ("ISO 4032 hex nut across flats", standards.ISO_4032, "across_flats",
     "nut.blt", "hexagonnut1", "s"),
    ("ISO 4032 hex nut thickness", standards.ISO_4032, "height",
     "nut.blt", "hexagonnut1", "m_max"),
    ("ISO 7089 washer bore", standards.ISO_7089, "inner_diameter",
     "washer.blt", "plainwasher1", "d1"),
    ("ISO 7089 washer outside diameter", standards.ISO_7089, "outer_diameter",
     "washer.blt", "plainwasher1", "d2"),
    ("ISO 7089 washer thickness", standards.ISO_7089, "thickness",
     "washer.blt", "plainwasher1", "s"),
    ("coarse thread pitch", standards.THREADS, "pitch",
     "hex.blt", "hexscrew1", "pitch"),
    ("ISO 15 bearing bore", standards.BEARINGS, "bore",
     "bearings.blt", "singlerowradialbearing", "d1"),
    ("ISO 15 bearing outside diameter", standards.BEARINGS, "outer_diameter",
     "bearings.blt", "singlerowradialbearing", "d2"),
    ("ISO 15 bearing width", standards.BEARINGS, "width",
     "bearings.blt", "singlerowradialbearing", "B"),
]


#: Disagreements that have been looked into, with the reasoning. Recorded
#: rather than silently skipped: a difference is worth seeing every run, but
#: a settled one should not fail the check forever.
KNOWN = {
    ("ISO 7089 washer bore", "M10"): (
        "ours 10.5 stands. The ISO 7089 bore tracks the ISO 273 close-fit "
        "series exactly at every other size (3.2, 4.3, 5.3, 6.4, 8.4, 13, "
        "17, 21, 25); BOLTS' 10 is the only value that breaks the pattern, "
        "and 10.5 is the close-fit clearance for M10."),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bolts", required=True,
                        help="Path to a boltsparts/boltsparts checkout.")
    args = parser.parse_args()

    root = Path(args.bolts)
    if not (root / "data").is_dir():
        print(f"No BOLTS data at {root}/data. Clone it first:")
        print("  git clone https://github.com/boltsparts/boltsparts.git /tmp/bolts")
        return 2

    total_checked = total_missing = 0
    disagreements: list[str] = []
    settled: list[str] = []

    for label, ours, attribute, filename, class_id, column in CHECKS:
        try:
            reference = _column(_load(root, filename, class_id), column)
        except Exception as error:
            print(f"  SKIP  {label} - {type(error).__name__}: {error}")
            continue

        checked = missing = 0
        bad: list[str] = []
        for key, spec in ours.items():
            mine = float(getattr(spec, attribute))
            theirs = reference.get(key)
            if theirs is None:
                missing += 1
                continue
            checked += 1
            if abs(mine - theirs) > TOLERANCE:
                note = KNOWN.get((label, key))
                if note:
                    settled.append(
                        f"{label} {key}: ours {mine:g}, BOLTS {theirs:g} - {note}")
                else:
                    bad.append(f"{key}: ours {mine:g}, BOLTS {theirs:g}")

        total_checked += checked
        total_missing += missing
        mark = "OK  " if not bad else "DIFF"
        detail = f"{checked} agree"
        if missing:
            detail += f", {missing} not in BOLTS"
        print(f"  {mark}  {label:<38} {detail}")
        for line in bad:
            print(f"          {line}")
            disagreements.append(f"{label} - {line}")

    print("\n" + "=" * 62)
    print(f"{total_checked} values compared against BOLTS, "
          f"{total_missing} had no counterpart there.")
    for line in settled:
        print(f"\n  Investigated: {line}")
    if disagreements:
        print(f"{len(disagreements)} DISAGREEMENT(S) - go and read the "
              f"standard; neither source is automatically right:")
        for line in disagreements:
            print(f"   - {line}")
        return 1
    print("Every value with a counterpart in BOLTS agrees.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
