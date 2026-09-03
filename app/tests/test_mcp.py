"""The MCP server, driven the way a client drives it.

A real subprocess speaking JSON-RPC over stdio, against a real CADSmith
server, building real geometry.  Nothing here inspects the module's functions
directly: if the handshake, the schemas or the transport were wrong, a client
would fail and these checks would not, which would be worse than useless.

The model backend is the mock provider, so the five agents run for real
without needing a key.

Run:  .venv/bin/python -m app.tests.test_mcp
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable
APP_PORT = 8155
MOCK_PORT = 8156
APP = f"http://127.0.0.1:{APP_PORT}"


class Client:
    """Just enough MCP client to prove the server is a real one."""

    def __init__(self, env: dict):
        self.proc = subprocess.Popen(
            [PYTHON, "-m", "app.tools.mcp_server"],
            cwd=str(ROOT), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self._id = 0

    def _send(self, method: str, params: dict | None = None,
              notify: bool = False) -> dict | None:
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self._id += 1
            message["id"] = self._id
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(
                    "MCP server closed the connection. stderr:\n"
                    + self.proc.stderr.read()[-2000:])
            reply = json.loads(line)
            if reply.get("id") == self._id:
                return reply

    def initialize(self) -> dict:
        reply = self._send("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "cadsmith-test", "version": "1"},
        })
        self._send("notifications/initialized", {}, notify=True)
        return reply

    def tools(self) -> list[dict]:
        return self._send("tools/list", {})["result"]["tools"]

    def call(self, name: str, arguments: dict) -> dict:
        reply = self._send("tools/call",
                           {"name": name, "arguments": arguments})
        if "error" in reply:
            return {"_error": reply["error"]}
        result = reply["result"]
        text = "".join(c.get("text", "") for c in result.get("content", []))
        if result.get("isError"):
            return {"_error": text}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_text": text}

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:                              # noqa: BLE001
            self.proc.kill()


def _wait_http(url: str, seconds: int = 90) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=3)
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:                              # noqa: BLE001
            time.sleep(1)
    return False


def main() -> int:
    failures: list[str] = []
    runs = Path(tempfile.mkdtemp(prefix="cadsmith_mcp_runs_"))
    out = Path(tempfile.mkdtemp(prefix="cadsmith_mcp_out_"))

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}"
              f"{(' - ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    env = dict(os.environ)
    env["CADSMITH_RUNS_DIR"] = str(runs)
    env["CADSMITH_URL"] = APP
    env["PYTHONPATH"] = str(ROOT)

    mock = subprocess.Popen(
        [PYTHON, "-m", "app.tools.mock_provider", "--port", str(MOCK_PORT)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    app = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "app.server.app:app",
         "--host", "127.0.0.1", "--port", str(APP_PORT)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = None

    try:
        if not _wait_http(f"{APP}/api/providers"):
            check("the app started", False, "no response")
            return 1
        urllib.request.urlopen(urllib.request.Request(
            f"{APP}/api/providers/custom/key",
            data=json.dumps({"api_key": "mock",
                             "base_url": f"http://127.0.0.1:{MOCK_PORT}/v1"}
                            ).encode(),
            headers={"Content-Type": "application/json"}), timeout=30)

        client = Client(env)

        print("\nThe handshake a real client performs")
        info = client.initialize()
        check("initialize is answered", "result" in info,
              json.dumps(info)[:150])
        server_info = info.get("result", {}).get("serverInfo", {})
        check("the server names itself", server_info.get("name") == "cadsmith",
              json.dumps(server_info))
        check("it declares a tools capability",
              "tools" in info.get("result", {}).get("capabilities", {}))

        print("\nThe tools it advertises")
        tools = {t["name"]: t for t in client.tools()}
        for name in ("generate_part", "edit_part", "measure_part",
                     "check_spec", "export_part", "find_standard_part",
                     "list_parts"):
            check(f"{name} is advertised", name in tools)
        check("every tool describes itself",
              all(t.get("description") for t in tools.values()))
        schema = tools["generate_part"]["inputSchema"]
        check("generate_part requires a description",
              "description" in schema.get("required", []),
              json.dumps(schema.get("required")))
        check("its optional arguments are typed",
              schema["properties"]["max_iterations"]["type"] == "integer")

        print("\nA standard part, with no model call")
        nut = client.call("find_standard_part",
                          {"designation": "An M8 hex nut, ISO 4032."})
        check("the nut is found", nut.get("found") is True, json.dumps(nut)[:200])
        check("its standard is named", "4032" in str(nut.get("standard")),
              str(nut.get("standard")))
        check("the kernel verified it", nut.get("verified") is True)
        check("it comes back as editable source",
              "cadquery" in (nut.get("code") or "").lower())
        check("with real measurements",
              (nut.get("measured") or {}).get("volume_mm3", 0) > 0,
              json.dumps(nut.get("measured")))

        vague = client.call("find_standard_part", {"designation": "a bracket"})
        check("a request that is not standard hardware returns nothing",
              vague.get("found") is False)
        check("and says what to do instead",
              "generate_part" in (vague.get("reason") or ""),
              str(vague.get("reason")))

        print("\nGenerating a part")
        part = client.call("generate_part", {
            "description": "A flat washer: outer diameter 20mm, inner "
                           "diameter 10.5mm, thickness 2mm.",
            "max_iterations": 1, "use_catalog": False, "use_vision": False,
        })
        check("a part id comes back", bool(part.get("part_id")),
              json.dumps(part)[:200])
        check("real geometry was built",
              (part.get("measured") or {}).get("volume_mm3", 0) > 0,
              json.dumps(part.get("measured")))
        check("the solid is watertight",
              (part.get("measured") or {}).get("watertight") is True)
        check("the CadQuery source is returned",
              "cadquery" in (part.get("code") or "").lower())
        check("trustworthiness is stated before anything else",
              list(part)[:2] == ["part_id", "trustworthy"], str(list(part)[:3]))

        part_id = part["part_id"]

        print("\nMeasuring and checking")
        measured = client.call("measure_part", {"part_id": part_id})
        check("measuring returns the same volume",
              measured["measured"]["volume_mm3"]
              == part["measured"]["volume_mm3"])

        holes = (measured.get("spec") or {}).get("holes")
        good = client.call("check_spec", {"part_id": part_id, "num_holes": 1})
        check("a washer has one hole", good.get("meets_specification") is True,
              good.get("summary", ""))

        bad = client.call("check_spec", {"part_id": part_id, "num_holes": 4})
        check("claiming four holes fails",
              bad.get("meets_specification") is False, bad.get("summary", ""))
        check("and the failure is a measurement, not an opinion",
              any(c["what"] == "hole count" and not c["passed"]
                  for c in bad.get("checks", [])),
              json.dumps(bad.get("checks"))[:200])

        size = client.call("check_spec",
                           {"part_id": part_id, "bbox_mm": [999, 999, 999]})
        check("a wrong overall size is reported",
              any(not c["passed"] for c in size.get("checks", [])),
              size.get("summary", ""))

        print("\nEditing")
        edited = client.call("edit_part", {"part_id": part_id,
                                           "instruction": "make it 5mm thick"})
        check("the edit is applied", not edited.get("error"),
              str(edited.get("error"))[:160])
        check("and reports which path took it",
              bool(edited.get("edit_method")), str(edited.get("edit_method")))
        check("the geometry actually changed",
              edited["measured"]["volume_mm3"]
              != part["measured"]["volume_mm3"],
              f"{part['measured']['volume_mm3']} -> "
              f"{edited['measured']['volume_mm3']}")

        print("\nExporting")
        for fmt, suffix, magic in (("step", ".step", "ISO-10303"),
                                   ("stl", ".stl", ""),
                                   ("py", ".py", "cadquery")):
            target = out / f"part{suffix}"
            written = client.call("export_part", {
                "part_id": part_id, "path": str(target), "fmt": fmt})
            ok = target.exists() and target.stat().st_size > 0
            check(f"{fmt.upper()} is written to disk", ok,
                  f"{written.get('bytes')} bytes")
            if ok and magic:
                head = target.read_text(errors="ignore")[:400].lower()
                check(f"the {fmt.upper()} file is really that format",
                      magic.lower() in head, head[:60])

        bad_fmt = client.call("export_part", {
            "part_id": part_id, "path": str(out / "x.dwg"), "fmt": "dwg"})
        check("an unsupported format is refused clearly",
              "step" in str(bad_fmt.get("error", "")).lower(),
              str(bad_fmt)[:160])

        print("\nListing")
        listed = client.call("list_parts", {"limit": 5})
        check("the part appears in the list",
              any(row.get("part_id") == part_id for row in listed),
              json.dumps(listed)[:200])

        print("\nBad input is reported, not crashed on")
        missing = client.call("measure_part", {"part_id": "no-such-part"})
        check("an unknown part id is an error, not a traceback",
              bool(missing.get("error")) and "Traceback" not in str(missing),
              str(missing)[:160])
        check("and the message is readable",
              "no such job" in str(missing.get("error", "")).lower(),
              str(missing.get("error"))[:120])

    finally:
        if client is not None:
            client.close()
        for proc in (app, mock):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(runs, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)

    print(f"\n{'=' * 58}")
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
