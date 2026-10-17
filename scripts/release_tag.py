"""Release gate shared by CI and tests: the pushed tag must be exactly 'v' + pyproject version."""

import os
import sys
import tomllib
from pathlib import Path


def release_tag_matches_version(tag: str, version: str) -> bool:
    return tag == f"v{version}"


def main() -> int:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    tag = os.environ.get("GITHUB_REF_NAME", "")
    if not release_tag_matches_version(tag, version):
        print(f"release tag {tag!r} != 'v{version}' (pyproject.toml version)", file=sys.stderr)
        return 1
    print(f"release tag {tag} matches pyproject version {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
