from pathlib import Path

import yaml

from kfocr.ui.live import BUNDLED_ICONS, data_dir, live_config
from kfocr.vision.icons import TemplateIconRecognizer
from scripts.release_tag import release_tag_matches_version

ROOT = Path(__file__).resolve().parents[2]


def test_release_tag_must_be_v_plus_the_exact_pyproject_version() -> None:
    assert release_tag_matches_version("v0.1.0", "0.1.0")
    assert not release_tag_matches_version("v0.1.0", "0.1.1")
    assert not release_tag_matches_version("0.1.0", "0.1.0")  # the v prefix is required
    assert not release_tag_matches_version("v0.1.0-beta.1", "0.1.0")


def test_frozen_app_keeps_its_data_in_localappdata_and_source_runs_keep_data_dir() -> None:
    env = {"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}
    assert data_dir(frozen=True, environ=env) == Path(env["LOCALAPPDATA"]) / "killfeed-lens"
    assert data_dir(frozen=False, environ=env) == Path("data")


def test_live_config_writes_into_the_data_dir_and_uses_its_roi_profile(tmp_path) -> None:
    config = live_config(tmp_path)
    assert config.roi.rect is None  # first run: calibration needed
    assert Path(config.storage.sqlite_path) == tmp_path / "kfocr.sqlite3"
    assert Path(config.debug.artifact_dir) == tmp_path / "sessions"
    (tmp_path / "roi.yaml").write_text(
        "roi: {profile: roi, rect: {x: 0.6, y: 0.0, w: 0.3, h: 0.2}}\n", encoding="utf-8")
    assert live_config(tmp_path).roi.rect.x == 0.6


def test_live_config_uses_the_bundled_icon_templates_unless_the_config_names_its_own(tmp_path):
    manifest = TemplateIconRecognizer(BUNDLED_ICONS).model_manifest()
    assert manifest["status"] == "ready" and manifest["manifest"]["version"] == "2026-10"
    assert live_config(tmp_path).vision.icon_template_dir == str(BUNDLED_ICONS)
    (tmp_path / "mine").mkdir()
    (tmp_path / "config.yaml").write_text("vision: {icon_template_dir: mine}\n", encoding="utf-8")
    assert Path(live_config(tmp_path).vision.icon_template_dir) == (tmp_path / "mine").resolve()


def test_release_workflow_runs_ci_on_v_tags_and_publishes_only_the_installer() -> None:
    text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    triggers = workflow[True]  # YAML 1.1 reads the bare `on` key as True
    assert triggers == {"push": {"tags": ["v*"]}}
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert jobs["verify"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["build-release"]["needs"] == "verify"
    assert jobs["build-release"]["runs-on"] == "windows-latest"
    assert [name for name, job in jobs.items() if "permissions" in job] == ["publish-release"]
    publish = jobs["publish-release"]["steps"][-1]
    assert publish["uses"].startswith("softprops/action-gh-release@")
    assert publish["with"]["files"].split() == ["release-assets/*-setup.exe"]  # no portable for now
    assert "draft" not in publish["with"] and "workflow_dispatch" not in text
    assert "scripts/release_tag.py" in text


def test_ci_keeps_the_linux_and_windows_checks() -> None:
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    test = ci["jobs"]["test"]
    assert test["strategy"]["matrix"]["os"] == ["ubuntu-latest", "windows-latest"]
    runs = {step.get("run"): step.get("if") for step in test["steps"]}
    assert runs["uv run ruff check src tests scripts"] is None
    assert runs["uv run mypy src scripts"] == "runner.os == 'Linux'"
    assert "uv run pytest -q" in runs and "uv run python scripts/audit_capabilities.py" in runs
    assert ci["jobs"]["smoke-packaged"]["runs-on"] == "windows-latest"


def test_installer_is_per_user_without_elevation() -> None:
    iss = (ROOT / "packaging/killfeed-lens.iss").read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in iss
    assert r"DefaultDirName={localappdata}\Programs\killfeed-lens" in iss
    assert "OutputBaseFilename=killfeed-lens-{#AppVersion}-setup" in iss


def test_git_lookup_never_flashes_a_console(monkeypatch) -> None:
    import subprocess
    import sys

    from kfocr import pipeline

    calls = []
    monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **kw: calls.append(kw) or
                        subprocess.CompletedProcess(a, 0, stdout="abc\n"))
    assert pipeline._git_commit() == "abc"
    assert calls[0]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert pipeline._git_commit() is None and len(calls) == 1  # installed app: git is not run
