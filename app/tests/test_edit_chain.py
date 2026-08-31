"""Edit the same part over and over, the way a person actually works.

One edit working is not the same as ten. What matters is that changes
*accumulate*: after "40 teeth" and then "module 3", the part must have both,
not the second applied to a forgotten first. That is the failure mode worth
hunting - an edit silently rebasing on the original code looks fine for one
step and quietly discards work on the next.

Also checked: a refused edit must leave the part exactly as it was and must
not consume a version number, editing an older version must branch from that
one rather than the newest, and a long chain must not drift.

Runs against the HTTP API on a catalogue part, so it needs a server but no
API key - every step here is a parameter patch with no model call.

    ./app/run_app.sh &
    .venv/bin/python -m app.tests.test_edit_chain
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

failures: list[str] = []
APP = "http://127.0.0.1:8077"


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        APP + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


def get(path: str) -> dict:
    with urllib.request.urlopen(APP + path, timeout=90) as response:
        return json.load(response)


def text(path: str) -> str:
    with urllib.request.urlopen(APP + path, timeout=90) as response:
        return response.read().decode()


def wait(job_id: str, limit: float = 240.0) -> dict:
    started = time.time()
    while True:
        state = get(f"/api/jobs/{job_id}")
        if state["job"].get("finished_at"):
            return state
        if time.time() - started > limit:
            raise TimeoutError(f"job {job_id} did not finish")
        time.sleep(0.4)


def latest(job: dict) -> dict:
    return max(job["versions"], key=lambda v: v["iteration"])


def code_of(job_id: str, iteration: int) -> str:
    return text(f"/api/jobs/{job_id}/v/{iteration}/code.py")


def geometry_of(job_id: str, iteration: int) -> dict:
    return json.loads(text(f"/api/jobs/{job_id}/v/{iteration}/geometry.json"))


def assignments(code: str) -> dict[str, float]:
    """The script's top-level numeric parameters, read back from source."""
    import re
    found = {}
    for line in code.splitlines():
        match = re.match(r"^([a-zA-Z_]\w*)\s*=\s*(-?\d+(?:\.\d+)?)\s*(#.*)?$",
                         line)
        if match:
            found[match.group(1)] = float(match.group(2))
    return found


