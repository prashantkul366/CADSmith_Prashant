"""Editing the same part over and over, in a real browser.

The way a CAD person actually works: make a part, then keep changing it -
"40 teeth", "now module 3", "wider face", "bigger bore" - each on top of the
last, watching the model update each time.

What this covers that the HTTP test does not: that the edit bar stays usable
across a long chain, that every step lands in the timeline and can be
clicked back to, that editing while looking at an *older* version branches
from the one on screen rather than the newest, and - the thing most likely
to strand someone - that a refused edit explains itself on screen instead of
failing quietly.

Needs a running server, but no API key: every edit here is a parameter patch.

    ./app/run_app.sh &
    .venv/bin/python -m app.tests.ui_edit_check
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_CANDIDATE_BROWSERS = [
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
]

failures: list[str] = []
timings: list[tuple[str, float]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def executable() -> str | None:
    for candidate in _CANDIDATE_BROWSERS:
        if Path(candidate).exists():
            return candidate
    return None


def generate(page, prompt: str) -> None:
    page.fill("#prompt", prompt)
    page.click("#genBtn")
    page.wait_for_function("() => S.busy === false", timeout=180000)
    page.wait_for_timeout(800)


def apply_edit(page, instruction: str, expect_new_version: bool = True
               ) -> tuple[bool, float]:
    """Type a change and apply it. Returns (a version was added, seconds)."""
    before = page.evaluate("S.versions.length")
    page.fill("#cmdIn", instruction)
    started = time.time()
    page.click("#applyBtn")
    if expect_new_version:
        try:
            page.wait_for_function(
                f"() => S.busy === false && S.versions.length > {before}",
                timeout=180000)
        except Exception:
            return False, time.time() - started
    else:
        page.wait_for_function("() => S.busy === false", timeout=180000)
    page.wait_for_timeout(700)
    added = page.evaluate("S.versions.length") > before
    return added, time.time() - started


CHAIN = [
    ("make it 40 teeth", 84.0),          # module 2 -> 2*40 + 2*2
    ("set the module to 3", 126.0),      # 3*40 + 2*3
    ("make the face width 20mm", 126.0),
    ("set the bore diameter to 12mm", 126.0),
    ("make it 24 teeth", 78.0),          # 3*24 + 2*3
    ("set the module to 2", 52.0),       # 2*24 + 2*2
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8077")
    parser.add_argument("--out", default="/tmp/cadsmith_edit")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    console: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=executable(),
            args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_page(viewport={"width": 1600, "height": 950})
        page.on("pageerror", lambda e: console.append(str(e)))
        page.on("console", lambda m: console.append(m.text)
                if m.type == "error" else None)
        page.goto(args.url, wait_until="networkidle")
        page.wait_for_timeout(1500)

        print("\nA part to work on")
        generate(page, "a 20 tooth spur gear, module 2")
        check("the gear is on screen",
              page.evaluate("S.versions.length") == 1
              and abs(page.evaluate("Viewer.extents.x") - 44.0) < 0.3,
              f"{page.evaluate('Viewer.extents.x'):.2f} mm across")
        check("the edit bar is ready", not page.locator("#cmdIn").is_disabled())

        # -----------------------------------------------------------------
        print("\nSix changes in a row, each on top of the last")
        for step, (instruction, expected_dia) in enumerate(CHAIN, start=1):
            added, seconds = apply_edit(page, instruction)
            timings.append((instruction, seconds))
            across = page.evaluate("Viewer.extents ? Viewer.extents.x : null")
            check(f"{step}. '{instruction}'",
                  added and across is not None
                  and abs(across - expected_dia) < 0.4,
                  f"{across:.2f} mm across, want {expected_dia:g} "
                  f"· {seconds:.1f}s" if across else "no geometry")
            check(f"   the box clears for the next change",
                  page.locator("#cmdIn").input_value() == "",
                  repr(page.locator("#cmdIn").input_value()))

        check("every step is a version in the timeline",
              page.locator("#iters .iter").count() == len(CHAIN) + 1,
              f"{page.locator('#iters .iter').count()} cards")
        check("they are labelled as edits",
              page.locator("#iters").inner_text().count("EDIT") == len(CHAIN),
              page.locator("#iters").inner_text().replace("\n", " ")[:60])
        page.screenshot(path=str(out / "edit-chain.png"))

        # -----------------------------------------------------------------
        print("\nStepping back through the history")
        page.locator('.iter[data-i="0"]').click()
        page.wait_for_timeout(2000)
        check("the original is still there and rebuilds",
              abs(page.evaluate("Viewer.extents.x") - 44.0) < 0.3,
              f"{page.evaluate('Viewer.extents.x'):.2f} mm across")
        code = page.locator("#codeScroll").inner_text()
        check("its source is the original, not the edited one",
              "teeth_number = 20" in code.replace("  ", " "),
              [l.strip() for l in code.splitlines() if "teeth_number" in l][:1][0]
              if any("teeth_number" in l for l in code.splitlines()) else "?")

        print("\nEditing the version on screen, not the newest")
        added, _ = apply_edit(page, "make the face width 30mm")
        code = page.locator("#codeScroll").inner_text().replace("  ", " ")
        check("it branched from the original",
              added and "teeth_number = 20" in code
              and "face_width = 30.0" in code,
              "; ".join(l.strip() for l in code.splitlines()
                        if "teeth_number" in l or "face_width" in l))
        check("the newer versions were not discarded",
              page.locator("#iters .iter").count() == len(CHAIN) + 2,
              f"{page.locator('#iters .iter').count()} cards")
        page.screenshot(path=str(out / "edit-branched.png"))

        # -----------------------------------------------------------------
        print("\nRefusals explain themselves on screen")
        for instruction, expect in [
            ("make it nicer", "value"),
            ("make the flange diameter 90mm", "flange"),
            ("set the diameter to 50mm", "module"),
        ]:
            before = page.evaluate("S.versions.length")
            page.fill("#cmdIn", instruction)
            page.click("#applyBtn")
            page.wait_for_function("() => S.busy === false", timeout=120000)
            page.wait_for_timeout(1200)
            toast = page.locator("#toast").inner_text()
            same = page.evaluate("S.versions.length") == before
            check(f"'{instruction}' is refused with a reason",
                  same and expect.lower() in toast.lower(),
                  toast.replace("\n", " ")[:100] or "(no message shown)")
        page.screenshot(path=str(out / "edit-refused.png"))

        print("\nAnd the part still works afterwards")
        check("the edit bar is usable again",
              not page.locator("#cmdIn").is_disabled()
              and not page.locator("#applyBtn").is_disabled())
        added, _ = apply_edit(page, "make the face width 15mm")
        check("a good edit still applies after refusals", added)

        # -----------------------------------------------------------------
        print("\nEditing a generated part, not just a catalogue one")
        page.click("#histBtn")
        page.wait_for_timeout(800)
        page.click("#histClose")
        page.wait_for_timeout(400)
        check("no JavaScript errors so far",
              not [c for c in console if "favicon" not in c.lower()],
              "; ".join(console[:2]))

        page.screenshot(path=str(out / "edit-final.png"))
        browser.close()

    print("\nTiming per edit - parameter patches, so no model call")
    for instruction, seconds in timings:
        print(f"  {instruction:<34} {seconds:5.1f}s")
    if timings:
        print(f"  {'average':<34} "
              f"{sum(s for _, s in timings) / len(timings):5.1f}s")

    real = [c for c in console if "favicon" not in c.lower()]
    print("\nConsole")
    check("no JavaScript errors", not real, "; ".join(real[:3]))

    print(f"\nScreenshots in {out}")
    print("=" * 60)
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures[:6])}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
