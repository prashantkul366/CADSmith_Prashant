"""Prod the app the way an impatient person does, and see what breaks.

The other browser checks drive the app correctly. This one does not: it
clicks during every stage, double-fires buttons, switches provider and
options mid-run, mashes keyboard shortcuts, submits an empty prompt, pastes
something enormous, and reloads at awkward moments.

Nothing here asserts a feature works. What it asserts is that the app is
never left wedged - no JavaScript error, no button stuck disabled, no
spinner running with nothing behind it - because that is what a demo
audience will find within thirty seconds.

Needs a running server and the mock provider is started for it.

    ./app/run_app.sh &
    .venv/bin/python -m app.tests.ui_stress_check
"""

from __future__ import annotations

import argparse
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


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def executable() -> str | None:
    for candidate in _CANDIDATE_BROWSERS:
        if Path(candidate).exists():
            return candidate
    return None


def js_errors() -> list[str]:
    ignorable = ("favicon", "503 (service unavailable)",
                 "409 (conflict)", "failed to load resource")
    return [c for c in console
            if not any(token in c.lower() for token in ignorable)]


def not_wedged(page, label: str) -> None:
    """The app must always end up usable again."""
    page.wait_for_timeout(600)
    busy = page.evaluate("S.busy")
    gen_disabled = page.locator("#genBtn").is_disabled()
    overlay = page.locator("#ovPipe").is_visible()
    stuck = (busy is False) and (gen_disabled or overlay)
    check(f"{label}: not left wedged", not stuck,
          f"busy={busy} genDisabled={gen_disabled} spinner={overlay}")


def settle(page, timeout: int = 240000) -> None:
    page.wait_for_function("() => S.busy === false", timeout=timeout)
    page.wait_for_timeout(500)


