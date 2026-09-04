"""Japanese support: the dictionary, the plumbing, and what must stay English.

Three things are checked, and the third matters most.

**Nothing is half-translated.**  A key present in one language and missing in
the other shows up as one English sentence in an otherwise Japanese screen,
which reads as a bug in the app rather than a gap in a dictionary.  Both
catalogues - the server's and the browser's - are checked entry by entry, and
every key the interface actually asks for is checked against the dictionary
that has to answer it.

**The catalogue answers Japanese.**  Every standard-part family is requested
in Japanese and has to come back exactly, and the requests that name a
standard part inside a bigger noun still have to be refused: 「Oリング用の溝」
is a groove, and handing back the o-ring is the silent substitution the whole
guard exists to stop.

**The model is still spoken to in English.**  The Refiner is steered by
English instructions, so the measurements it is given stay English whatever
the interface is set to.  Translating them would change what the pipeline
does, not what it says.

Run:  .venv/bin/python -m app.tests.test_i18n
"""

from __future__ import annotations

import io
import re
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.catalog import grounding, japanese, router  # noqa: E402
from app.server import budget, i18n, spec  # noqa: E402
from app.server.jobs import JobOptions  # noqa: E402

WEB = ROOT / "app" / "web"

#: Keys whose Japanese is legitimately not Japanese: punctuation, and the
#: name of a product that is written the same way in both languages.
IDENTICAL_OK = {"code.empty", "diag.cadquery"}

CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")


# ---------------------------------------------------------------------------
# Reading the browser's dictionary from Python
# ---------------------------------------------------------------------------

