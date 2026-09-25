"""Opt-in live Cursor E2E (skipped unless AI_COMPANY_LIVE_CURSOR=1)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ai_company.services.result_parser import parse_for_kind

pytestmark = pytest.mark.skipif(
    os.environ.get("AI_COMPANY_LIVE_CURSOR") != "1",
    reason="Set AI_COMPANY_LIVE_CURSOR=1 to run live Cursor E2E",
)


FIXTURE_STDOUT = {
    "type": "result",
    "result": json.dumps(
        {
            "status": "completed",
            "changed_files": ["hello.txt"],
            "tests": {"passed": 1, "failed": 0},
            "commit": None,
            "issues": [],
        },
        ensure_ascii=False,
    ),
}


def test_parser_accepts_cursor_envelope_fixture() -> None:
    raw = json.dumps(FIXTURE_STDOUT)
    parsed = parse_for_kind(raw, "coding")
    assert parsed.ok
    assert parsed.result is not None
    assert parsed.result["changed_files"] == ["hello.txt"]


def test_live_cursor_fixture_repo(tmp_path: Path) -> None:
    if not shutil.which("agent"):
        pytest.skip("agent CLI not on PATH")
    repo = tmp_path / "fixture"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "live@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "live"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    # Minimal live call: ask agent to create hello.txt and emit JSON result
    prompt = (
        "Create hello.txt with content 'ok'. Then print ONLY a JSON object: "
        '{"status":"completed","changed_files":["hello.txt"],'
        '"tests":{"passed":1,"failed":0},"commit":null,"issues":[]}'
    )
    proc = subprocess.run(
        [
            "agent",
            "--print",
            "--output-format",
            "json",
            "--sandbox",
            "enabled",
            "--trust",
            "--workspace",
            str(repo),
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, proc.stderr
    parsed = parse_for_kind(proc.stdout, "coding")
    assert parsed.ok, parsed.error
    assert (repo / "hello.txt").exists() or (
        parsed.result and "hello.txt" in (parsed.result.get("changed_files") or [])
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "live@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "live"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("# fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def test_live_two_workers_parallel(tmp_path: Path) -> None:
    """§11: two real agent processes in parallel on isolated workspaces."""
    if not shutil.which("agent"):
        pytest.skip("agent CLI not on PATH")
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def run_one(name: str) -> tuple[str, int, bool]:
        repo = tmp_path / name
        _init_repo(repo)
        fname = f"{name}.txt"
        prompt = (
            f"Create {fname} with content 'ok'. Then print ONLY a JSON object: "
            f'{{"status":"completed","changed_files":["{fname}"],'
            f'"tests":{{"passed":1,"failed":0}},"commit":null,"issues":[]}}'
        )
        proc = subprocess.run(
            [
                "agent",
                "--print",
                "--output-format",
                "json",
                "--sandbox",
                "enabled",
                "--trust",
                "--workspace",
                str(repo),
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        parsed = parse_for_kind(proc.stdout, "coding")
        ok = proc.returncode == 0 and (
            (repo / fname).exists()
            or (
                parsed.ok
                and parsed.result is not None
                and fname in (parsed.result.get("changed_files") or [])
            )
        )
        return name, proc.returncode, ok

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(run_one, "w1"), pool.submit(run_one, "w2")]
        results = [f.result() for f in as_completed(futs)]
    assert len(results) == 2
    assert all(ok for _n, _code, ok in results), results
