"""random persona agents

Revision ID: f4a8c2d6b901
Revises: e8b1f4c7a2d9
Create Date: 2026-08-27

This downgrade is destructive for random-persona data: the old schema cannot
represent random agents, so random agents, their runs, and their dependent
turns and tool calls are deleted. Memory with no remaining fixed owner is also
discarded.

"""

import json

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f4a8c2d6b901"
down_revision = "e8b1f4c7a2d9"
branch_labels = None
depends_on = None


_AGENT_PERSONA_CHECK = (
    "(persona_mode = 'fixed' AND user_username IS NOT NULL) OR "
    "(persona_mode = 'random' AND user_username IS NULL)"
)


def _agent_table(metadata, *, with_persona=True, with_check=False, user_nullable=True):
    columns = [
        sa.Column("id", sa.Integer(), primary_key=True),
    ]
    if with_persona:
        columns.append(
            sa.Column(
                "persona_mode",
                sa.String(length=12),
                nullable=False,
                server_default="fixed",
            )
        )
    columns.extend(
        [
            sa.Column("user_username", sa.String(length=50), nullable=user_nullable),
            sa.Column("autonomy_tier", sa.String(length=20), nullable=False),
            sa.Column("is_enabled", sa.Boolean(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("config", sa.JSON(), nullable=False),
            sa.Column("state", sa.JSON(), nullable=False),
            sa.Column("last_run_at", sa.DateTime(), nullable=True),
            sa.Column("next_run_at", sa.DateTime(), nullable=True),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["user_username"], ["user.username"]),
            sa.UniqueConstraint("user_username"),
        ]
    )
    if with_check:
        columns.append(
            sa.CheckConstraint(_AGENT_PERSONA_CHECK, name="ck_agent_persona_mode_user")
        )
    return sa.Table("agent", metadata, *columns)


def _agent_run_table(metadata, *, with_persona=True, persona_nullable=True):
    columns = [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Integer(), nullable=False),
    ]
    if with_persona:
        columns.append(
            sa.Column(
                "persona_username",
                sa.String(length=50),
                nullable=persona_nullable,
            )
        )
    columns.extend(
        [
            sa.Column("trigger", sa.String(length=20), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("turn_count", sa.Integer(), nullable=False),
            sa.Column("action_count", sa.Integer(), nullable=False),
            sa.Column("token_usage", sa.JSON(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["agent_id"], ["agent.id"]),
        ]
    )
    columns.append(sa.Index("ix_agent_run_agent_id", "agent_id"))
    if with_persona:
        columns.append(sa.ForeignKeyConstraint(["persona_username"], ["user.username"]))
    return sa.Table("agent_run", metadata, *columns)


def _agent_memory_table(
    metadata,
    *,
    with_agent=True,
    with_user=False,
    agent_nullable=True,
    user_nullable=True,
):
    columns = [sa.Column("id", sa.Integer(), primary_key=True)]
    if with_agent:
        columns.append(sa.Column("agent_id", sa.Integer(), nullable=agent_nullable))
    if with_user:
        columns.append(
            sa.Column("user_username", sa.String(length=50), nullable=user_nullable)
        )
    columns.extend(
        [
            sa.Column("kind", sa.String(length=20), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        ]
    )
    if with_agent:
        columns.append(sa.ForeignKeyConstraint(["agent_id"], ["agent.id"]))
    if with_user:
        columns.append(sa.ForeignKeyConstraint(["user_username"], ["user.username"]))
    return sa.Table("agent_memory", metadata, *columns)


def _json_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def upgrade():
    bind = op.get_bind()
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    # Existing agent rows are fixed; the server default backfills persona_mode.
    op.add_column(
        "agent",
        sa.Column(
            "persona_mode",
            sa.String(length=12),
            nullable=False,
            server_default="fixed",
        ),
    )
    metadata = sa.MetaData()
    with op.batch_alter_table(
        "agent",
        copy_from=_agent_table(
            metadata, with_persona=True, with_check=False, user_nullable=False
        ),
    ) as batch_op:
        batch_op.alter_column(
            "user_username", existing_type=sa.String(length=50), nullable=True
        )
        batch_op.create_check_constraint(
            "ck_agent_persona_mode_user", _AGENT_PERSONA_CHECK
        )

    op.add_column(
        "user",
        sa.Column("agent_state", sa.JSON(), nullable=False, server_default="{}"),
    )
    bind = op.get_bind()
    agent_rows = bind.execute(
        sa.text(
            "SELECT user_username, state FROM agent WHERE user_username IS NOT NULL"
        )
    ).mappings()
    for row in agent_rows:
        state = _json_dict(row["state"])
        if not state or "subscriptions" not in state:
            continue
        subscriptions = state.pop("subscriptions")
        bind.execute(
            sa.text(
                "UPDATE user SET agent_state = :agent_state WHERE username = :username"
            ),
            {
                "agent_state": json.dumps({"subscriptions": subscriptions}),
                "username": row["user_username"],
            },
        )
        bind.execute(
            sa.text("UPDATE agent SET state = :state WHERE user_username = :username"),
            {
                "state": json.dumps(state),
                "username": row["user_username"],
            },
        )

    op.add_column(
        "agent_run",
        sa.Column("persona_username", sa.String(length=50), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE agent_run SET persona_username = "
            "(SELECT user_username FROM agent WHERE agent.id = agent_run.agent_id)"
        )
    )
    metadata = sa.MetaData()
    with op.batch_alter_table(
        "agent_run", copy_from=_agent_run_table(metadata, with_persona=True)
    ) as batch_op:
        batch_op.alter_column(
            "persona_username",
            existing_type=sa.String(length=50),
            nullable=False,
        )
    op.create_index("ix_agent_run_persona_username", "agent_run", ["persona_username"])
    op.create_index(
        "uq_agent_run_running_persona",
        "agent_run",
        ["persona_username"],
        unique=True,
        sqlite_where=sa.text("status = 'running'"),
    )

    op.add_column(
        "agent_memory",
        sa.Column("user_username", sa.String(length=50), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE agent_memory SET user_username = "
            "(SELECT user_username FROM agent WHERE agent.id = agent_memory.agent_id)"
        )
    )
    op.drop_index("ix_agent_memory_agent_id", table_name="agent_memory")
    with op.batch_alter_table(
        "agent_memory",
        copy_from=_agent_memory_table(
            metadata,
            with_agent=True,
            with_user=True,
            agent_nullable=False,
            user_nullable=True,
        ),
    ) as batch_op:
        batch_op.drop_column("agent_id")
        batch_op.alter_column(
            "user_username", existing_type=sa.String(length=50), nullable=False
        )
    op.create_index(
        "ix_agent_memory_user_kind_created",
        "agent_memory",
        ["user_username", "kind", "created_at"],
    )

    op.execute(
        sa.text(
            "UPDATE prompt_pin SET target_key = "
            "(SELECT CAST(id AS TEXT) FROM agent "
            "WHERE agent.user_username = prompt_pin.target_key) "
            "WHERE target_kind = 'agent' AND EXISTS "
            "(SELECT 1 FROM agent WHERE agent.user_username = prompt_pin.target_key)"
        )
    )
    bind.connection.connection.commit()
    bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade():
    bind = op.get_bind()
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    # dependent rows before deleting the agents themselves.
    bind.execute(
        sa.text(
            "DELETE FROM tool_call WHERE run_id IN "
            "(SELECT id FROM agent_run WHERE agent_id IN "
            "(SELECT id FROM agent WHERE user_username IS NULL))"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM agent_turn WHERE run_id IN "
            "(SELECT id FROM agent_run WHERE agent_id IN "
            "(SELECT id FROM agent WHERE user_username IS NULL))"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM agent_run WHERE agent_id IN "
            "(SELECT id FROM agent WHERE user_username IS NULL)"
        )
    )
    bind.execute(sa.text("DELETE FROM agent WHERE user_username IS NULL"))

    bind.execute(
        sa.text(
            "DELETE FROM agent_memory WHERE NOT EXISTS "
            "(SELECT 1 FROM agent WHERE agent.user_username = agent_memory.user_username)"
        )
    )

    rows = bind.execute(
        sa.text(
            "SELECT agent.id, agent.state, user.agent_state "
            "FROM agent JOIN user ON user.username = agent.user_username"
        )
    ).mappings()
    for row in rows:
        agent_state = _json_dict(row["agent_state"])
        if not agent_state or "subscriptions" not in agent_state:
            continue
        state = _json_dict(row["state"]) or {}
        state["subscriptions"] = agent_state["subscriptions"]
        bind.execute(
            sa.text("UPDATE agent SET state = :state WHERE id = :id"),
            {"state": json.dumps(state), "id": row["id"]},
        )

    bind.execute(
        sa.text(
            "UPDATE prompt_pin SET target_key = "
            "(SELECT user_username FROM agent WHERE CAST(agent.id AS TEXT) = "
            "prompt_pin.target_key) WHERE target_kind = 'agent' AND EXISTS "
            "(SELECT 1 FROM agent WHERE CAST(agent.id AS TEXT) = prompt_pin.target_key)"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM prompt_pin WHERE target_kind = 'agent' "
            "AND target_key != '' AND target_key NOT GLOB '*[^0-9]*' "
            "AND NOT EXISTS "
            "(SELECT 1 FROM agent WHERE CAST(agent.id AS TEXT) = prompt_pin.target_key)"
        )
    )

    op.drop_index("uq_agent_run_running_persona", table_name="agent_run")
    op.drop_index("ix_agent_run_persona_username", table_name="agent_run")
    metadata = sa.MetaData()
    with op.batch_alter_table(
        "agent_run",
        copy_from=_agent_run_table(metadata, with_persona=True, persona_nullable=False),
    ) as batch_op:
        batch_op.drop_column("persona_username")

    op.add_column("agent_memory", sa.Column("agent_id", sa.Integer(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE agent_memory SET agent_id = "
            "(SELECT id FROM agent WHERE agent.user_username = agent_memory.user_username)"
        )
    )
    metadata = sa.MetaData()
    with op.batch_alter_table(
        "agent_memory",
        copy_from=_agent_memory_table(
            metadata,
            with_agent=True,
            with_user=True,
            agent_nullable=True,
            user_nullable=False,
        ),
    ) as batch_op:
        batch_op.drop_column("user_username")
        batch_op.alter_column("agent_id", existing_type=sa.Integer(), nullable=False)
    op.create_index("ix_agent_memory_agent_id", "agent_memory", ["agent_id"])

    metadata = sa.MetaData()
    with op.batch_alter_table(
        "agent", copy_from=_agent_table(metadata, with_persona=True, with_check=True)
    ) as batch_op:
        batch_op.drop_constraint("ck_agent_persona_mode_user", type_="check")
        batch_op.alter_column(
            "user_username", existing_type=sa.String(length=50), nullable=False
        )
        batch_op.drop_column("persona_mode")

    op.drop_column("user", "agent_state")
    bind.connection.connection.commit()
    bind.exec_driver_sql("PRAGMA foreign_keys=ON")
