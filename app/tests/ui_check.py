"""Drive the real frontend in a browser and report what it renders.

Loads the page against a running server, opens a recorded run, and checks
that the panels are populated from backend data - the design plan, the
kernel-measured facts, the Judge's verdict, the iteration timeline and the
STL actually reaching the WebGL canvas.  Screenshots are written for review.

Usage:
    .venv/bin/python -m app.tests.ui_check [--url http://127.0.0.1:8077] [--out DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_URL = "http://127.0.0.1:8077"

# Prefer a Chromium already on the machine over one Playwright would fetch;
# the pinned browser build and the installed client version need not agree.
_CANDIDATE_BROWSERS = [
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
]


def _executable_path() -> str | None:
    for candidate in _CANDIDATE_BROWSERS:
        if Path(candidate).exists():
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--out", default="/tmp/cadsmith_ui")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    console_errors: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    with sync_playwright() as pw:
        executable = _executable_path()
        browser = pw.chromium.launch(
            executable_path=executable,
            args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"],
        )
        page = browser.new_page(viewport={"width": 1600, "height": 950})
        page.on("console", lambda m: console_errors.append(m.text)
                if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(str(e)))
        page.on("requestfailed",
                lambda r: console_errors.append(f"request failed: {r.url}"))
        page.on("response", lambda r: console_errors.append(
            f"HTTP {r.status}: {r.url}") if r.status >= 400 else None)

        print("\nPage load")
        page.goto(args.url, wait_until="networkidle")
        page.wait_for_timeout(1500)
        check("title", "CADSmith" in page.title(), page.title())
        check("three.js loaded", page.evaluate("typeof THREE !== 'undefined'"))
        check("viewer initialised", page.evaluate("typeof Viewer !== 'undefined'"))
        check("canvas present", page.locator("#gl canvas").count() == 1)

        health = page.locator("#healthChip span").inner_text()
        check("health chip reports state", health not in ("", "CHECKING…"), health)
        check("benchmark prompts listed",
              page.locator("#samples .sample").count() > 0,
              f"{page.locator('#samples .sample').count()} prompts")
        page.screenshot(path=str(out / "01-empty.png"))

        print("\nEnvironment panel")
        page.click("#healthChip")
        page.wait_for_timeout(400)
        check("diagnostics open", page.locator("#diag").is_visible())
        check("all checks listed", page.locator("#diag .drow").count() >= 4,
              f"{page.locator('#diag .drow').count()} rows")
        page.screenshot(path=str(out / "02-diagnostics.png"))
        page.keyboard.press("Escape")

        print("\nRecorded run")
        page.click("#histBtn")
        page.wait_for_timeout(600)
        runs = page.locator("#hlist .hitem")
        check("history lists runs", runs.count() > 0, f"{runs.count()} runs")
        page.screenshot(path=str(out / "03-history.png"))

        # The two-iteration bracket run is the interesting one.
        target = None
        for i in range(runs.count()):
            if "2 ITER" in runs.nth(i).inner_text():
                target = runs.nth(i)
                break
        check("a multi-iteration run exists", target is not None)
        (target or runs.first).click()
        page.wait_for_timeout(3000)

        print("\nLoaded model")
        check("viewer has geometry",
              page.evaluate("Viewer.extents !== null && Viewer.extents !== undefined"))
        extents = page.evaluate(
            "Viewer.extents ? [Viewer.extents.x, Viewer.extents.y, Viewer.extents.z] : null")
        check("STL parsed to the right size",
              extents is not None and abs(extents[0] - 100) < 0.5
              and abs(extents[2] - 55) < 0.5,
              f"{[round(v, 1) for v in extents] if extents else None}")

        facts = page.locator("#mfacts").inner_text()
        check("kernel facts shown", "WATERTIGHT" in facts, facts.replace("\n", " ")[:90])

        plan = page.locator("#planBody").inner_text()
        check("design plan populated", "base plate" in plan.lower(),
              plan.replace("\n", " ")[:70])

        code = page.locator("#hl").inner_text()
        check("real CadQuery source shown", "import cadquery" in code)
        check("code is the refined version", "support_height + base_thickness" in code)
        # A chained-replace highlighter corrupts its own markup; make sure the
        # rendered text carries no leaked class attributes.
        check("syntax highlighting is not corrupted",
              'class="' not in code and '">' not in code,
              next((line for line in code.splitlines() if '">' in line), ""))
        check("comments survive highlighting", "# Base plate" in code,
              code.splitlines()[6] if len(code.splitlines()) > 6 else "")

        verdict = page.locator("#valBody").inner_text()
        check("judge verdict shown", "Accepted by the Judge" in verdict)
        check("three-view render displayed",
              page.locator("#rthumb").count() == 1)

        iters = page.locator("#iters .iter")
        check("iteration timeline built", iters.count() == 2, f"{iters.count()} cards")
        page.screenshot(path=str(out / "04-loaded.png"))

        print("\nComparing iterations")
        iters.first.click()
        page.wait_for_timeout(2500)
        first_extents = page.evaluate(
            "Viewer.extents ? [Viewer.extents.x, Viewer.extents.y, Viewer.extents.z] : null")
        check("first attempt loads its own geometry",
              first_extents is not None and abs(first_extents[2] - 45) < 0.5,
              f"z={round(first_extents[2], 1) if first_extents else None} (rejected attempt was 45mm)")
        rejected = page.locator("#valBody").inner_text()
        check("first attempt shows the rejection",
              "Rejected by the Judge" in rejected)
        page.screenshot(path=str(out / "05-rejected-iteration.png"))

        print("\nViews")
        page.click('.vt[data-view="front"]')
        page.wait_for_timeout(900)
        page.screenshot(path=str(out / "06-front-view.png"))
        page.click("#wireBtn")
        page.wait_for_timeout(600)
        page.screenshot(path=str(out / "07-wireframe.png"))
        page.click("#wireBtn")

        print("\nConsole")
        real_errors = [e for e in console_errors if "favicon" not in e.lower()]
        check("no JavaScript errors", not real_errors,
              "; ".join(real_errors[:3]))

        browser.close()

    print(f"\nScreenshots in {out}")
    print("=" * 58)
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
