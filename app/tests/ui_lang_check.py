"""Japanese in a real browser: the switch, the panels, and a Japanese run.

Reading the dictionary from Python proves the strings exist. It does not
prove they reach the screen, that the switch redraws the panels that were
already drawn, or that a Japanese request still gets an exact standard part.
So this drives the actual interface.

The interesting case is switching language *after* a part is on screen. Every
panel on the right is drawn by JavaScript from state, not from the markup, so
a naive implementation translates the chrome and leaves the verdict, the
measured checks and the token strip in the language they were drawn in.

Needs a running server (no model backend required):
    ./app/run_app.sh &
    python -m app.tests.ui_lang_check
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_CANDIDATE_BROWSERS = [
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
]

CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")

#: A standard part, so the run needs no model backend and finishes in seconds.
JAPANESE_PROMPT = "M10の平座金"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}"
          f"{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def executable() -> str | None:
    for candidate in _CANDIDATE_BROWSERS:
        if Path(candidate).exists():
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8077")
    parser.add_argument("--shots", default="")
    args = parser.parse_args()

    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=executable(), args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1600, "height": 950})
        page.goto(args.url, wait_until="networkidle")
        page.wait_for_timeout(1200)

        print("\nThe switch is there and English is the starting point")
        check("both languages are offered",
              page.locator("#langSw .lang").count() == 2)
        check("the app opens in English",
              page.get_attribute("html", "lang") == "en",
              page.get_attribute("html", "lang"))
        check("and reads as English",
              "Design Input" in page.inner_text(".col.left"))

        print("\nSwitching to Japanese redraws the interface")
        page.click('#langSw .lang[data-lang="ja"]')
        page.wait_for_timeout(500)
        check("the document declares Japanese",
              page.get_attribute("html", "lang") == "ja")
        for label, selector in (("the left column", ".col.left"),
                                ("the viewer toolbar", ".vtools"),
                                ("the right column", ".col.right"),
                                ("the editor bar", ".cmd")):
            text = page.inner_text(selector)
            check(f"{label} is in Japanese", CJK.search(text) is not None,
                  " ".join(text.split())[:40])
        check("the benchmark prompts are in Japanese",
              CJK.search(page.inner_text("#samples")) is not None)
        page.locator("#samples .sample").first.click()
        check("the prompt box now holds the Japanese prompt",
              CJK.search(page.input_value("#prompt")) is not None,
              page.input_value("#prompt")[:40])
        if shots:
            page.screenshot(path=str(shots / "01_japanese_idle.png"))

        print("\nA Japanese request is still served from the catalogue")
        page.fill("#prompt", JAPANESE_PROMPT)
        page.click("#genBtn")
        page.wait_for_function("() => S.busy === false", timeout=120000)
        page.wait_for_timeout(1200)

        check("a version was produced",
              page.evaluate("() => S.versions.length") >= 1)
        check("it came from the catalogue",
              page.evaluate("() => S.versions[0] && S.versions[0].source")
              == "catalog")
        check("and the part is the one that was asked for",
              "M10" in page.evaluate(
                  "() => (S.versions[0].catalog || {}).title || ''"),
              page.evaluate("() => (S.versions[0].catalog || {}).title || ''"))
        validation = page.inner_text("#valBody")
        check("the verdict is written in Japanese",
              CJK.search(validation) is not None,
              " ".join(validation.split())[:50])
        check("the status bar is in Japanese",
              CJK.search(page.inner_text("#mtitle")) is not None,
              page.inner_text("#mtitle"))
        check("the run log is in Japanese",
              CJK.search(page.inner_text("#plog")) is not None,
              " ".join(page.inner_text("#plog").split())[:50])
        check("no model was called, and it says so in Japanese",
              CJK.search(page.inner_text("#usage")) is not None,
              " ".join(page.inner_text("#usage").split())[:50])
        if shots:
            page.screenshot(path=str(shots / "02_japanese_part.png"))

        print("\nSwitching back redraws what was already on screen")
        page.click('#langSw .lang[data-lang="en"]')
        page.wait_for_timeout(500)
        check("the document declares English",
              page.get_attribute("html", "lang") == "en")
        english_validation = page.inner_text("#valBody")
        check("the verdict was redrawn, not left in Japanese",
              CJK.search(english_validation) is None,
              " ".join(english_validation.split())[:50])
        check("so was the status bar",
              CJK.search(page.inner_text("#mtitle")) is None,
              page.inner_text("#mtitle"))
        check("and the token strip",
              CJK.search(page.inner_text("#usage")) is None)
        check("the model is still on screen",
              page.evaluate("() => S.versions.length") >= 1)
        if shots:
            page.screenshot(path=str(shots / "03_back_to_english.png"))

        print("\nSwitching does not disturb what was typed into the form")
        page.fill("#optGenModel", "some/model-id")
        page.fill("#prompt", "M10の平座金")
        page.click('#langSw .lang[data-lang="ja"]')
        page.wait_for_timeout(400)
        check("a typed model id survives the switch",
              page.input_value("#optGenModel") == "some/model-id",
              page.input_value("#optGenModel"))
        check("and so does the prompt",
              page.input_value("#prompt") == "M10の平座金",
              page.input_value("#prompt"))
        page.click('#langSw .lang[data-lang="en"]')
        page.wait_for_timeout(400)
        check("and back again",
              page.input_value("#optGenModel") == "some/model-id",
              page.input_value("#optGenModel"))

        print("\nThe choice survives a reload")
        page.click('#langSw .lang[data-lang="ja"]')
        page.wait_for_timeout(300)
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1000)
        check("the page comes back in Japanese",
              page.get_attribute("html", "lang") == "ja")
        check("and so does the interface",
              CJK.search(page.inner_text(".col.left")) is not None)

        print("\nA refusal from the server is in Japanese too")
        page.fill("#prompt", "")
        message = page.evaluate("""async () => {
          try { await API.createJob("", {}); return "no error"; }
          catch (e) { return e.message; }
        }""")
        check("the server refuses in Japanese",
              CJK.search(message) is not None, message[:50])
        if shots:
            page.screenshot(path=str(shots / "04_japanese_reloaded.png"))

        browser.close()

    print(f"\n{'=' * 58}")
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
