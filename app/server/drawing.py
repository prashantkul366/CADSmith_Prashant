"""Engineering drawing sheet built from real orthographic projections.

The mock produced its views by re-drawing the four hardcoded parts from their
parameters, so the sheet could only ever depict shapes the frontend already
knew.  Here the projections come from the exported STEP solid via CadQuery's
SVG exporter, which projects the real B-rep and resolves hidden lines - so any
part the pipeline can build gets a correct drawing.

Two things the exporter does not do are handled here.  It fits each view to
its own frame independently, which is wrong for a drawing: views must share
one scale to be comparable, so each is rescaled to the smallest of the four.
And it can emit geometry outside the frame it was given, so every view is
clipped to its own viewport.

Projection work happens in a subprocess, matching how ``autofab.executor``
isolates kernel work: an OCCT failure takes down the worker, not the server.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

# Sheet geometry, in SVG user units.
SHEET_W, SHEET_H = 1120, 780
VIEW_W, VIEW_H = 510, 270
PAD_X, PAD_Y = 10, 24          # inset of the drawing area within a frame
INNER_W = VIEW_W - 2 * PAD_X
INNER_H = VIEW_H - PAD_Y - 12

QUADRANTS = [
    ("FRONT VIEW", (40, 40)),
    ("TOP VIEW", (570, 40)),
    ("RIGHT VIEW", (40, 330)),
    ("ISOMETRIC", (570, 330)),
]

TITLE_X, TITLE_Y, TITLE_W, TITLE_H = 570, 616, 510, 136

# Looking directions, matching the labels above.  Z is up, as in CadQuery.
PROJECTIONS = {
    "FRONT VIEW": (0, -1, 0),
    "TOP VIEW": (0, 0, 1),
    "RIGHT VIEW": (1, 0, 0),
    "ISOMETRIC": (1, 1, 1),
}

_SVG_BODY = re.compile(r"<svg\b[^>]*>(.*)</svg>", re.DOTALL | re.IGNORECASE)
_SCALE = re.compile(r"scale\(\s*(-?[\d.eE+]+)\s*,")

_PROJECT_SCRIPT = '''
import json, sys
import cadquery as cq

step_path, out_dir, spec_json = sys.argv[1], sys.argv[2], sys.argv[3]
spec = json.loads(spec_json)

shape = cq.importers.importStep(step_path)
written = {}
for name, direction in spec.items():
    out = f"{out_dir}/{name}.svg"
    cq.exporters.export(shape, out, opt={
        "width": %(w)d, "height": %(h)d,
        "marginLeft": 18, "marginTop": 18,
        "showAxes": False,
        "projectionDir": tuple(direction),
        "strokeWidth": 0.45,
        "strokeColor": (0, 0, 0),
        "hiddenColor": (150, 150, 150),
        "showHidden": True,
    })
    written[name] = out

print("__DRAWING__")
print(json.dumps(written))
''' % {"w": INNER_W, "h": INNER_H}


def _project(step_path: Path, timeout: int = 120) -> dict[str, str]:
    """Export one SVG per view. Returns {view name: svg body}."""
    work = Path(tempfile.mkdtemp(prefix="cadsmith_drawing_"))
    script = work / "project.py"
    script.write_text(_PROJECT_SCRIPT)

    spec = {name: list(direction) for name, direction in PROJECTIONS.items()}
    result = subprocess.run(
        [sys.executable, str(script), str(step_path), str(work), json.dumps(spec)],
        capture_output=True, text=True, timeout=timeout,
    )
    if "__DRAWING__" not in result.stdout:
        raise RuntimeError(
            (result.stderr or result.stdout or "projection produced no output")[-800:])

    written = json.loads(result.stdout.split("__DRAWING__")[1].strip())
    bodies: dict[str, str] = {}
    for name, path in written.items():
        match = _SVG_BODY.search(Path(path).read_text())
        if match:
            bodies[name] = match.group(1)
    return bodies


def _view_scale(body: str) -> Optional[float]:
    """Recover the units-per-mm the exporter chose for this view."""
    match = _SCALE.search(body)
    if not match:
        return None
    try:
        value = abs(float(match.group(1)))
    except ValueError:
        return None
    return value or None


def _common_scale_factors(bodies: dict[str, str]) -> dict[str, float]:
    """Per-view factors that bring every view onto one shared scale.

    Each view arrives fitted to its own frame, so a small part and a large one
    would be drawn the same size.  Shrinking every view to the smallest scale
    present makes them directly comparable, and guarantees each still fits the
    frame it was fitted to (the factor is never greater than 1).
    """
    scales = {name: _view_scale(body) for name, body in bodies.items()}
    known = [s for s in scales.values() if s]
    if not known:
        return {name: 1.0 for name in bodies}
    target = min(known)
    return {
        name: (target / scale if scale else 1.0)
        for name, scale in scales.items()
    }


def _title_block(prompt: str, geometry: dict, job_id: str, version: int) -> str:
    bbox = geometry.get("bounding_box", {})
    dims = "{:.1f} x {:.1f} x {:.1f} mm".format(
        bbox.get("xlen", 0), bbox.get("ylen", 0), bbox.get("zlen", 0))
    title = " ".join(prompt.split()).rstrip(".")
    if len(title) > 54:
        title = title[:54].rsplit(" ", 1)[0] + "…"

    rows = [
        ("TITLE", title),
        ("OVERALL", dims),
        ("VOLUME", f"{geometry.get('volume', 0):.0f} mm3"),
        ("FACES / EDGES", f"{geometry.get('num_faces', '—')} / "
                          f"{geometry.get('num_edges', '—')}"),
        ("SOLID", "WATERTIGHT" if geometry.get("is_valid") else "INVALID"),
    ]

    lines = [
        f'<rect x="{TITLE_X}" y="{TITLE_Y}" width="{TITLE_W}" height="{TITLE_H}" '
        f'fill="#fff" stroke="#000" stroke-width="1.1"/>'
    ]
    row_h = TITLE_H / len(rows)
    for i, (label, value) in enumerate(rows):
        ry = TITLE_Y + i * row_h
        if i:
            lines.append(
                f'<line x1="{TITLE_X}" y1="{ry:.1f}" x2="{TITLE_X + TITLE_W}" '
                f'y2="{ry:.1f}" stroke="#000" stroke-width="0.6"/>')
        lines.append(
            f'<text x="{TITLE_X + 9}" y="{ry + row_h / 2 + 4:.1f}" '
            f'font-family="monospace" font-size="9.5" fill="#555" '
            f'letter-spacing="0.8">{html.escape(label)}</text>')
        lines.append(
            f'<text x="{TITLE_X + 128}" y="{ry + row_h / 2 + 4:.1f}" '
            f'font-family="monospace" font-size="11" fill="#000">'
            f'{html.escape(value)}</text>')
    lines.append(
        f'<line x1="{TITLE_X + 120}" y1="{TITLE_Y}" x2="{TITLE_X + 120}" '
        f'y2="{TITLE_Y + TITLE_H}" stroke="#000" stroke-width="0.6"/>')

    # Footer sits to the left of the title block, clear of every frame.
    footer = [
        "CADSmith · projected from the exported STEP solid",
        f"{job_id} · version {version} · {time.strftime('%Y-%m-%d')}",
        "All views to a common scale · hidden lines shown dashed",
    ]
    for i, text in enumerate(footer):
        lines.append(
            f'<text x="40" y="{TITLE_Y + 22 + i * 16}" font-family="monospace" '
            f'font-size="9.5" fill="#777">{html.escape(text)}</text>')

    return "\n".join(lines)


def build_sheet(
    step_path: Path,
    geometry: dict,
    prompt: str,
    job_id: str,
    version: int,
) -> str:
    """Compose a full drawing sheet as a standalone SVG document."""
    bodies = _project(step_path)
    factors = _common_scale_factors(bodies)

    parts: list[str] = []
    for label, (x, y) in QUADRANTS:
        parts.append(
            f'<rect x="{x}" y="{y}" width="{VIEW_W}" height="{VIEW_H}" '
            f'fill="none" stroke="#CCC" stroke-width="0.6"/>')
        parts.append(
            f'<text x="{x + 9}" y="{y + 17}" font-family="monospace" font-size="10" '
            f'letter-spacing="1" fill="#555">{html.escape(label)}</text>')

        body = bodies.get(label)
        if not body:
            parts.append(
                f'<text x="{x + VIEW_W / 2}" y="{y + VIEW_H / 2}" '
                f'font-family="monospace" font-size="11" fill="#999" '
                f'text-anchor="middle">view unavailable</text>')
            continue

        # Rescale about the centre of the drawing area so the view keeps its
        # position while adopting the shared scale.
        k = factors.get(label, 1.0)
        cx, cy = INNER_W / 2, INNER_H / 2
        transform = (f'translate({cx * (1 - k):.3f},{cy * (1 - k):.3f}) '
                     f'scale({k:.5f})')

        # overflow="hidden" is the SVG default for a nested viewport, but the
        # exporter can place geometry outside the frame it was given, so it is
        # stated explicitly rather than relied upon.
        parts.append(
            f'<svg x="{x + PAD_X}" y="{y + PAD_Y}" width="{INNER_W}" '
            f'height="{INNER_H}" viewBox="0 0 {INNER_W} {INNER_H}" '
            f'overflow="hidden"><g transform="{transform}">{body}</g></svg>')

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SHEET_W}" '
        f'height="{SHEET_H}" viewBox="0 0 {SHEET_W} {SHEET_H}">'
        f'<rect width="{SHEET_W}" height="{SHEET_H}" fill="#fff"/>'
        f'<rect x="14" y="14" width="{SHEET_W - 28}" height="{SHEET_H - 28}" '
        f'fill="none" stroke="#000" stroke-width="1.6"/>'
        f'<rect x="22" y="22" width="{SHEET_W - 44}" height="{SHEET_H - 44}" '
        f'fill="none" stroke="#000" stroke-width="0.7"/>'
        + "\n".join(parts)
        + _title_block(prompt, geometry, job_id, version)
        + "</svg>"
    )


def ensure_sheet(
    version_dir: Path,
    prompt: str,
    job_id: str,
    version: int,
) -> Optional[Path]:
    """Return the sheet for a version, building and caching it on first use."""
    target = version_dir / "drawing.svg"
    if target.exists() and target.stat().st_size > 0:
        return target

    step = version_dir / "model.step"
    geometry_file = version_dir / "geometry.json"
    if not step.exists():
        return None

    geometry = {}
    if geometry_file.exists():
        try:
            geometry = json.loads(geometry_file.read_text())
        except json.JSONDecodeError:
            pass

    sheet = build_sheet(step, geometry, prompt, job_id, version)
    target.write_text(sheet)
    return target
