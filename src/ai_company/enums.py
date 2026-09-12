"""Enums for task / worker / budget lifecycle."""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    BACKLOG = "BACKLOG"
    WAITING = "WAITING"
    READY = "READY"
    RUNNING = "RUNNING"
    REVIEW = "REVIEW"
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    DEFERRED_BUDGET = "DEFERRED_BUDGET"


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
