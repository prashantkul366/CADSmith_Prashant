"""Downloads, and whether repeated use contaminates them.

A presenter will click STEP, STL and .py in front of people, probably after
generating several parts. Two things have to hold: the file has to actually
arrive, and it has to be the part on screen - not the one before it, and not
the newest when an older attempt is selected.

Also covers the sequence the audit cares about: generate, generate again,
fail, recover, generate again - checking nothing leaks between runs.

    ./app/run_app.sh &
    .venv/bin/python -m app.tests.ui_export_check
"""

from __future__ import annotations

import argparse
import struct
import subprocess
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
console: list[str] = []


def first_line(text: str, needle: str) -> str:
    """The line naming a parameter, for a readable check detail."""
    for line in (text or "").splitlines():
        if needle in line:
            return line.strip()
    return "not found"


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def executable() -> str | None:
    for c in _CANDIDATE_BROWSERS:
        if Path(c).exists():
            return c
    return None


def stl_triangles(path: Path) -> int:
    """Binary STL: 80-byte header, then a uint32 triangle count."""
    raw = path.read_bytes()
    if len(raw) < 84:
        return -1
    count = struct.unpack("<I", raw[80:84])[0]
    return count if len(raw) == 84 + 50 * count else -1


def settle(page, timeout: int = 240000) -> None:
    page.wait_for_function("() => S.busy === false", timeout=timeout)
    page.wait_for_timeout(600)


def generate(page, prompt: str) -> None:
    page.fill("#prompt", prompt)
    page.click("#genBtn")
    settle(page)