def edit(job_id: str, instruction: str, version: int | None = None) -> dict:
    """Apply one edit and wait for it to actually finish.

    Waiting on finished_at alone is not enough: it still holds the previous
    run's value the instant the edit is queued, so a naive wait returns
    immediately and the next request hits a still-running job with a 409.
    Wait for the timestamp to *change*.
    """
    before = get(f"/api/jobs/{job_id}")["job"].get("finished_at")
    payload = {"instruction": instruction}
    if version is not None:
        payload["version"] = version
    post(f"/api/jobs/{job_id}/edit", payload)

    started = time.time()
    while True:
        state = get(f"/api/jobs/{job_id}")
        job = state["job"]
        if (job.get("status") not in ("queued", "running")
                and job.get("finished_at") != before):
            return state
        if time.time() - started > 240:
            raise TimeoutError(f"edit '{instruction}' did not finish")
        time.sleep(0.3)


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=APP)
    args = parser.parse_args()
    globals()["APP"] = args.url

    print("\nStarting part: a spur gear from the catalogue (no model needed)")
    job = post("/api/jobs", {
        "prompt": "a 20 tooth spur gear, module 2",
        "options": {"use_catalog": True}})["job"]
    state = wait(job["id"])
    job_id = job["id"]
    base = latest(state["job"])
    check("the gear was produced", base.get("source") == "catalog",
          str(base.get("source")))
    start_code = assignments(code_of(job_id, base["iteration"]))
    check("its source is parametric",
          {"module", "teeth_number", "face_width", "bore_diameter"}
          <= set(start_code), ", ".join(sorted(start_code)))

    # -----------------------------------------------------------------
    # A chain of edits. `expect` is the full set of values that must hold
    # AFTER this step - so every step re-asserts the earlier ones, which is
    # what catches an edit rebasing on the original code.
    print("\nEditing the same part over and over")
    chain = [
        ("make it 40 teeth",
         {"teeth_number": 40, "module": 2.0}),
        ("set the module to 3",
         {"teeth_number": 40, "module": 3.0}),
        ("make the face width 20mm",
         {"teeth_number": 40, "module": 3.0, "face_width": 20.0}),
        ("set the bore diameter to 12mm",
         {"teeth_number": 40, "module": 3.0, "face_width": 20.0,
          "bore_diameter": 12.0}),
        ("make it 30 teeth",
         {"teeth_number": 30, "module": 3.0, "face_width": 20.0,
          "bore_diameter": 12.0}),
        ("set the pressure angle to 25",
         {"teeth_number": 30, "module": 3.0, "face_width": 20.0,
          "bore_diameter": 12.0, "pressure_angle": 25.0}),
    ]

    seen_iterations = [base["iteration"]]
    for step, (instruction, expected) in enumerate(chain, start=1):
        state = edit(job_id, instruction)
        version = latest(state["job"])
        if version["iteration"] in seen_iterations:
            check(f"{step}. '{instruction}'", False,
                  "no new version was produced")
            continue
        seen_iterations.append(version["iteration"])
        values = assignments(code_of(job_id, version["iteration"]))
        wrong = {name: (values.get(name), want)
                 for name, want in expected.items()
                 if abs(values.get(name, -1e9) - want) > 1e-6}
        check(f"{step}. '{instruction}'", not wrong,
              "; ".join(f"{n}={got} want {want}"
                        for n, (got, want) in wrong.items())
              or f"v{version['iteration']}")

    # The whole point: the last version carries every change, not just the last
    final = latest(get(f"/api/jobs/{job_id}")["job"])
    values = assignments(code_of(job_id, final["iteration"]))
    check("every change accumulated onto one part",
          values.get("teeth_number") == 30 and values.get("module") == 3.0
          and values.get("face_width") == 20.0
          and values.get("bore_diameter") == 12.0
          and values.get("pressure_angle") == 25.0,
          ", ".join(f"{k}={v:g}" for k, v in sorted(values.items())))

    # ...and the kernel agrees. module 3, 30 teeth -> tip diameter 3*30+2*3=96
    geometry = geometry_of(job_id, final["iteration"])
    box = geometry.get("bounding_box", {})
    check("the kernel measures what the chain asked for (96 x 96 x 20)",
          abs(box.get("xlen", 0) - 96.0) < 0.3
          and abs(box.get("zlen", 0) - 20.0) < 0.1,
          f"{box.get('xlen', 0):.2f} x {box.get('ylen', 0):.2f} "
          f"x {box.get('zlen', 0):.2f}")

    # -----------------------------------------------------------------
    print("\nEdits that must be refused, without damaging the part")
    before = get(f"/api/jobs/{job_id}")["job"]
    before_count = len(before["versions"])
    before_code = code_of(job_id, latest(before)["iteration"])

    for instruction, why in [
        ("make it 30 teeth", "it is already 30"),
        ("set the bore diameter to -4mm", "a negative dimension"),
        ("make the flange diameter 90mm", "no such parameter"),
        ("make it nicer", "no target value"),
        ("set the diameter to 50mm", "a gear's diameter is derived, not declared"),
        ("make it 50mm across", "same, phrased differently"),
    ]:
        state = edit(job_id, instruction)
        now = state["job"]
        added = len(now["versions"]) - before_count
        check(f"'{instruction}' is refused ({why})", added == 0,
              f"{added} version(s) added")

    after = get(f"/api/jobs/{job_id}")["job"]
    check("the part is untouched after the refusals",
          len(after["versions"]) == before_count
          and code_of(job_id, latest(after)["iteration"]) == before_code)

    print("\nAnd editing still works afterwards")
    state = edit(job_id, "make the face width 25mm")
    values = assignments(code_of(job_id, latest(state["job"])["iteration"]))
    check("a good edit after refusals still applies",
          values.get("face_width") == 25.0
          and values.get("teeth_number") == 30,
          f"face_width={values.get('face_width')}, "
          f"teeth={values.get('teeth_number')}")

    # -----------------------------------------------------------------
    print("\nEditing an older version branches from that one")
    job2 = post("/api/jobs", {
        "prompt": "a 20 tooth spur gear, module 2",
        "options": {"use_catalog": True}})["job"]
    state = wait(job2["id"])
    root = latest(state["job"])["iteration"]
    state = edit(job2["id"], "make it 40 teeth")
    first_edit = latest(state["job"])["iteration"]
    state = edit(job2["id"], "make the face width 20mm")
    second = latest(state["job"])["iteration"]
    # Now go back to the original and edit that instead.
    state = edit(job2["id"], "set the bore diameter to 10mm", version=root)
    branched = assignments(code_of(job2["id"], latest(state["job"])["iteration"]))
    check("it branched from the original, not the newest",
          branched.get("bore_diameter") == 10.0
          and branched.get("teeth_number") == 20
          and branched.get("face_width") == 10.0,
          f"teeth={branched.get('teeth_number')} "
          f"face={branched.get('face_width')} "
          f"bore={branched.get('bore_diameter')}")
    kept = get(f"/api/jobs/{job2['id']}")["job"]["versions"]
    check("the earlier versions are still there", len(kept) == 4,
          f"{len(kept)} versions: {[v['iteration'] for v in kept]}")

    # -----------------------------------------------------------------
    print("\nA long chain does not drift")
    job3 = post("/api/jobs", {
        "prompt": "an M8x30 socket head cap screw",
        "options": {"use_catalog": True}})["job"]
    wait(job3["id"])
    lengths = [35, 40, 45, 50, 55, 60, 25, 30]
    ok = True
    for want in lengths:
        state = edit(job3["id"], f"make it {want}mm long")
        values = assignments(code_of(job3["id"],
                                     latest(state["job"])["iteration"]))
        if values.get("length") != float(want):
            ok = False
            check(f"length -> {want}", False, f"got {values.get('length')}")
            break
    if ok:
        check(f"{len(lengths)} edits in a row all applied", True,
              f"final length {lengths[-1]}mm")
    final3 = get(f"/api/jobs/{job3['id']}")["job"]
    check("every step is kept as its own version",
          len(final3["versions"]) == len(lengths) + 1,
          f"{len(final3['versions'])} versions")

    # -----------------------------------------------------------------
    # Everything above is a catalogue part, where every edit is a parameter
    # patch. A generated part mixes the two paths: some edits patch a number,
    # some go to the Refiner, and the chain has to survive alternating
    # between them - a Refiner edit rewrites the whole script, so the next
    # patch has to read parameters back out of code it did not write.
    print("\nA generated part: patches and Refiner edits alternating")
    mock = subprocess.Popen(
        [sys.executable, "-m", "app.tools.mock_provider", "--port", "8144"],
        cwd=str(Path(__file__).resolve().parents[2]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    try:
        post("/api/providers/custom/key",
             {"api_key": "mock", "base_url": "http://127.0.0.1:8144/v1"})
        job4 = post("/api/jobs", {
            "prompt": "an L-shaped mounting bracket with a gusset",
            "options": {"max_iterations": 1, "provider": "custom",
                        "generation_model": "mock-coder",
                        "judge_model": "mock-judge", "use_catalog": False}})["job"]
        state = wait(job4["id"])
        versions = state["job"]["versions"]
        check("the bracket was generated", len(versions) >= 1,
              f"{len(versions)} version(s)")

        start = assignments(code_of(job4["id"], latest(state["job"])["iteration"]))
        check("the generated script declares parameters",
              "thickness" in start, ", ".join(sorted(start))[:70])

        steps = [
            ("make it 15mm thick", "parameter patch"),
            ("add a reinforcing rib across the corner", "refiner agent"),
            ("make it 20mm thick", "parameter patch"),
        ]
        count = len(get(f"/api/jobs/{job4['id']}")["job"]["versions"])
        for instruction, expect_method in steps:
            state = edit(job4["id"], instruction)
            now = state["job"]["versions"]
            added = len(now) - count
            count = len(now)
            method = latest(state["job"]).get("method", "")
            check(f"'{instruction}' -> {expect_method}",
                  added == 1 and method == expect_method,
                  f"{added} version(s), method={method!r}")

        final = assignments(code_of(job4["id"],
                                    latest(get(f"/api/jobs/{job4['id']}")['job'])
                                    ["iteration"]))
        check("a patch after a Refiner edit still lands",
              final.get("thickness") == 20.0,
              f"thickness={final.get('thickness')}")
    finally:
        mock.terminate()
        mock.wait(timeout=10)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED:")
        for name in failures:
            print(f"   - {name}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
