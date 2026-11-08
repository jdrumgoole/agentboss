"""The `agentboss` command: flags override AGENTBOSS_* variables, which override defaults."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentboss import cli
from agentboss.config import DEFAULT_STATE_DIR

ENV_VARS = (
    "AGENTBOSS_CLAUDE_HOME",
    "AGENTBOSS_STATE_DIR",
    "AGENTBOSS_REFRESH_INTERVAL",
    "AGENTBOSS_ALLOW_CONTROL",
)


@pytest.fixture
def run(monkeypatch):
    """Run main() with uvicorn stubbed out; returns the Settings it would serve."""
    for name in ENV_VARS:
        # setenv first so monkeypatch records the original value and restores
        # it at teardown, even though main() itself writes these under --reload.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    captured: dict = {}

    def fake_create_app(settings):
        captured["settings"] = settings
        return object()

    def fake_uvicorn_run(app, **kwargs):
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli.uvicorn, "run", fake_uvicorn_run)

    def _run(argv: list[str]) -> dict:
        captured.clear()
        captured["rc"] = cli.main(argv)
        return captured

    return _run


def test_defaults(run, tmp_path):
    out = run(["--claude-home", str(tmp_path)])
    s = out["settings"]
    assert out["rc"] == 0
    assert s.claude_home == tmp_path
    assert s.state_dir == DEFAULT_STATE_DIR
    assert s.refresh_interval == 1.0
    assert s.allow_control is False


def test_environment_is_honoured(run, monkeypatch, tmp_path):
    home = tmp_path / "claude"
    home.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("AGENTBOSS_CLAUDE_HOME", str(home))
    monkeypatch.setenv("AGENTBOSS_STATE_DIR", str(state))
    monkeypatch.setenv("AGENTBOSS_REFRESH_INTERVAL", "2.5")
    monkeypatch.setenv("AGENTBOSS_ALLOW_CONTROL", "1")

    s = run([])["settings"]
    assert s.claude_home == home
    assert s.state_dir == state
    assert s.refresh_interval == 2.5
    assert s.allow_control is True


def test_flags_override_environment(run, monkeypatch, tmp_path):
    env_home = tmp_path / "env-claude"
    env_home.mkdir()
    flag_home = tmp_path / "flag-claude"
    flag_home.mkdir()
    monkeypatch.setenv("AGENTBOSS_CLAUDE_HOME", str(env_home))
    monkeypatch.setenv("AGENTBOSS_STATE_DIR", str(tmp_path / "env-state"))
    monkeypatch.setenv("AGENTBOSS_REFRESH_INTERVAL", "2.5")

    s = run(
        [
            "--claude-home",
            str(flag_home),
            "--state-dir",
            str(tmp_path / "flag-state"),
            "--refresh",
            "0.5",
            "--allow-control",
        ]
    )["settings"]
    assert s.claude_home == flag_home
    assert s.state_dir == tmp_path / "flag-state"
    assert s.refresh_interval == 0.5
    assert s.allow_control is True


def test_missing_claude_home_from_environment(run, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTBOSS_CLAUDE_HOME", str(tmp_path / "nope"))
    assert run([])["rc"] == 1


def test_reload_exports_resolved_settings(run, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTBOSS_REFRESH_INTERVAL", "3.0")
    monkeypatch.setenv("AGENTBOSS_ALLOW_CONTROL", "yes")
    state = tmp_path / "state"

    out = run(["--claude-home", str(tmp_path), "--state-dir", str(state), "--reload"])
    assert out["app"] == "agentboss.app:app"
    assert out["kwargs"]["reload"] is True

    assert Path(os.environ["AGENTBOSS_CLAUDE_HOME"]) == tmp_path
    assert Path(os.environ["AGENTBOSS_STATE_DIR"]) == state
    # Env values the user set survive --reload instead of being reset to flag defaults.
    assert os.environ["AGENTBOSS_REFRESH_INTERVAL"] == "3.0"
    assert os.environ["AGENTBOSS_ALLOW_CONTROL"] == "1"
