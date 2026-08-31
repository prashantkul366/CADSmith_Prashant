"""Drive a real generation in the browser, the way a person does it.

Covers what the other browser check does not: entering a key in the app,
picking models, pressing Generate, and watching a run go through the stages
to finished geometry - plus what the UI does when the provider misbehaves,
which is where a run that quietly appears "stuck" would show up.

Needs a running server and a running mock provider:
    python -m app.tools.mock_provider --port 8123 &
    ./app/run_app.sh &
    python -m app.tests.ui_generate_check
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_CANDIDATE_BROWSERS = [
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
]

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def executable() -> str | None:
    for candidate in _CANDIDATE_BROWSERS:
        if Path(candidate).exists():
            return candidate
    return None


def configure_provider(page, base_url: str) -> None:
    """Enter the backend through the UI, as a person would."""
    page.select_option("#optProvider", "custom")
    page.wait_for_timeout(400)
    page.fill("#providerBase", base_url)
    page.fill("#providerKey", "mock-key")
    page.click("#saveKeyBtn")
    page.wait_for_timeout(1200)
    page.fill("#optGenModel", "mock-coder")
    page.fill("#optJudgeModel", "mock-judge")
    page.wait_for_timeout(400)


def start_mock(port: int, mode: str) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-m", "app.tools.mock_provider",
         "--port", str(port), "--fail", mode],
        cwd=str(Path(__file__).resolve().parents[2]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.0)
    if process.poll() is not None:
        raise RuntimeError(
            f"mock provider for '{mode}' exited immediately - port {port} busy?")
    return process


def stop_mock(process: subprocess.Popen) -> None:
    """Wait for it to actually die, so the next one can bind its port."""
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8077")
    parser.add_argument("--mock-port", type=int, default=8123)
    parser.add_argument("--out", default="/tmp/cadsmith_ui")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base_url = f"http://127.0.0.1:{args.mock_port}/v1"

    console: list[str] = []
    mock = start_mock(args.mock_port, "none")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=executable(),
            args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_page(viewport={"width": 1600, "height": 950})
        page.on("pageerror", lambda e: console.append(str(e)))
        page.on("console", lambda m: console.append(m.text)
                if m.type == "error" else None)

        # ---------------------------------------------------------------
        print("\nEntering a backend through the UI")
        page.goto(args.url, wait_until="networkidle")
        page.wait_for_timeout(1200)
        configure_provider(page, base_url)
        check("generate becomes available once configured",
              not page.locator("#genBtn").is_disabled(),
              page.locator("#providerNote").inner_text()[:70])

        # ---------------------------------------------------------------
        print("\nA real run, end to end")
        page.fill("#prompt", "A rectangular plate 40mm x 30mm x 10mm thick "
                             "with a central 8mm hole.")
        page.click("#genBtn")

        page.wait_for_selector("#ovPipe:not([hidden])", timeout=15000)
        check("the pipeline overlay appears", True)
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / "20-running.png"))

        stages = page.evaluate(
            "[...document.querySelectorAll('#pipe .pstep')].length")
        check("all five stages are rendered together", stages == 5, str(stages))
        boxes = page.evaluate("""() => {
            const steps = [...document.querySelectorAll('#pipe .pstep')];
            return steps.map(s => Math.round(s.getBoundingClientRect().left));
        }""")
        check("stages share one column",
              len(set(boxes)) == 1, f"left offsets {sorted(set(boxes))}")

        page.wait_for_function(
            "() => document.querySelectorAll('#iters .iter').length >= 2",
            timeout=180000)
        page.wait_for_timeout(3000)

        check("the refinement loop ran",
              page.locator("#iters .iter").count() == 2,
              f"{page.locator('#iters .iter').count()} attempts")
        extents = page.evaluate(
            "Viewer.extents ? [Viewer.extents.x, Viewer.extents.y, Viewer.extents.z] : null")
        check("the corrected geometry is on screen",
              extents is not None and abs(extents[0] - 40) < 0.5
              and abs(extents[2] - 10) < 0.5,
              str([round(v, 1) for v in extents] if extents else None))
        check("the Judge's acceptance is shown",
              "Accepted by the Judge" in page.locator("#valBody").inner_text())
        check("the overlay is gone once finished",
              page.locator("#ovPipe").is_hidden())
        page.screenshot(path=str(out / "21-generated.png"))

        first = page.locator('.iter[data-i="0"]')
        first.click()
        page.wait_for_timeout(2500)
        rejected = page.locator("#valBody").inner_text()
        check("the rejected attempt explains itself",
              "Rejected by the Judge" in rejected and "20.0mm" in rejected,
              rejected.replace("\n", " ")[:80])

        browser.close()
    stop_mock(mock)

    # -------------------------------------------------------------------
    # Failure modes: each must surface, not hang. A fresh port per mode, so a
    # lingering server from the previous mode cannot answer for this one -
    # which silently made every failure look like a success.
    for offset, (mode, expect) in enumerate(
            (("429", "rate"), ("500", "500"),
             ("empty", "reasoning only"), ("badjson", "")), start=1):
        port = args.mock_port + offset
        mode_url = f"http://127.0.0.1:{port}/v1"
        print(f"\nProvider misbehaving: {mode}")
        mock = start_mock(port, mode)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                executable_path=executable(),
                args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"])
            page = browser.new_page(viewport={"width": 1600, "height": 950})
            page.on("pageerror", lambda e: console.append(str(e)))
            page.goto(args.url, wait_until="networkidle")
            page.wait_for_timeout(1000)
            configure_provider(page, mode_url)
            page.fill("#prompt", "A 20mm cube.")
            page.click("#genBtn")

            settled = False
            deadline = time.time() + 150
            while time.time() < deadline:
                if page.locator("#ovErr").is_visible():
                    settled = "error"
                    break
                if page.locator("#iters .iter").count() > 0:
                    settled = "geometry"
                    break
                page.wait_for_timeout(1000)

            if mode in ("429", "500", "empty"):
                check(f"{mode} surfaces an error instead of hanging",
                      settled == "error",
                      page.locator("#errMsg").inner_text()[:90]
                      if settled == "error" else f"settled={settled}")
                if settled == "error":
                    message = page.locator("#errMsg").inner_text()
                    check(f"{mode} error names the cause",
                          expect.lower() in message.lower() or mode in message,
                          message[:90])
            else:
                check("prose-wrapped JSON still produces geometry",
                      settled == "geometry", f"settled={settled}")

            page.screenshot(path=str(out / f"22-{mode}.png"))
            browser.close()
        stop_mock(mock)

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
