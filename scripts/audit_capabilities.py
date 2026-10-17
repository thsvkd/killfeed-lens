"""Static tripwire for prohibited application integrations; complements manual review."""

import argparse
import ast
import json
import re
import tomllib
from pathlib import Path

FORBIDDEN_MODULES = {"pymem", "pyautogui", "pydirectinput", "pynput", "keyboard", "mouse",
                     "scapy", "pyshark", "dpkt", "frida", "winappdbg"}
FORBIDDEN_CALLS = {"ReadProcessMemory", "WriteProcessMemory", "OpenProcess",
                  "CreateRemoteThread", "SetWindowsHookEx", "SendInput"}
_PEP508_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    findings = []
    for source_root in (args.root / "src", args.root / "scripts"):
        if not source_root.exists():
            continue
        for path in source_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [item.name.split(".")[0] for item in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                elif isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name):
                        names = [node.func.id]
                    elif isinstance(node.func, ast.Attribute):
                        names = [node.func.attr]
                for name in names:
                    if name in FORBIDDEN_MODULES | FORBIDDEN_CALLS:
                        findings.append({"file": path.relative_to(args.root).as_posix(),
                                         "line": getattr(node, "lineno", 0), "capability": name})
    project = args.root / "pyproject.toml"
    if project.exists():
        metadata = tomllib.loads(project.read_text(encoding="utf-8"))
        deps = list(metadata.get("project", {}).get("dependencies", []))
        for group in metadata.get("project", {}).get("optional-dependencies", {}).values():
            deps.extend(group)
        for dep in deps:
            match = _PEP508_NAME.match(dep)
            name = match.group(1).lower().replace("-", "_") if match else ""
            if name in FORBIDDEN_MODULES:
                findings.append({"file": "pyproject.toml", "capability": dep})
    print(json.dumps({"status": "fail" if findings else "pass", "findings": findings}, indent=2))
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
