"""Build worker prompts from templates + task fields."""

from __future__ import annotations

from pathlib import Path

from ai_company.db.models import Task
from ai_company.paths import repo_root


def prompts_dir() -> Path:
    override = __import__("os").environ.get("AI_COMPANY_PROMPTS_DIR")
    if override:
        return Path(override).resolve()
    return repo_root() / "prompts"


def load_role_prompt(role: str) -> str:
    mapping = {
        "coding_worker": "coding_worker.md",
        "backend_engineer": "coding_worker.md",
        "frontend_engineer": "coding_worker.md",
        "reviewer": "reviewer.md",
        "secretary": "secretary.md",
    }
    name = mapping.get(role, "coding_worker.md")
    path = prompts_dir() / name
    if not path.exists():
        return f"You are a short-lived {role} worker. Complete only the assigned task."
    return path.read_text(encoding="utf-8")


def build_task_prompt(task: Task) -> str:
    role_prompt = load_role_prompt(task.role_required)
    allowed = task.allowed_paths or []
    forbidden = task.forbidden_paths or []
    return f"""{role_prompt}

---
Task ID: {task.id}
Title: {task.title}
Objective: {task.objective or task.title}
Role: {task.role_required}
Priority: {task.priority}
Constraints: {task.constraints or "(none)"}
Acceptance Criteria: {task.acceptance_criteria or "(none)"}
Allowed Paths: {allowed or "(unspecified)"}
Forbidden Paths: {forbidden or "(unspecified)"}

When finished, print a single JSON object (no markdown fences) with keys:
status ("completed" or "failed"),
changed_files (array of strings),
tests (object with passed/failed ints),
commit (string or null),
issues (array of strings).
"""