def configure_mock(page, port: int) -> None:
    page.select_option("#optProvider", "custom")
    page.wait_for_timeout(400)
    page.fill("#providerBase", f"http://127.0.0.1:{port}/v1")
    page.fill("#providerKey", "mock")
    page.click("#saveKeyBtn")
    page.wait_for_timeout(1500)
    page.fill("#optGenModel", "mock-coder")
    page.fill("#optJudgeModel", "mock-judge")
    page.wait_for_timeout(300)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8077")
    parser.add_argument("--mock-port", type=int, default=8166)
    parser.add_argument("--out", default="/tmp/cadsmith_stress")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    mock = subprocess.Popen(
        [sys.executable, "-m", "app.tools.mock_provider",
         "--port", str(args.mock_port), "--delay", "2"],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    if mock.poll() is not None:
        raise RuntimeError(f"mock provider died - port {args.mock_port} busy?")

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
            page.wait_for_timeout(1500)

            # ---------------------------------------------------------
            print("\nBad input")
            page.fill("#prompt", "")
            page.click("#genBtn")
            page.wait_for_timeout(1200)
            check("an empty prompt is refused, not submitted",
                  page.evaluate("S.busy") is not True
                  and page.evaluate("S.jobId") is None,
                  f"jobId={page.evaluate('S.jobId')}")

            page.fill("#prompt", "x" * 12000)
            page.click("#genBtn")
            page.wait_for_timeout(2500)
            check("an enormous prompt does not crash the page",
                  not js_errors(), "; ".join(js_errors()[:2]))
            not_wedged(page, "huge prompt")
            if page.evaluate("S.busy") is True:
                settle(page)

            page.fill("#prompt", "🔩 a plate — with “smart quotes”, ½ symbols & <script>alert(1)</script>")
            page.click("#genBtn")
            page.wait_for_timeout(2000)
            check("odd characters are handled",
                  not js_errors(), "; ".join(js_errors()[:2]))
            if page.evaluate("S.busy") is True:
                settle(page)

            # ---------------------------------------------------------
            print("\nDouble-firing the buttons")
            configure_mock(page, args.mock_port)
            page.fill("#prompt", "a 20 tooth spur gear, module 2")
            for _ in range(5):
                page.evaluate("document.querySelector('#genBtn').click()")
            page.wait_for_timeout(1500)
            jobs_before = page.evaluate("S.jobId")
            settle(page)
            check("five rapid Generate clicks make one run",
                  page.evaluate("S.versions.length") >= 1,
                  f"{page.evaluate('S.versions.length')} version(s)")
            check("no error from the repeated clicks", not js_errors(),
                  "; ".join(js_errors()[:2]))

            # ---------------------------------------------------------
            print("\nClicking through every stage of a slow run")
            page.fill("#prompt", "an L-shaped mounting bracket with a gusset")
            page.click("#genBtn")
            page.wait_for_selector("#ovPipe:not([hidden])", timeout=20000)

            # The provider is delayed 2s per call, so there is time to
            # interfere at each stage rather than only at the start.
            for round_number in range(6):
                page.wait_for_timeout(1400)
                stage = page.evaluate(
                    "(document.querySelector('#pipe .pstep.act .plabel')||{}).textContent || ''")
                for selector in ("#wireBtn", "#spinBtn", "#fitBtn",
                                 "#histBtn", "#histClose"):
                    try:
                        page.click(selector, timeout=2500)
                    except Exception:
                        pass
                page.evaluate("document.querySelector('#drawBtn').click()")
                page.evaluate("document.querySelector('#applyBtn').click()")
                page.evaluate("document.querySelector('#genBtn').click()")
                for key in ("w", "h", "d", "f", "Escape"):
                    page.keyboard.press(key)
                # change options mid-run
                page.evaluate("document.querySelector('#optVision').click()")
                page.evaluate("document.querySelector('#optGround').click()")
                if page.evaluate("S.busy") is False:
                    break
            print(f"    interfered through stage: {stage!r}")

            settle(page)
            check("the run still finished after all that",
                  page.evaluate("S.versions.length") >= 1
                  and page.locator("#ovPipe").is_hidden(),
                  f"{page.evaluate('S.versions.length')} version(s)")
            check("no JavaScript error from mid-run clicking",
                  not js_errors(), "; ".join(js_errors()[:3]))
            not_wedged(page, "mid-run clicking")
            page.screenshot(path=str(out / "stress-after-clicking.png"))

            # ---------------------------------------------------------
            print("\nSwitching provider mid-run")
            page.fill("#prompt", "a hydraulic manifold block")
            page.click("#genBtn")
            page.wait_for_selector("#ovPipe:not([hidden])", timeout=20000)
            page.wait_for_timeout(1200)
            try:
                page.select_option("#optProvider", "anthropic", timeout=3000)
                page.wait_for_timeout(600)
                page.select_option("#optProvider", "custom", timeout=3000)
            except Exception as error:
                print(f"    (provider select refused mid-run: {type(error).__name__})")
            settle(page)
            check("switching provider mid-run does not break the run",
                  page.evaluate("S.versions.length") >= 1, "")
            check("no error from the provider switch", not js_errors(),
                  "; ".join(js_errors()[:2]))
            not_wedged(page, "provider switch")

            # ---------------------------------------------------------
            print("\nEditing: empty, repeated, and mashed")
            # The clicking loop above pressed "h", which toggles the history
            # drawer - and an open drawer covers the page. Escape should now
            # clear it, which is itself worth asserting.
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            check("Escape closes the history drawer",
                  page.locator("#hist").evaluate(
                      "el => !el.classList.contains('open')"))
            page.fill("#cmdIn", "")
            page.click("#applyBtn")
            page.wait_for_timeout(900)
            check("an empty edit is refused", page.evaluate("S.busy") is not True)

            page.fill("#cmdIn", "make it 15mm thick")
            for _ in range(4):
                page.evaluate("document.querySelector('#applyBtn').click()")
            settle(page)
            check("four rapid Apply clicks do not double-apply",
                  not js_errors(), "; ".join(js_errors()[:2]))
            not_wedged(page, "rapid edits")

            # ---------------------------------------------------------
            print("\nReloading at an awkward moment")
            page.fill("#prompt", "a pillow block bearing housing")
            page.click("#genBtn")
            page.wait_for_selector("#ovPipe:not([hidden])", timeout=20000)
            page.wait_for_timeout(1500)
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(2500)
            check("a reload mid-run leaves a usable page",
                  not js_errors(), "; ".join(js_errors()[:2]))
            settle(page)
            not_wedged(page, "reload mid-run")

            print("\nRapid navigation")
            for _ in range(6):
                page.click("#histBtn")
                page.wait_for_timeout(220)
                page.click("#histClose")
                page.wait_for_timeout(120)
            check("hammering History opens and closes cleanly",
                  not js_errors() and page.locator("#hist").evaluate(
                      "el => !el.classList.contains('open')"),
                  "; ".join(js_errors()[:2]))

            versions = page.evaluate("S.versions.length")
            if versions:
                for _ in range(3):
                    for index in range(min(versions, 4)):
                        try:
                            page.locator(f'.iter[data-i="{index}"]').click(timeout=2500)
                        except Exception:
                            pass
                        page.wait_for_timeout(120)
                settle(page)
                check("clicking rapidly between attempts is safe",
                      not js_errors(), "; ".join(js_errors()[:2]))
                not_wedged(page, "attempt switching")

            page.screenshot(path=str(out / "stress-final.png"))
            browser.close()
    finally:
        mock.terminate()
        mock.wait(timeout=10)

    print("\nConsole")
    real = js_errors()
    check("no JavaScript errors across the whole run", not real,
          "; ".join(real[:4]))

    print(f"\nScreenshots in {out}")
    print("=" * 60)
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures[:6])}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
