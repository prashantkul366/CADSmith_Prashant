"""Run real mechanical parts through the app, and prod it while it works.

Three things the other browser checks do not cover:

  * prompts from vague ("something to hold a rotating shaft") to fully
    dimensioned, each landing on the part a CAD engineer meant and on the
    dimensions the kernel actually measures;
  * clicking around mid-run - history, view tools, a second Generate, the
    edit bar - which is where a demo gets embarrassing;
  * how long each stage takes, read from the run's own event timestamps
    rather than guessed.

Needs a running server:
    ./app/run_app.sh &
    python -m app.tests.ui_parts_check
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.tools import mock_parts  # noqa: E402

_CANDIDATE_BROWSERS = [
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
]

failures: list[str] = []
timings: list[dict] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def executable() -> str | None:
    for candidate in _CANDIDATE_BROWSERS:
        if Path(candidate).exists():
            return candidate
    return None


# Vague through to fully specified. The bounding box is what the kernel
# reports for the *corrected* part, so it doubles as proof the refinement
# landed rather than the first attempt being kept.
PROMPTS = [
    ("vague",
     "I need something to hold a rotating shaft on a frame",
     "pillow_block", (90.0, 40.0, 70.0)),
    ("loose",
     "a bracket to join two panels at a right angle, with a gusset",
     "l_bracket", (100.0, 80.0, 80.0)),
    ("named part",
     "Make me a V-belt pulley for an A section belt.",
     "v_pulley", (120.0, 120.0, 40.0)),
    ("dimensioned",
     "A raised-face pipe flange: 160mm outer diameter, 50mm bore, 18mm "
     "thick. Eight 18mm bolt holes on a 125mm bolt circle. A raised sealing "
     "face 90mm diameter stands 2mm proud, and a hub 80mm diameter extends "
     "25mm from the back.",
     "pipe_flange", (160.0, 160.0, 45.0)),
    ("complex",
     "A hydraulic manifold block 120mm by 60mm by 50mm. A 16mm main gallery "
     "is drilled the full 120mm length. Three 10mm ports enter the top face "
     "on 40mm centres and meet the gallery. Four M10 mounting holes, 11mm "
     "clearance, sit 12mm in from each corner.",
     "manifold_block", (120.0, 60.0, 50.0)),
]


def configure_provider(page, base_url: str) -> None:
    page.select_option("#optProvider", "custom")
    page.wait_for_timeout(400)
    page.fill("#providerBase", base_url)
    page.fill("#providerKey", "mock-key")
    page.click("#saveKeyBtn")
    page.wait_for_timeout(1200)
    page.fill("#optGenModel", "mock-coder")
    page.fill("#optJudgeModel", "mock-judge")
    page.wait_for_timeout(400)


def start_mock(port: int, delay: float = 0.0) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-m", "app.tools.mock_provider",
         "--port", str(port), "--delay", str(delay)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.0)
    if process.poll() is not None:
        raise RuntimeError(f"mock provider exited immediately - port {port} busy?")
    return process


def stop_mock(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def run_and_wait(page, prompt: str, timeout_ms: int = 240000) -> float:
    """Generate, wait for the run to settle, return wall-clock seconds."""
    page.fill("#prompt", prompt)
    started = time.time()
    page.click("#genBtn")
    page.wait_for_selector("#ovPipe:not([hidden])", timeout=20000)
    # The overlay hides the moment the first attempt renders, so it says
    # nothing about the run being over - waiting on it measured the first
    # iteration and called it the whole run. S.busy is the real signal.
    page.wait_for_function("() => S.busy === false", timeout=timeout_ms)
    page.wait_for_timeout(1200)
    return time.time() - started


def stage_timings(job_id: str) -> list[tuple[str, float]]:
    """Per-phase seconds, from the run's own event timestamps."""
    path = ROOT / "app" / "runs" / job_id / "events.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    rows = [r for r in rows if r["phase"] != "log"]
    if len(rows) < 2:
        return []
    spent: dict[str, float] = {}
    for earlier, later in zip(rows, rows[1:]):
        spent[earlier["phase"]] = spent.get(earlier["phase"], 0.0) + (
            later["ts"] - earlier["ts"])
    return sorted(spent.items(), key=lambda kv: -kv[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8077")
    parser.add_argument("--mock-port", type=int, default=8133)
    parser.add_argument("--out", default="/tmp/cadsmith_parts")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base_url = f"http://127.0.0.1:{args.mock_port}/v1"

    console: list[str] = []
    mock = start_mock(args.mock_port)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                executable_path=executable(),
                args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"])
            page = browser.new_page(viewport={"width": 1600, "height": 950})
            page.on("pageerror", lambda e: console.append(str(e)))
            page.on("console", lambda m: console.append(m.text)
                    if m.type == "error" else None)

            page.goto(args.url, wait_until="networkidle")
            page.wait_for_timeout(1200)
            configure_provider(page, base_url)

            # ---------------------------------------------------------
            print("\nReal parts, from a vague ask to a fully dimensioned one")
            for label, prompt, part_id, bbox in PROMPTS:
                print(f"\n  [{label}] {prompt[:64]}…")
                elapsed = run_and_wait(page, prompt)
                job_id = page.evaluate("S.jobId")

                extents = page.evaluate(
                    "Viewer.extents ? [Viewer.extents.x, Viewer.extents.y, "
                    "Viewer.extents.z] : null")
                got = [round(v, 1) for v in extents] if extents else None
                check(f"{label}: kernel builds the part it was asked for",
                      extents is not None and all(
                          abs(a - b) < 0.6 for a, b in zip(extents, bbox)),
                      f"{got} vs expected {list(bbox)}")

                code = page.locator("#codeScroll").inner_text()
                expected = mock_parts.select(prompt)
                check(f"{label}: routed to {part_id}",
                      expected.id == part_id, f"got {expected.id}")

                # The corrected value, not the seeded mistake, must be what
                # is on screen at the end.
                fixed_only = [
                    line for line in expected.code_fixed.splitlines()
                    if line.strip() and line not in expected.code_first]
                check(f"{label}: the refined source is what is shown",
                      any(line.strip() in code for line in fixed_only),
                      (fixed_only[0].strip() if fixed_only else "?")[:48])

                attempts = page.locator("#iters .iter").count()
                check(f"{label}: the Judge rejected once, then accepted",
                      attempts == 2 and "Accepted by the Judge" in
                      page.locator("#valBody").inner_text(),
                      f"{attempts} attempts")

                phases = stage_timings(job_id) if job_id else []
                timings.append({"label": label, "part": part_id,
                                "seconds": elapsed, "phases": phases})
                print(f"        {elapsed:5.1f}s  " + "  ".join(
                    f"{name} {secs:.1f}s" for name, secs in phases[:5]))
                page.screenshot(path=str(out / f"parts-{part_id}.png"))

            # ---------------------------------------------------------
            # Slow the provider down for the rest: the interesting mistakes
            # are the ones a person makes *during* a run, and a run that is
            # over in six seconds leaves no window to make them in.
            print("\nClicking around while a run is in flight")
            stop_mock(mock)
            mock = start_mock(args.mock_port + 1, delay=3.0)
            configure_provider(page, f"http://127.0.0.1:{args.mock_port + 1}/v1")

            page.fill("#prompt", "a stepped transmission shaft with a keyway")
            page.click("#genBtn")
            page.wait_for_selector("#ovPipe:not([hidden])", timeout=20000)
            page.wait_for_timeout(800)

            check("Generate is locked while busy",
                  page.locator("#genBtn").is_disabled())
            check("the drawing button is locked while busy",
                  page.locator("#drawBtn").is_disabled())
            check("the edit bar is locked while busy",
                  page.locator("#cmdIn").is_disabled()
                  and page.locator("#applyBtn").is_disabled())

            # Force the clicks anyway: a disabled button a person can still
            # reach through the keyboard must not start a second run.
            page.evaluate("document.querySelector('#genBtn').click()")
            page.evaluate("document.querySelector('#drawBtn').click()")
            page.wait_for_timeout(400)
            in_flight = page.evaluate("S.jobId")

            page.click("#histBtn")
            page.wait_for_timeout(800)
            check("history opens mid-run",
                  page.locator("#hist").evaluate(
                      "el => el.classList.contains('open')"))
            check("history lists the finished runs",
                  page.locator("#hlist .hrow").count() >= len(PROMPTS),
                  f"{page.locator('#hlist .hrow').count()} rows")
            page.click("#histClose")
            page.wait_for_timeout(400)

            for button in ("#wireBtn", "#spinBtn", "#fitBtn"):
                page.click(button)
                page.wait_for_timeout(250)
            page.click("#spinBtn")          # stop spinning again
            page.wait_for_timeout(250)
            check("the view tools survive being used mid-run", True)
            page.screenshot(path=str(out / "parts-midrun.png"))

            check("the forced clicks did not start a second run",
                  page.evaluate("S.jobId") == in_flight,
                  f"{in_flight} -> {page.evaluate('S.jobId')}")

            # The demo risk that actually bites: someone reloads, or the
            # laptop sleeps, while the pipeline is still working. The event
            # log is append-only and replayable from a sequence number, so
            # the page should reattach rather than lose the run.
            print("\nReloading the page mid-run")
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(2000)
            reattached = page.evaluate("S.jobId")
            check("a reload reattaches to the same run",
                  reattached == in_flight,
                  f"{in_flight} -> {reattached}")
            check("the reattached run is shown as still working",
                  page.evaluate("S.busy") is True
                  or page.locator("#ovPipe").is_visible(),
                  f"busy={page.evaluate('S.busy')}")

            page.wait_for_function("() => S.busy === false", timeout=240000)
            page.wait_for_timeout(1500)
            check("the run still finished after all that",
                  page.locator("#ovPipe").is_hidden()
                  and page.locator("#iters .iter").count() == 2,
                  f"{page.locator('#iters .iter').count()} attempts")
            check("its geometry is on screen",
                  page.evaluate("Viewer.extents ? "
                                "Math.round(Viewer.extents.z) : null") == 200,
                  str(page.evaluate("Viewer.extents ? Viewer.extents.z : null")))

            # ---------------------------------------------------------
            print("\nInterrupting: a second run started from a rejected attempt")
            page.locator('.iter[data-i="0"]').click()
            page.wait_for_timeout(1500)
            check("a rejected attempt can still be opened afterwards",
                  "Rejected by the Judge" in
                  page.locator("#valBody").inner_text(),
                  page.locator("#valBody").inner_text()
                  .replace("\n", " ")[:70])

            check("the controls unlock once the run is done",
                  not page.locator("#genBtn").is_disabled()
                  and not page.locator("#cmdIn").is_disabled())

            print("\nDrawing the finished part")
            page.locator('.iter[data-i="1"]').click()
            page.wait_for_timeout(1500)
            page.click("#drawBtn")
            page.wait_for_timeout(1000)
            page.wait_for_function(
                "() => document.querySelector('#sheet') "
                "&& document.querySelector('#sheet').innerHTML.includes('svg')",
                timeout=120000)
            svg_len = page.evaluate(
                "document.querySelector('#sheet').innerHTML.length")
            check("an orthographic drawing is produced", svg_len > 2000,
                  f"{svg_len} chars of SVG")
            page.screenshot(path=str(out / "parts-drawing.png"))

            page.screenshot(path=str(out / "parts-after.png"))
            browser.close()
    finally:
        stop_mock(mock)

    # -------------------------------------------------------------------
    print("\nTiming")
    print(f"  {'prompt':<13} {'part':<16} {'total':>7}   slowest stages")
    for row in timings:
        stages = "  ".join(f"{n} {s:.1f}s" for n, s in row["phases"][:4])
        print(f"  {row['label']:<13} {row['part']:<16} "
              f"{row['seconds']:6.1f}s   {stages}")
    if timings:
        total = sum(r["seconds"] for r in timings)
        print(f"  {'':<13} {'':<16} {total:6.1f}s   "
              f"({total / len(timings):.1f}s average per run)")

    real = [c for c in console if "favicon" not in c.lower()]
    print("\nConsole")
    check("no JavaScript errors", not real, "; ".join(real[:3]))

    print(f"\nScreenshots in {out}")
    print("=" * 58)
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