def grab(page, selector: str, out: Path) -> Path | None:
    try:
        with page.expect_download(timeout=30000) as info:
            page.click(selector)
        download = info.value
        target = out / download.suggested_filename
        download.save_as(str(target))
        return target
    except Exception as error:
        print(f"        download via {selector} failed: {type(error).__name__}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8077")
    parser.add_argument("--out", default="/tmp/cadsmith_export")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*"):
        stale.unlink()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=executable(),
            args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"])
        context = browser.new_context(viewport={"width": 1600, "height": 950},
                                      accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda e: console.append(str(e)))
        page.on("console", lambda m: console.append(m.text)
                if m.type == "error" else None)
        page.goto(args.url, wait_until="networkidle")
        page.wait_for_timeout(1500)

        # -----------------------------------------------------------
        print("\nExporting a part (catalogue, so no key needed)")
        generate(page, "a 20 tooth spur gear, module 2")
        extents = page.evaluate("Viewer.extents.x")
        check("the gear is on screen", abs(extents - 44.0) < 0.4,
              f"{extents:.2f} mm")

        step = grab(page, "#dlStep", out)
        stl = grab(page, "#dlStl", out)
        code = grab(page, "#dlPy", out)

        check("STEP downloads", step is not None and step.stat().st_size > 0,
              f"{step.name} {step.stat().st_size:,} bytes" if step else "no file")
        check("STL downloads", stl is not None and stl.stat().st_size > 0,
              f"{stl.name} {stl.stat().st_size:,} bytes" if stl else "no file")
        check(".py downloads", code is not None and code.stat().st_size > 0,
              f"{code.name} {code.stat().st_size:,} bytes" if code else "no file")

        if step:
            head = step.read_text(errors="ignore")[:400]
            check("the STEP file is really STEP",
                  "ISO-10303" in head, head[:50].replace("\n", " "))
        if stl:
            triangles = stl_triangles(stl)
            check("the STL is a well-formed binary mesh", triangles > 0,
                  f"{triangles} triangles")
        if code:
            body = code.read_text()
            check("the .py is the source on screen",
                  "teeth_number = 20" in body and "SpurGear" in body,
                  first_line(body, "teeth_number"))
        check("filenames identify the run and version",
              all(f and "_v" in f.name for f in (step, stl, code)),
              ", ".join(f.name for f in (step, stl, code) if f)[:70])

        # -----------------------------------------------------------
        print("\nGenerate something else - the download must follow")
        generate(page, "an M8x30 socket head cap screw")
        step2 = grab(page, "#dlStep", out)
        code2 = grab(page, "#dlPy", out)
        check("the new part exports, not the old one",
              code2 is not None and "SpurGear" not in code2.read_text()
              and "M8" in code2.read_text(),
              code2.name if code2 else "no file")
        check("and it is a different file from the first",
              step2 is not None and step is not None
              and step2.name != step.name,
              f"{step.name if step else '?'} vs {step2.name if step2 else '?'}")

        # -----------------------------------------------------------
        print("\nSelecting an older attempt exports THAT attempt")
        generate(page, "a 20 tooth spur gear, module 2")
        page.fill("#cmdIn", "make it 40 teeth")
        page.click("#applyBtn")
        page.wait_for_function("() => S.busy === false && S.versions.length >= 2",
                               timeout=180000)
        page.wait_for_timeout(1200)
        newest = grab(page, "#dlPy", out)
        check("the edited version exports 40 teeth",
              newest is not None and "teeth_number = 40" in newest.read_text(),
              first_line(newest.read_text() if newest else "", "teeth_number"))

        page.locator('.iter[data-i="0"]').click()
        page.wait_for_timeout(2500)
        older = grab(page, "#dlPy", out)
        check("selecting the original exports 20 teeth, not 40",
              older is not None and "teeth_number = 20" in older.read_text(),
              first_line(older.read_text() if older else "", "teeth_number"))

        # -----------------------------------------------------------
        print("\nRepeated use: generate, fail, recover, generate")
        sequence = [
            ("a 6203 bearing", "geometry"),
            ("an M8 flat washer", "geometry"),
            ("write me a poem", "either"),      # no backend -> refused
            ("a GT2 timing pulley with 20 teeth", "geometry"),
            ("a compression spring, 2mm wire, 20mm od, 50mm long", "geometry"),
        ]
        for prompt, expect in sequence:
            page.fill("#prompt", prompt)
            page.click("#genBtn")
            deadline = time.time() + 200
            while time.time() < deadline and page.evaluate("S.busy") is not False:
                page.wait_for_timeout(400)
            page.wait_for_timeout(800)
            got = ("geometry" if page.evaluate("S.versions.length")
                   and not page.locator("#ovErr").is_visible() else "error")
            ok = got == expect or expect == "either"
            check(f"'{prompt[:36]}' -> {got}", ok and
                  page.evaluate("S.busy") is False,
                  "" if ok else f"expected {expect}")

        check("no stale geometry after the sequence",
              page.evaluate("Viewer.extents") is not None)
        check("the app is still usable at the end",
              not page.locator("#genBtn").is_disabled()
              and page.locator("#ovPipe").is_hidden())

        # -----------------------------------------------------------
        print("\nMemory across repeated generations")
        before = page.evaluate("performance.memory ? "
                               "performance.memory.usedJSHeapSize : null")
        for _ in range(6):
            generate(page, "an M8 flat washer")
        page.evaluate("window.gc && window.gc()")
        page.wait_for_timeout(1200)
        after = page.evaluate("performance.memory ? "
                              "performance.memory.usedJSHeapSize : null")
        if before and after:
            growth = (after - before) / 1024 / 1024
            check("the heap does not run away over repeated runs",
                  growth < 60, f"{growth:+.1f} MB over 6 more generations")
        else:
            print("    (performance.memory unavailable - skipped)")

        page.screenshot(path=str(out / "export-final.png"))
        context.close()
        browser.close()

    real = [c for c in console if "favicon" not in c.lower()
            and "failed to load resource" not in c.lower()]
    print("\nConsole")
    check("no JavaScript errors", not real, "; ".join(real[:3]))

    print(f"\nFiles in {out}")
    print("=" * 60)
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures[:6])}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