def parse_js_dict(source: str) -> dict[str, list[str]]:
    """Every ``"key": [english, japanese]`` entry in i18n.js.

    A real parse rather than a regex over the whole entry: the strings are
    written as concatenations across several lines, and a pattern that tried
    to match the closing bracket would stop at the first ``]`` inside one.
    """
    entries: dict[str, list[str]] = {}
    for match in re.finditer(r'"([\w.]+)":\s*\[', source):
        key = match.group(1)
        i = match.end()
        depth, in_string, quote, escaped = 1, False, "", False
        parts, current = [], []
        while i < len(source) and depth:
            ch = source[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    in_string = False
                else:
                    current.append(ch)
            elif ch in "\"'":
                in_string, quote = True, ch
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if not depth:
                    parts.append("".join(current))
            elif ch == "," and depth == 1:
                parts.append("".join(current))
                current = []
            i += 1
        entries[key] = parts
    return entries


def literal_keys(source: str) -> set[str]:
    """Keys the interface asks for by name: t("x"), I18N.has("x")."""
    # The closing quote must be followed by a comma or the closing bracket:
    # t("stage." + stage) builds its key at draw time and the literal here is
    # a prefix, not a key. Those families are listed out separately below.
    found = set()
    for call in (r"\bt", r"I18N\.has"):
        found |= set(re.findall(call + r'\(\s*"([\w.]+)"\s*[,)]', source))
    return found


def markup_keys(source: str) -> set[str]:
    """Keys the markup asks for: data-i18n, -ph, -title, -alt."""
    return set(re.findall(r'data-i18n(?:-(?:ph|title|alt))?="([\w.]+)"', source))


def main() -> int:  # noqa: C901 - a checklist, not a branchy function
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}"
              f"{(' - ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    # -- the server's catalogue --------------------------------------------
    print("\nThe server speaks both languages")
    missing = i18n.check()
    check("every server message has both languages", not missing,
          ", ".join(missing[:6]))
    untranslated = [key for key, entry in i18n.MESSAGES.items()
                    if not CJK.search(entry["ja"])]
    check("and the Japanese is actually Japanese", not untranslated,
          ", ".join(untranslated[:6]))

    check("a bare tag resolves", i18n.normalise("ja") == "ja")
    check("a region-qualified tag resolves", i18n.normalise("ja-JP") == "ja")
    check("case does not matter", i18n.normalise("JA") == "ja")
    check("a language we do not have falls back to English",
          i18n.normalise("fr") == "en")
    check("so does nothing at all", i18n.normalise(None) == "en")
    check("a browser asking for Japanese gets it",
          i18n.from_header("ja-JP,ja;q=0.9,en-US;q=0.8") == "ja")
    check("a browser asking for English gets it",
          i18n.from_header("en-GB,en;q=0.9") == "en")
    check("an unknown key returns itself rather than raising",
          i18n.t("no.such.key") == "no.such.key")
    check("a missing placeholder does not lose the message",
          "budget" in i18n.t("budget.stopped", "en").lower())

    japanese_stop = i18n.t("budget.stopped", "ja", limit="250,000",
                           calls=7, spent="32,000")
    english_stop = i18n.t("budget.stopped", "en", limit="250,000",
                          calls=7, spent="32,000")
    check("the spend ceiling explains itself in Japanese",
          CJK.search(japanese_stop) is not None
          and "250,000" in japanese_stop
          and "CADSMITH_TOKEN_BUDGET" in japanese_stop,
          japanese_stop[:60])
    check("and still in English", "250,000" in english_stop
          and not CJK.search(english_stop))

    print("\nA run carries the language it was started in")
    stopped = budget.Budget(limit=10, lang="ja")
    try:
        stopped.check({"input_tokens": 99, "output_tokens": 0, "calls": 3})
        check("a Japanese run is stopped in Japanese", False, "not stopped")
    except budget.BudgetExceeded as exc:
        check("a Japanese run is stopped in Japanese",
              CJK.search(str(exc)) is not None, str(exc)[:50])
    check("the default is English",
          not CJK.search(budget.Budget(limit=0).stopped or "x"))

    options = JobOptions.from_dict({"lang": "ja-JP"})
    check("the client's language reaches the job", options.lang == "ja")
    check("and survives being written to disk and read back",
          JobOptions.from_dict(asdict(options)).lang == "ja")
    check("a language we do not have does not break a run",
          JobOptions.from_dict({"lang": "xx"}).lang == "en")
    check("and neither does none at all", JobOptions().lang == "en")

    # -- the browser's catalogue -------------------------------------------
    print("\nThe interface speaks both languages")
    dictionary = parse_js_dict(io.open(WEB / "i18n.js", encoding="utf-8").read())
    check("the browser dictionary was read", len(dictionary) > 150,
          f"{len(dictionary)} keys")

    lopsided = [key for key, parts in dictionary.items()
                if len(parts) != 2 or not parts[0].strip() or not parts[1].strip()]
    check("every interface string has both languages", not lopsided,
          ", ".join(lopsided[:6]))

    same = [key for key, parts in dictionary.items()
            if len(parts) == 2 and not CJK.search(parts[1])
            and key not in IDENTICAL_OK]
    check("and none of them was left in English", not same,
          ", ".join(same[:6]))

    app_js = io.open(WEB / "app.js", encoding="utf-8").read()
    html = io.open(WEB / "index.html", encoding="utf-8").read()

    asked = literal_keys(app_js) | markup_keys(html)
    unknown = sorted(k for k in asked if k not in dictionary)
    check("every key the interface asks for exists", not unknown,
          ", ".join(unknown[:8]))
    check("and the interface asks for a lot of them", len(asked) > 100,
          f"{len(asked)} keys")

    # Built at draw time from a variable, so a regex over the source cannot
    # see them; each family is listed here instead.
    print("\nThe keys built from a variable are all present")
    families = {
        "stage.": ["plan", "code", "execute", "judge", "done"],
        "agent.": ["planner", "coder", "errorfix", "judge", "refiner"],
        "edit.step.": ["read", "apply", "rebuild", "validate", "done"],
        "spec.": ["solid_valid", "bbox", "num_holes", "num_holes.advisory",
                  "hole_diameter", "volume_estimate"],
    }
    for prefix, names in families.items():
        gaps = [prefix + name for name in names
                if prefix + name not in dictionary]
        check(f"{prefix}* is complete", not gaps, ", ".join(gaps))

    # Every check the kernel can emit must have a label in both languages, or
    # a measured failure reads as a key on screen.
    emitted = {"solid_valid", "bbox", "num_holes", "hole_diameter",
               "volume_estimate"}
    source = io.open(ROOT / "app" / "server" / "spec.py", encoding="utf-8").read()
    declared = set(re.findall(r'key="(\w+)"', source))
    check("every check spec.py emits has a label",
          declared <= emitted, ", ".join(sorted(declared - emitted)))

    # -- the catalogue in Japanese -----------------------------------------
    print("\nA standard part asked for in Japanese is still a standard part")
    wanted = [
        ("M10の平座金", "washer"),
        ("M8x30の六角穴付きボルト", "socket head screw"),
        ("M6の六角ナット", "hex nut"),
        ("内径20mm、線径2.5mmのOリング", "O-ring"),
        ("6203の深溝玉軸受", "Deep groove ball bearing"),
        ("直径3mm、長さ16mmの平行ピン", "Dowel pin"),
        ("線径2mm、外径20mm、自由長50mmの圧縮ばね", "Compression spring"),
        ("モジュール2、歯数24、歯幅10mm、穴径8mmの平歯車", "Spur gear"),
        ("GT2ベルト用、歯数20のタイミングプーリー", "timing pulley"),
    ]
    for request, expected in wanted:
        routed = router.select(request)
        title = routed.part.title if routed else ""
        check(f"{request[:22]} -> {expected}", expected.lower() in title.lower(),
              title or "nothing")

    print("\nAnd a part that merely mentions one is still refused")
    for request in ("内径20mm、線径2.5mmのOリング用の溝を持つカバープレート",
                    "M8ボルト4本用の取付ブラケット",
                    "608軸受用のハウジング",
                    "6203軸受のハウジング"):
        check(f"{request[:26]} falls through", router.select(request) is None,
              str(router.select(request)))

    print("\nAn English request is untouched by any of it")
    for request, expected in (("A flat washer for M10", "washer"),
                              ("An M8x30 socket head cap screw", "M8")):
        routed = router.select(request)
        check(f"{request[:28]}", routed is not None
              and expected.lower() in routed.part.title.lower(),
              routed.part.title if routed else "nothing")
    check("a bracket that mentions an M8 is still a bracket",
          router.select("A bracket that takes four M8 screws") is None)
    check("English text is never rewritten",
          not japanese.has_japanese("An M8x30 socket head cap screw"))

    print("\nStandard dimensions are found in a Japanese request too")
    for request, subject in (("NEMA 17ステッピングモーター用のマウント", "NEMA 17"),
                             ("6203軸受用のハウジング", "6203"),
                             ("M8ボルト4本用の取付ブラケット", "M8")):
        _, facts = grounding.ground(request)
        check(f"{request[:24]} grounds in {subject}",
              any(subject in fact.subject for fact in facts),
              ", ".join(f.subject for f in facts) or "nothing")

    # -- what must not be translated ---------------------------------------
    print("\nWhat the model reads is still English")
    report = spec.SpecReport(checks=[
        spec.SpecCheck(key="num_holes", label="hole count", expected="4",
                       actual="1", passed=False, hard=True)])
    feedback = report.feedback()
    check("the Refiner is given English measurements",
          not CJK.search(feedback) and "wanted" in feedback, feedback[:60])
    check("and the check keeps a stable key for the browser to translate",
          report.checks[0].key == "num_holes")
    server_source = io.open(ROOT / "app" / "server" / "spec.py",
                            encoding="utf-8").read()
    check("spec.py holds no translation of its own",
          "i18n" not in server_source and not CJK.search(server_source))
    for module in ("instrument.py", "jobs.py", "budget.py", "app.py",
                   "catalog_run.py"):
        text = io.open(ROOT / "app" / "server" / module, encoding="utf-8").read()
        check(f"{module} holds no Japanese literals of its own",
              not CJK.search(text))

    print(f"\n{'=' * 58}")
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures[:8])}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
