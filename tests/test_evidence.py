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


# --- live Cursor regression (2026-11-18): read-only workers blamed for others' files ---


def _git_repo(path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )


def test_readonly_worker_not_blamed_for_state_dir_or_earlier_output(tmp_path: Path) -> None:
    from ai_company.services.evidence import collect_evidence, snapshot_head, snapshot_worktree

    ws = tmp_path / "proj"
    ws.mkdir()
    _git_repo(ws)
    # an earlier writer left an uncommitted design doc
    (ws / "docs").mkdir()
    (ws / "docs" / "scope.md").write_text("scope v1", encoding="utf-8")
    (ws / ".ai_company").mkdir()
    (ws / ".ai_company" / "ai_company.db").write_text("db", encoding="utf-8")

    ignore = [".ai_company"]
    base = snapshot_head(ws)
    pre = snapshot_worktree(ws, ignore_prefixes=ignore)
    assert pre.keys() == {"docs/scope.md"}

    # during the judge run the orchestrator keeps writing its own state
    (ws / ".ai_company" / "ai_company.db").write_text("db2", encoding="utf-8")
    (ws / ".ai_company" / "stdout.txt").write_text("out", encoding="utf-8")

    pack = collect_evidence(
        ws,
        baseline_ref=base,
        role="judge",
        allow_writes=False,
        pre_dirty=pre,
        ignore_prefixes=ignore,
    )
    assert pack.ok, pack.notes
    assert not pack.write_roles_violated


def test_readonly_worker_still_blamed_for_its_own_change(tmp_path: Path) -> None:
    from ai_company.services.evidence import collect_evidence, snapshot_head, snapshot_worktree

    ws = tmp_path / "proj"
    ws.mkdir()
    _git_repo(ws)
    (ws / "scope.md").write_text("v1", encoding="utf-8")
    base = snapshot_head(ws)
    pre = snapshot_worktree(ws)

    (ws / "scope.md").write_text("v2 edited by judge", encoding="utf-8")  # modified
    (ws / "new.txt").write_text("x", encoding="utf-8")  # created

    pack = collect_evidence(ws, baseline_ref=base, role="judge", allow_writes=False, pre_dirty=pre)
    assert pack.write_roles_violated
    assert set(pack.untracked_files) == {"scope.md", "new.txt"}
