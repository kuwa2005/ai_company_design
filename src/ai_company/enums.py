"""Enums for task / worker / budget / validation lifecycle."""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    BACKLOG = "BACKLOG"
    WAITING = "WAITING"
    READY = "READY"
    RUNNING = "RUNNING"
    REVIEW = "REVIEW"  # legacy alias
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    DEFERRED_BUDGET = "DEFERRED_BUDGET"


class TaskKind(StrEnum):
    IMPLEMENTATION = "implementation"
    VALIDATION = "validation"
    JUDGE = "judge"
    REPAIR = "repair"


class WorkerStatus(StrEnum):
    HIRED = "HIRED"
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    REPORTING = "REPORTING"
    TERMINATING = "TERMINATING"
    TERMINATED = "TERMINATED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    KILLED = "KILLED"


class ProjectStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class BudgetKind(StrEnum):
    RESERVE = "reserve"
    SPEND = "spend"
    RELEASE = "release"
    ADJUSTMENT = "adjustment"


class CostDecision(StrEnum):
    ALLOW = "ALLOW"
    THROTTLE = "THROTTLE"
    DEFER = "DEFER"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class StaffingStatus(StrEnum):
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FULFILLED = "FULFILLED"
    CANCELLED = "CANCELLED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ReviewVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNCERTAIN = "UNCERTAIN"


class JudgeVerdict(StrEnum):
    PASS = "PASS"
    REJECT = "REJECT"
    HUMAN_ESCALATION = "HUMAN_ESCALATION"


class ValidationPlanStatus(StrEnum):
    OPEN = "OPEN"
    WAITING_REVIEWS = "WAITING_REVIEWS"
    JUDGING = "JUDGING"
    CLOSED = "CLOSED"
