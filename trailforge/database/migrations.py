from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import inspect, text

from trailforge.database.session import Database
from trailforge.models.audit import SchemaMigration


@dataclass(frozen=True)
class Migration:
    version: str
    description: str
    upgrade: Callable[[Database], None] | None = None


def _migration_0002(database: Database) -> None:
    # create_all already creates brand-new tables; existing databases only need
    # the reservation link column added to gear_loans.
    with database.engine.begin() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(gear_loans)")}
        if "reservation_item_id" not in columns:
            connection.execute(
                text(
                    "ALTER TABLE gear_loans ADD COLUMN reservation_item_id INTEGER "
                    "REFERENCES gear_reservation_items(id) ON DELETE SET NULL"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_gear_loans_reservation_item_id "
                    "ON gear_loans (reservation_item_id)"
                )
            )


MIGRATIONS = [
    Migration(version="0001", description="Initial TrailForge schema"),
    Migration(
        version="0002",
        description="Activity-level gear reservations and loan linkage",
        upgrade=_migration_0002,
    ),
]


def initialize_database(database: Database) -> list[str]:
    database.create_schema()
    applied: list[str] = []
    with database.session() as session:
        known = {
            row.version
            for row in session.query(SchemaMigration).order_by(SchemaMigration.version).all()
        }
        pending = [migration for migration in MIGRATIONS if migration.version not in known]
    for migration in pending:
        if migration.upgrade is not None:
            migration.upgrade(database)
        with database.session() as session:
            session.add(
                SchemaMigration(
                    version=migration.version,
                    description=migration.description,
                )
            )
        applied.append(migration.version)
    return applied


def migration_status(database: Database) -> dict[str, object]:
    inspector = inspect(database.engine)
    if "schema_migrations" not in inspector.get_table_names():
        return {
            "initialized": False,
            "applied": [],
            "pending": [item.version for item in MIGRATIONS],
        }
    with database.session() as session:
        applied = [
            row.version
            for row in session.query(SchemaMigration).order_by(SchemaMigration.version).all()
        ]
    pending = [item.version for item in MIGRATIONS if item.version not in set(applied)]
    return {"initialized": True, "applied": applied, "pending": pending}


def assert_database_integrity(database: Database) -> dict[str, object]:
    with database.engine.connect() as connection:
        integrity = connection.exec_driver_sql("PRAGMA integrity_check").scalar_one()
        foreign_key_rows = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    return {
        "integrity_check": str(integrity),
        "foreign_key_violations": [list(row) for row in foreign_key_rows],
        "healthy": integrity == "ok" and not foreign_key_rows,
    }
