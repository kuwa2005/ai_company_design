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
    REPLAN = "replan"
    STILL_NEEDED = "still_needed"
    PROJECT_HEALTH = "project_health"
    PRODUCT_FIT = "product_fit"
    USABILITY_AUDIT = "usability_audit"
    CUSTOMER_SUCCESS = "customer_success"
    IT_OPS_AUDIT = "it_ops_audit"
    COMPLIANCE_AUDIT = "compliance_audit"
    TEST_AUDIT = "test_audit"
    QUALITY_AUDIT = "quality_audit"
    SALES_AUDIT = "sales_audit"
    MAINTENANCE_AUDIT = "maintenance_audit"
    # 重役会議の決議から起票される部門作業（文書成果物・コード変更なし）
    DEPARTMENT_WORK = "department_work"
    # 部長所見（短命の部長 Worker が会議の論点を判断する。既定オフ・読み取り専用）
    HEAD_BRIEF = "head_brief"
    # 招集見直し（キーワード判定の出席者に LLM が部を追加招集する。読み取り専用）
    ATTENDANCE_REVIEW = "attendance_review"


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
