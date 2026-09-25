"""Evidence collector unit tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ai_company.services.evidence import collect_evidence, snapshot_head


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def test_false_changed_files_detected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    baseline = snapshot_head(repo)
    assert baseline

    pack = collect_evidence(
        repo,
        baseline_ref=baseline,
        reported_files=["b.txt"],
        allow_writes=True,
    )
    assert not pack.ok
    assert "b.txt" in pack.missing_reported


def test_forbidden_path_flagged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "ok.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "ok.txt")
    _git(repo, "commit", "-m", "init")
    baseline = snapshot_head(repo)
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    pack = collect_evidence(
        repo,
        baseline_ref=baseline,
        reported_files=[".env"],
        allow_writes=True,
        forbidden_paths=[".env"],
    )
    assert not pack.ok
    assert any("path_policy_violation" in n for n in pack.notes)


def test_reviewer_write_forbidden(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    baseline = snapshot_head(repo)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")

    pack = collect_evidence(
        repo,
        baseline_ref=baseline,
        reported_files=[],
        role="test_reviewer",
        allow_writes=False,
    )
    assert pack.write_roles_violated
    assert not pack.ok
