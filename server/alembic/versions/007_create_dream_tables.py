"""Create the Dream tables (dream_runs, dream_cluster_states)

Revision ID: 007
Revises: 006
Create Date: 2026-09-17

`dream_runs` 是运行审计：一轮整合一行。
`dream_cluster_states` 是成本跳过表：一行代表「这个成员集合已被判定过」。

两张表都不参与正确性判定——表数据丢失只影响成本与可观测性，不改变整合结果
（设计 docs/design/memory-dream.md §4.3）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dream_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("scopes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clusters", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_clusters", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("observations_written", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("observations_superseded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("report_path", sa.Text(), nullable=True),
    )
    op.create_index("ix_dream_runs_started_at", "dream_runs", ["started_at"])

    op.create_table(
        "dream_cluster_states",
        sa.Column("observation_key", sa.String(length=64), primary_key=True),
        sa.Column("scope_user_id", sa.String(length=255), nullable=False),
        sa.Column("scope_agent_id", sa.String(length=255), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("observed_point_id", sa.String(length=64), nullable=True),
        sa.Column("member_ids_hash", sa.String(length=64), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluations", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_dream_cluster_states_scope_user_id", "dream_cluster_states", ["scope_user_id"])
    op.create_index("ix_dream_cluster_states_scope_agent_id", "dream_cluster_states", ["scope_agent_id"])


def downgrade() -> None:
    op.drop_index("ix_dream_cluster_states_scope_agent_id", table_name="dream_cluster_states")
    op.drop_index("ix_dream_cluster_states_scope_user_id", table_name="dream_cluster_states")
    op.drop_table("dream_cluster_states")
    op.drop_index("ix_dream_runs_started_at", table_name="dream_runs")
    op.drop_table("dream_runs")
