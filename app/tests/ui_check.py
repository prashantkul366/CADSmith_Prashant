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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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

    # Record a fresh two-iteration run and drive the test against that exact
    # job. Reusing whatever happens to be in app/runs made the test depend on
    # earlier runs of itself, which had already added an edit version.
    print("\nRecording a run to test against")
    from app.server.jobs import JobManager
    from app.tools.seed_demo_run import RUNS_DIR, seed

    job_id = seed("bracket", JobManager(RUNS_DIR))

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

        print("\nModel backend picker")
        options = page.evaluate(
            "[...document.querySelectorAll('#optProvider option')].map(o => o.value)")
        for wanted in ("anthropic", "openai", "ollama", "lmstudio", "custom"):
            check(f"{wanted} offered", wanted in options, str(options))
        check("a provider is selected", bool(page.locator("#optProvider").input_value()))
        check("unconfigured providers say so",
              "needs setup" in page.evaluate(
                  "[...document.querySelectorAll('#optProvider option')]"
                  ".map(o => o.textContent).join('|')"))

        page.select_option("#optProvider", "ollama")
        page.wait_for_timeout(500)
        note = page.locator("#providerNote").inner_text()
        check("an unreachable local provider is reported honestly",
              "Nothing is listening" in note, note[:70])
        check("generate is blocked with no usable backend",
              page.locator("#genBtn").is_disabled())
        check("the key field appears when setup is needed",
              page.locator("#keyRow").is_visible())

        page.select_option("#optProvider", "custom")
        page.wait_for_timeout(400)
        check("a custom backend exposes its base URL",
              page.locator("#providerBase").is_visible())

        page.select_option("#optProvider", "anthropic")
        page.wait_for_timeout(400)
        check("model roles are prefilled per provider",
              page.locator("#optGenModel").input_value()
              != page.locator("#optJudgeModel").input_value(),
              f'{page.locator("#optGenModel").input_value()} / '
              f'{page.locator("#optJudgeModel").input_value()}')

        # Configure a backend through the UI, which is also how a key gets in.
        page.select_option("#optProvider", "custom")
        page.wait_for_timeout(400)
        page.fill("#providerBase", "http://127.0.0.1:9/v1")
        page.fill("#providerKey", "ui-entered-key")
        page.click("#saveKeyBtn")
        page.wait_for_timeout(1200)
        check("the provider becomes usable once configured",
              "needs setup" not in page.evaluate(
                  "document.querySelector('#optProvider')"
                  ".selectedOptions[0].textContent"),
              page.evaluate("document.querySelector('#optProvider')"
                            ".selectedOptions[0].textContent"))
        check("the key field is cleared after use",
              page.locator("#providerKey").input_value() == "")

        check("a gateway with no declared default leaves the model empty",
              page.locator("#optGenModel").input_value() == "",
              page.locator("#optGenModel").input_value())
        page.fill("#optGenModel", "same-model")
        page.fill("#optJudgeModel", "same-model")
        page.wait_for_timeout(400)
        check("using one model for both roles is flagged",
              "grades its own work" in page.locator("#providerNote").inner_text(),
              page.locator("#providerNote").inner_text()[:60])
        page.fill("#optJudgeModel", "stronger-model")
        page.wait_for_timeout(400)
        check("a distinct judge model clears the warning",
              "grades its own work" not in page.locator("#providerNote").inner_text())
        check("the pane header names the judge model",
              "STRONGER-MODEL" in page.locator("#judgeModelLabel").inner_text(),
              page.locator("#judgeModelLabel").inner_text())
        page.screenshot(path=str(out / "12-providers.png"))

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
        page.wait_for_timeout(900)
        runs = page.locator("#hlist .hitem")
        check("history lists runs", runs.count() > 0, f"{runs.count()} runs")
        page.screenshot(path=str(out / "03-history.png"))

        target = page.locator(f'#hlist .hitem[data-job="{job_id}"]')
        check("the run just recorded is listed", target.count() == 1, job_id)
        target.click()
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

        print("\nNatural-language edit (parameter patch, no API key needed)")
        # An edit applies to the version on screen, so select one explicitly
        # rather than relying on whichever was left selected.
        page.click('.iter[data-i="0"]')
        page.wait_for_timeout(2200)
        # Length is unambiguous: in this attempt the walls stand 45mm from
        # z=0, so thickening the base would not change the overall height.
        before = page.evaluate("Viewer.extents.x")
        page.fill("#cmdIn", "make the base length 140mm")
        page.click("#applyBtn")
        page.wait_for_function(
            "() => document.querySelectorAll('#iters .iter').length === 3",
            timeout=180000)
        page.wait_for_timeout(2500)
        check("an edit version appears",
              page.locator("#iters .iter").count() == 3)
        labels = page.evaluate(
            "[...document.querySelectorAll('#iters .iter .ilabel')].map(e => e.textContent.trim())")
        check("it is labelled as an edit",
              any("EDIT" in l for l in labels), str(labels))
        after = page.evaluate("Viewer.extents.x")
        check("the kernel rebuilt it at the new length",
              abs(before - 100.0) < 0.5 and abs(after - 140.0) < 0.5,
              f"{round(before, 1)}mm -> {round(after, 1)}mm")
        code_now = page.locator("#hl").inner_text()
        check("the patched source is shown",
              "base_length = 140.0" in code_now,
              next((l for l in code_now.splitlines()
                    if "base_length" in l), ""))
        check("the edit was applied to the selected attempt, not the newest",
              "support_height + base_thickness" not in code_now)
        verdict_now = page.locator("#valBody").inner_text()
        check("a patch is not credited to the Judge",
              "Judge" not in verdict_now.split("\n")[0],
              verdict_now.split("\n")[0])
        check("it says the Judge was not re-run",
              "JUDGE NOT RE-RUN" in verdict_now)
        page.screenshot(path=str(out / "09-edited.png"))

        print("\nEngineering drawing")
        page.click('.iter[data-i="1"]')
        page.wait_for_timeout(2000)
        page.click("#drawBtn")
        page.wait_for_selector("#paper svg", timeout=90000)
        page.wait_for_timeout(600)
        check("sheet opens", page.locator("#sheet.on").count() == 1)
        # SVG <text> has no innerText; read textContent from the DOM instead.
        labels = page.evaluate(
            "[...document.querySelectorAll('#paper svg text')].map(t => t.textContent)")
        for view in ("FRONT VIEW", "TOP VIEW", "RIGHT VIEW", "ISOMETRIC"):
            check(f"{view} projected", view in labels)
        check("title block carries kernel dimensions",
              any("100.0 x 60.0 x 55.0 mm" in t for t in labels),
              next((t for t in labels if " x " in t), ""))
        check("projection geometry present",
              page.locator("#paper svg path").count() > 40,
              f"{page.locator('#paper svg path').count()} paths")
        page.screenshot(path=str(out / "08-drawing.png"))
        page.click("#back3d")
        page.wait_for_timeout(400)

        print("\nReplay")
        page.click("#histBtn")
        page.wait_for_timeout(600)
        replay_buttons = page.locator("#hlist .hreplay")
        rows = page.locator("#hlist .hrow")
        check("every run offers a replay",
              rows.count() > 0 and replay_buttons.count() == rows.count(),
              f"{replay_buttons.count()} buttons for {rows.count()} runs")
        # Replay the two-iteration run: the last row is the oldest.
        row = page.locator(f'#hlist .hrow:has(.hitem[data-job="{job_id}"])')
        check("the recorded run can be replayed", row.count() == 1)
        row.locator(".hreplay").click()
        page.wait_for_timeout(1200)
        check("the header marks it as a replay",
              "REPLAY" in page.locator("#enginePill").inner_text(),
              page.locator("#enginePill").inner_text())
        page.screenshot(path=str(out / "10-replaying.png"))

        # The run has three versions by now: two iterations plus the edit.
        page.wait_for_function(
            "() => document.querySelectorAll('#iters .iter').length === 3",
            timeout=180000)
        page.wait_for_timeout(2500)
        check("the replay reproduced every version",
              page.locator("#iters .iter").count() == 3)
        page.click('.iter[data-i="1"]')
        page.wait_for_timeout(2200)
        replay_extents = page.evaluate(
            "Viewer.extents ? [Viewer.extents.x, Viewer.extents.z] : null")
        check("replayed geometry matches the original",
              replay_extents is not None
              and abs(replay_extents[0] - 100) < 0.5
              and abs(replay_extents[1] - 55) < 0.5,
              str([round(v, 1) for v in replay_extents] if replay_extents else None))
        replay_verdict = page.locator("#valBody").inner_text()
        check("the original Judge text is replayed",
              "Accepted by the Judge" in replay_verdict)
        page.screenshot(path=str(out / "11-replay-done.png"))

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
