"""Add tasks.department_id / tasks.section_id (部/課 ownership) — revision 0002.

The application also applies this additively at engine start
(``ai_company.db.migrate_additive_columns``), so ``alembic upgrade`` is
optional for SQLite users; this revision keeps the alembic history honest.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_task_org_units"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    # 0001_baseline is a no-op (tables come from ``create_all``); on a fresh DB
    # there is nothing to alter yet — the app creates ``tasks`` with the columns.
    if "tasks" not in insp.get_table_names():
        return
    have = {c["name"] for c in insp.get_columns("tasks")}
    indexes = {ix["name"] for ix in insp.get_indexes("tasks")}
    if "department_id" not in have or "section_id" not in have:
        with op.batch_alter_table("tasks") as batch:
            if "department_id" not in have:
                batch.add_column(sa.Column("department_id", sa.String(64), nullable=True))
            if "section_id" not in have:
                batch.add_column(sa.Column("section_id", sa.String(64), nullable=True))
    if "ix_tasks_department_id" not in indexes:
        op.create_index("ix_tasks_department_id", "tasks", ["department_id"])
    # Same idempotent backfill the app runs at start-up (only NULL rows).
    from ai_company.db import backfill_task_org_units

    backfill_task_org_units(bind)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "tasks" not in insp.get_table_names():
        return
    have = {c["name"] for c in insp.get_columns("tasks")}
    indexes = {ix["name"] for ix in insp.get_indexes("tasks")}
    if "ix_tasks_department_id" in indexes:
        op.drop_index("ix_tasks_department_id", table_name="tasks")
    drop = [c for c in ("section_id", "department_id") if c in have]
    if drop:
        with op.batch_alter_table("tasks") as batch:
            for col in drop:
                batch.drop_column(col)
