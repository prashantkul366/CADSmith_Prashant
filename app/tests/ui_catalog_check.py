"""The catalogue in a real browser, including the edit path.

The claim this has to hold up is a strong one: a standard part is served
exactly, instantly, with no API key at all - and it is still editable,
because it is parametric source rather than a downloaded solid.

It also has to hold up the honesty claim. A part no agent produced must be
badged as such and must not carry a Judge verdict, or the demo is showing
catalogue geometry as evidence that the agents work.

Needs a running server (no model backend required):
    ./app/run_app.sh &
    python -m app.tests.ui_catalog_check
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


def run(page, prompt: str, timeout_ms: int = 180000) -> float:
    page.fill("#prompt", prompt)
    started = time.time()
    page.click("#genBtn")
    page.wait_for_function("() => S.busy === false", timeout=timeout_ms)
    page.wait_for_timeout(900)
    return time.time() - started


CATALOG_PROMPTS = [
    ("an M8x30 socket head cap screw", "iso4762_socket_head_m8x30"),
    ("a 20 tooth spur gear, module 2", "gear_spur_m2_z20"),
    ("a module 1.5 helical gear with 30 teeth", "gear_helical_m1.5_z30"),
    ("a 6203 bearing", "bearing_6203"),
    ("an M8 flat washer", "iso7089_m8"),
    ("a GT2 timing pulley with 20 teeth", "pulley_gt2_20t"),
    ("a compression spring, 2mm wire, 20mm od, 50mm long", "spring_d2_od20_l50"),
    ("a T5 timing pulley with 18 teeth", "pulley_t5_18t"),
    ("a 16 tooth sprocket", "sprocket_16t_p12.7"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8077")
    parser.add_argument("--out", default="/tmp/cadsmith_catalog")
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

        # -----------------------------------------------------------------
        print("\nNo model backend is configured")
        health = page.evaluate("S.health")
        check("the app knows it has no model backend",
              not health["checks"]["model_backend"]["ok"])
        check("the catalogue is reported in diagnostics",
              health["checks"].get("catalog", {}).get("ok") is True,
              health["checks"].get("catalog", {}).get("detail", ""))
        check("Generate is available anyway",
              not page.locator("#genBtn").is_disabled(),
              "a standard part needs no key")

        # -----------------------------------------------------------------
        print("\nStandard parts, served without a key")
        for prompt, part_id in CATALOG_PROMPTS:
            elapsed = run(page, prompt)
            timings.append((part_id, elapsed))
            versions = page.evaluate("S.versions")
            got = (versions or [{}])[0]
            cat = got.get("catalog") or {}
            ok = (got.get("source") == "catalog"
                  and cat.get("part_id") == part_id)
            extents = page.evaluate(
                "Viewer.extents ? [Viewer.extents.x, Viewer.extents.y, "
                "Viewer.extents.z] : null")
            check(f"'{prompt[:40]}'", ok and extents is not None,
                  f"{cat.get('part_id') or got.get('source')} · "
                  f"{[round(v, 1) for v in extents] if extents else 'no geometry'} · "
                  f"{elapsed:.1f}s")

        page.screenshot(path=str(out / "catalog-sprocket.png"))

        # -----------------------------------------------------------------
        print("\nProvenance is not hidden")
        run(page, "a 20 tooth spur gear, module 2")
        verdict = page.locator("#valBody").inner_text()
        check("it says the part came from the catalogue",
              "catalogue" in verdict.lower(), verdict.replace("\n", " ")[:80])
        # It may mention the Judge - saying one did not run is the honest
        # thing. What it must never do is claim a verdict.
        check("it does not claim a Judge accepted it",
              "Accepted by the Judge" not in verdict
              and "Rejected by the Judge" not in verdict,
              verdict.replace("\n", " ")[:80])
        check("the attempt card is badged CATALOG",
              "CATALOG" in page.locator("#iters").inner_text(),
              page.locator("#iters").inner_text().replace("\n", " ")[:40])
        check("the viewer titles it a standard part",
              "Standard part" in page.locator("#mtitle").inner_text(),
              page.locator("#mtitle").inner_text())
        code = page.locator("#codeScroll").inner_text()
        check("the code panel shows parametric source",
              "teeth_number" in code and "module" in code,
              code.replace("\n", " ")[:60])
        page.screenshot(path=str(out / "catalog-gear.png"))

        # -----------------------------------------------------------------
        print("\nA catalogue part is still editable")
        check("the edit bar is available", not page.locator("#cmdIn").is_disabled())
        page.fill("#cmdIn", "make it 40 teeth")
        started = time.time()
        page.click("#applyBtn")
        page.wait_for_function(
            "() => S.busy === false && S.versions.length >= 2", timeout=120000)
        page.wait_for_timeout(1200)
        edit_seconds = time.time() - started
        versions = page.evaluate("S.versions")
        check("the edit produced a new version", len(versions) >= 2,
              f"{len(versions)} versions in {edit_seconds:.1f}s")
        extents = page.evaluate(
            "Viewer.extents ? Viewer.extents.x : null")
        # module 2, 40 teeth -> tip diameter 2*40 + 2*2 = 84mm
        check("the gear really is 40 teeth now (tip dia 84mm)",
              extents is not None and abs(extents - 84.0) < 0.5,
              f"{extents:.2f}" if extents else "none")
        edited_code = page.locator("#codeScroll").inner_text()
        patched_lines = [line.strip() for line in edited_code.splitlines()
                         if "teeth_number" in line]
        check("the source was patched, not regenerated",
              any("40" in line and "=" in line for line in patched_lines),
              "; ".join(patched_lines[:2]))
        page.screenshot(path=str(out / "catalog-edited.png"))

        # -----------------------------------------------------------------
        print("\nA drawing from a catalogue part")
        page.click("#drawBtn")
        page.wait_for_function(
            "() => document.querySelector('#sheet') && "
            "document.querySelector('#sheet').innerHTML.includes('svg')",
            timeout=120000)
        check("an orthographic drawing is produced",
              page.evaluate("document.querySelector('#sheet').innerHTML.length")
              > 2000)
        page.screenshot(path=str(out / "catalog-drawing.png"))
        page.click("#back3d")
        page.wait_for_timeout(600)

        # -----------------------------------------------------------------
        print("\nA custom part still needs a model")
        page.fill("#prompt", "a bracket to hold a motor at 30 degrees")
        page.click("#genBtn")
        page.wait_for_timeout(3000)
        check("it refuses with a clear reason rather than pretending",
              page.locator("#ovErr").is_visible()
              or "key" in page.locator("#errMsg").inner_text().lower(),
              page.locator("#errMsg").inner_text()[:80]
              if page.locator("#ovErr").is_visible() else "no error shown")
        page.screenshot(path=str(out / "catalog-custom-refused.png"))

        print("\nHistory")
        page.click("#histBtn")
        page.wait_for_timeout(900)
        rows = page.locator("#hlist").inner_text()
        check("catalogue runs are badged in history", "CATALOG" in rows,
              rows.replace("\n", " ")[:70])
        page.click("#histClose")

        browser.close()

    print("\nTiming - no model call, so this is kernel time only")
    for part_id, seconds in timings:
        print(f"  {part_id:<30} {seconds:5.1f}s")
    if timings:
        print(f"  {'average':<30} "
              f"{sum(s for _, s in timings) / len(timings):5.1f}s")

    # The 503 is the refusal we deliberately provoked by asking for a custom
    # part with no backend - a browser network log, not a JavaScript fault.
    # Filtering it by hand would hide real ones, so match it narrowly.
    ignorable = ("favicon", "503 (service unavailable)")
    real = [c for c in console
            if not any(token in c.lower() for token in ignorable)]
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
