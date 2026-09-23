from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from trailforge.config import Settings
from trailforge.database.migrations import (
    assert_database_integrity,
    initialize_database,
    migration_status,
)
from trailforge.database.session import Database
from trailforge.models.users import User


def test_settings_reject_non_sqlite_url() -> None:
    with pytest.raises(ValueError, match="SQLite"):
        Settings(database_url="unsupported://localhost/trailforge")


def test_database_enables_foreign_keys_and_wal(database: Database) -> None:
    details = database.verify_connection()
    assert details["foreign_keys"] == 1
    assert str(details["journal_mode"]).lower() == "wal"


def test_migration_initialization_is_idempotent(database: Database) -> None:
    assert initialize_database(database) == []
    status = migration_status(database)
    assert status == {"initialized": True, "applied": ["0001", "0002"], "pending": []}


def test_integrity_check_reports_healthy_database(database: Database) -> None:
    result = assert_database_integrity(database)
    assert result["integrity_check"] == "ok"
    assert result["foreign_key_violations"] == []
    assert result["healthy"] is True


def test_unique_constraint_rolls_back_transaction(database: Database) -> None:
    with pytest.raises(IntegrityError), database.session() as session:
        session.add(User(email="same@example.com", display_name="First"))
        session.flush()
        session.add(User(email="same@example.com", display_name="Second"))
        session.flush()
    with database.session() as session:
        assert session.query(User).count() == 0


def test_foreign_key_constraint_is_enforced(database: Database) -> None:
    with pytest.raises(IntegrityError), database.session() as session:
        session.execute(
            text(
                "INSERT INTO sport_profiles "
                "(user_id,height_cm,weight_kg,fitness_level,outdoor_experience,"
                "weekly_training_minutes,notes,created_at,updated_at) "
                "VALUES (999,170,70,'beginner','',0,'','2026-01-01T00:00:00Z',"
                "'2026-01-01T00:00:00Z')"
            )
        )


def test_file_database_survives_engine_restart(settings: Settings) -> None:
    first = Database(settings)
    initialize_database(first)
    with first.session() as session:
        session.add(User(email="persist@example.com", display_name="Persistent"))
    first.engine.dispose()
    second = Database(settings)
    initialize_database(second)
    with second.session() as session:
        restored = session.query(User).filter_by(email="persist@example.com").one()
        assert restored.display_name == "Persistent"
    second.engine.dispose()


def test_0002_upgrade_adds_reservation_column_to_legacy_loans(settings: Settings) -> None:
    from sqlalchemy import inspect as sa_inspect

    legacy = Database(settings)
    # Build a 0001-era database: create the full schema, then drop the new
    # reservation tables/columns and stamp only the 0001 migration.
    legacy.create_schema()
    with legacy.session() as session:
        session.execute(text("DROP TABLE IF EXISTS gear_reservation_items"))
        session.execute(text("DROP TABLE IF EXISTS gear_reservations"))
        session.execute(text("CREATE TABLE gear_loans_legacy_backup AS SELECT * FROM gear_loans"))
    legacy.engine.dispose()

    # Recreate gear_loans without the reservation column to mimic 0001.
    legacy = Database(settings)
    with legacy.session() as session:
        session.execute(text("ALTER TABLE gear_loans RENAME TO gear_loans_v2"))
        session.execute(
            text(
                "CREATE TABLE gear_loans ("
                "id INTEGER PRIMARY KEY, created_at VARCHAR(32) NOT NULL, "
                "updated_at VARCHAR(32) NOT NULL, version INTEGER NOT NULL, "
                "inventory_id INTEGER NOT NULL, borrower_id INTEGER NOT NULL, "
                "expedition_id INTEGER, quantity INTEGER NOT NULL, "
                "returned_quantity INTEGER NOT NULL, loaned_at VARCHAR(32) NOT NULL, "
                "due_at VARCHAR(32) NOT NULL, returned_at VARCHAR(32), "
                "status VARCHAR(24) NOT NULL, condition_out VARCHAR(24) NOT NULL, "
                "condition_in VARCHAR(24), notes TEXT NOT NULL)"
            )
        )
        session.execute(text("DROP TABLE gear_loans_v2"))
        session.execute(text("DROP TABLE gear_loans_legacy_backup"))
        session.execute(text("DELETE FROM schema_migrations"))
        session.execute(
            text(
                "INSERT INTO schema_migrations (version, description, applied_at) "
                "VALUES ('0001', 'Initial TrailForge schema', '2026-01-01T00:00:00Z')"
            )
        )
    legacy.engine.dispose()

    upgraded = Database(settings)
    applied = initialize_database(upgraded)
    assert applied == ["0002"]
    with upgraded.session() as session:
        connection = session.connection()
        columns = {col["name"] for col in sa_inspect(connection).get_columns("gear_loans")}
        assert "reservation_item_id" in columns
        indexes = {idx["name"] for idx in sa_inspect(connection).get_indexes("gear_loans")}
        assert "ix_gear_loans_reservation_item_id" in indexes
    # Upgrade is idempotent.
    upgraded.engine.dispose()
    again = Database(settings)
    assert initialize_database(again) == []
    again.engine.dispose()


def test_concurrent_run_write_preserves_all_rows(database: Database) -> None:
    def write(index: int) -> int:
        return database.run_write(lambda session: _insert_concurrent_user(session, index))

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(write, range(12)))
    assert len(set(ids)) == 12
    with database.session() as session:
        assert session.query(User).filter(User.email.like("thread-%")).count() == 12


def _insert_concurrent_user(session, index: int) -> int:
    user = User(email=f"thread-{index}@example.com", display_name=f"Thread {index}")
    session.add(user)
    session.flush()
    return user.id
