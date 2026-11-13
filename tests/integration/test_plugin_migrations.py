"""A plugin's own alembic branch and models (docs/plugins.md "Database tables and migrations").

A throwaway plugin package with a ``models`` module and a migration branch is written to a
temporary directory; alembic runs in subprocesses against a scratch database, so the plugin's
table never reaches this process's ``Base.metadata`` or the shared test database."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from scribe.db.session import create_engine
from scribe.plugins import plugin_modules_from_env
from tests.conftest import ROOT

PLUGIN = "scribe_test_tables_plugin"

MODELS = """
import uuid

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from scribe.db.base import Base


class PluginTestItem(Base):
    __tablename__ = "plugin_test_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    note: Mapped[str] = mapped_column(Text)
"""

MIGRATION = """
import sqlalchemy as sa
from alembic import op

revision = "plugin_test_0001"
down_revision = None
branch_labels = ("plugin_test",)
depends_on = "0023"


def upgrade() -> None:
    op.create_table(
        "plugin_test_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_plugin_test_items_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plugin_test_items")),
    )


def downgrade() -> None:
    op.drop_table("plugin_test_items")
"""


def _write_plugin(tmp_path: Path) -> tuple[Path, Path]:
    """The plugin package under ``site/`` and an alembic.ini that also reads its migrations."""
    site = tmp_path / "site"
    package = site / PLUGIN
    migrations = tmp_path / "migrations"
    package.mkdir(parents=True)
    migrations.mkdir()
    (package / "__init__.py").write_text("")
    (package / "models.py").write_text(MODELS)
    (migrations / "plugin_test_0001.py").write_text(MIGRATION)

    ini = (ROOT / "alembic.ini").read_text()
    ini = ini.replace("script_location = %(here)s/alembic", f"script_location = {ROOT}/alembic")
    locations = os.pathsep.join([str(ROOT / "alembic" / "versions"), str(migrations)])
    ini = ini.replace("[alembic]\n", f"[alembic]\nversion_locations = {locations}\n", 1)
    config = tmp_path / "alembic.ini"
    config.write_text(ini)
    return site, config


def _alembic(
    config: Path, database_url: str, site: Path, plugins: str, *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(config), *args],
        cwd=ROOT,
        env={
            **os.environ,
            "SCRIBE_DATABASE_URL": database_url,
            "SCRIBE_PLUGINS": plugins,
            "PYTHONPATH": os.pathsep.join(filter(None, [str(site), os.environ.get("PYTHONPATH")])),
        },
        capture_output=True,
        text=True,
    )


def _revisions(output: str) -> set[str]:
    """Revision ids from ``alembic heads`` / ``alembic current`` output."""
    return {line.split()[0] for line in output.splitlines() if line.strip()}


async def test_plugin_branch_upgrades_with_heads_and_check_sees_plugin_models(
    migrated_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, config = _write_plugin(tmp_path)
    # Plugins already configured for alembic's working directory (environment, then .env) stay
    # enabled: their branches are in the versions directory and their tables in the database.
    monkeypatch.chdir(ROOT)
    existing = plugin_modules_from_env()
    without = ",".join(existing)
    plugins = ",".join([*existing, PLUGIN])
    name = f"scribe_plugin_branch_{uuid.uuid4().hex[:12]}"
    admin = create_engine(migrated_database_url)
    scratch_url = (
        make_url(migrated_database_url).set(database=name).render_as_string(hide_password=False)
    )
    async with admin.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        heads = _alembic(config, scratch_url, site, plugins, "heads")
        assert heads.returncode == 0, heads.stdout + heads.stderr
        assert "plugin_test_0001" in _revisions(heads.stdout)
        assert len(_revisions(heads.stdout)) >= 2

        single = _alembic(config, scratch_url, site, plugins, "upgrade", "head")
        assert single.returncode != 0
        assert "Multiple head revisions" in single.stdout + single.stderr

        upgraded = _alembic(config, scratch_url, site, plugins, "upgrade", "heads")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

        current = _alembic(config, scratch_url, site, plugins, "current")
        assert _revisions(current.stdout) == _revisions(heads.stdout)

        with_plugin = _alembic(config, scratch_url, site, plugins, "check")
        assert with_plugin.returncode == 0, with_plugin.stdout + with_plugin.stderr

        without_plugin = _alembic(config, scratch_url, site, without, "check")
        assert without_plugin.returncode != 0
        assert "plugin_test_items" in without_plugin.stdout + without_plugin.stderr
    finally:
        async with admin.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


def test_a_broken_plugin_models_module_stops_alembic(
    migrated_database_url: str, tmp_path: Path
) -> None:
    site, config = _write_plugin(tmp_path)
    (site / PLUGIN / "models.py").write_text("import scribe_test_absent_dependency\n")
    result = _alembic(config, migrated_database_url, site, PLUGIN, "check")
    assert result.returncode != 0
    assert f"{PLUGIN}.models" in result.stdout + result.stderr
