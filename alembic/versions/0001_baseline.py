"""Initial schema baseline (revision 0001).

create_all remains the bootstrap for empty DBs; this revision documents
the schema version for alembic upgrade/downgrade workflows.
"""

from __future__ import annotations

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Baseline: tables are created by ai_company.db.init_db (create_all).
    # Future revisions should use op.create_table / alter_column.
    pass


def downgrade() -> None:
    pass
