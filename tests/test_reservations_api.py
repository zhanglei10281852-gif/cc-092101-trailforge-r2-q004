from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from tests.conftest import create_expedition, create_route, create_user
from trailforge.database.migrations import initialize_database
from trailforge.database.session import Database
from trailforge.models.gear import GearInventory, GearReservation
from trailforge.schemas.gear import (
    GearCatalogCreate,
    GearInventoryCreate,
    GearRequirementCreate,
)
from trailforge.services.gear import GearService

UTC = UTC


def _setup_reserved_activity(session) -> tuple[int, int, int, int]:
    organizer = create_user(session)
    route = create_route(session, actor_id=organizer)
    expedition = create_expedition(session, organizer_id=organizer, route_id=route)
    gear = GearService(session)
    catalog = gear.create_catalog(
        GearCatalogCreate(sku="API-STOVE", name="Stove", category="cooking"),
        actor_id=organizer,
    )
    inventory = gear.create_inventory(
        GearInventoryCreate(
            catalog_id=catalog.id,
            ownership="club",
            quantity_total=3,
            actor_id=organizer,
            idempotency_key="api-stove-inv",
        )
    )
    gear.add_requirement(
        expedition,
        GearRequirementCreate(catalog_id=catalog.id, quantity_for_group=2),
        actor_id=organizer,
    )
    return organizer, expedition, catalog.id, inventory.id


def test_reservation_api_full_lifecycle(client) -> None:
    # Seed domain data directly through services.
    db = client.app.state.database
    with db.session() as session:
        organizer, expedition, _, inventory_id = _setup_reserved_activity(session)

    plan = client.get(f"/api/v1/gear/expeditions/{expedition}/reservations/plan")
    assert plan.status_code == 200
    assert plan.json()["items"][0]["suggested_quantity"] == 2

    draft_body = {
        "actor_id": organizer,
        "idempotency_key": "api-draft-key-1",
        "items": [{"inventory_id": inventory_id, "quantity": 2}],
    }
    draft = client.put(
        f"/api/v1/gear/expeditions/{expedition}/reservations/draft",
        json=draft_body,
    )
    assert draft.status_code == 200, draft.text
    reservation_id = draft.json()["id"]
    item_id = draft.json()["items"][0]["id"]

    # Repeated draft request with the same idempotency key returns the same resource.
    replay = client.put(
        f"/api/v1/gear/expeditions/{expedition}/reservations/draft",
        json=draft_body,
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == reservation_id

    confirm_body = {"actor_id": organizer, "idempotency_key": "api-confirm-key-1"}
    confirmed = client.post(
        f"/api/v1/gear/reservations/{reservation_id}/confirm",
        json=confirm_body,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    confirm_replay = client.post(
        f"/api/v1/gear/reservations/{reservation_id}/confirm",
        json=confirm_body,
    )
    assert confirm_replay.status_code == 200
    assert confirm_replay.json()["version"] == confirmed.json()["version"]

    # Missing report now accounts for the club reservation.
    missing = client.get(f"/api/v1/gear/expeditions/{expedition}/missing")
    assert missing.status_code == 200
    assert missing.json()["reserved_units"] == 2
    assert missing.json()["is_ready"] is True

    due = datetime.now(UTC) + timedelta(days=2)
    checkout_body = {
        "actor_id": organizer,
        "idempotency_key": "api-checkout-key-1",
        "loans": [
            {
                "item_id": item_id,
                "borrower_id": organizer,
                "quantity": 1,
                "loaned_at": datetime.now(UTC).isoformat(),
                "due_at": due.isoformat(),
            }
        ],
    }
    checkout = client.post(
        f"/api/v1/gear/reservations/{reservation_id}/checkout",
        json=checkout_body,
    )
    assert checkout.status_code == 201, checkout.text
    assert checkout.json()["reservation"]["status"] == "partially_checked_out"
    assert len(checkout.json()["loans"]) == 1

    cancel = client.post(
        f"/api/v1/gear/reservations/{reservation_id}/cancel",
        json={
            "actor_id": organizer,
            "reason": "Group size reduced",
            "idempotency_key": "api-cancel-key-1",
        },
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "cancelled"

    listing = client.get(
        "/api/v1/gear/reservations",
        params={"expedition_id": expedition, "status": "cancelled"},
    )
    assert listing.status_code == 200
    assert listing.json()["meta"]["total"] == 1


def test_api_rejects_damaged_allocation_and_insufficient_batch(client) -> None:
    db = client.app.state.database
    with db.session() as session:
        organizer = create_user(session, email="dam@example.com", name="Dam")
        route = create_route(session, actor_id=organizer, name="Dam Ridge")
        expedition = create_expedition(session, organizer_id=organizer, route_id=route)
        gear = GearService(session)
        catalog = gear.create_catalog(
            GearCatalogCreate(sku="DAM-RET-1", name="Worn harness", category="safety"),
            actor_id=organizer,
        )
        damaged = gear.create_inventory(
            GearInventoryCreate(
                catalog_id=catalog.id,
                ownership="club",
                quantity_total=2,
                condition="retired",
                actor_id=organizer,
                idempotency_key="damaged-inv-1",
            )
        )

    response = client.put(
        f"/api/v1/gear/expeditions/{expedition}/reservations/draft",
        json={
            "actor_id": organizer,
            "idempotency_key": "damaged-draft-key",
            "items": [{"inventory_id": damaged.id, "quantity": 1}],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "inventory_error"


def test_confirmed_reservation_survives_restart_and_expires(settings) -> None:
    first = Database(settings)
    initialize_database(first)
    with first.session() as session:
        organizer, expedition, _, inventory_id = _setup_reserved_activity(session)
    first.engine.dispose()

    # Draft and confirm after a full engine restart against the same file.
    second = Database(settings)
    initialize_database(second)
    expires_at = datetime.now(UTC) + timedelta(seconds=30)
    with second.session() as session:
        from trailforge.schemas.gear import (
            ReservationConfirmRequest,
            ReservationDraftRequest,
            ReservationItemAllocation,
        )
        from trailforge.services.reservations import ReservationService

        service = ReservationService(session)
        draft = service.upsert_draft(
            expedition,
            ReservationDraftRequest(
                actor_id=organizer,
                idempotency_key="restart-draft-key",
                expires_at=expires_at,
                items=[ReservationItemAllocation(inventory_id=inventory_id, quantity=2)],
            ),
        )
        service.confirm(
            draft.id,
            ReservationConfirmRequest(actor_id=organizer, idempotency_key="restart-confirm"),
        )
        reservation_id = draft.id
    second.engine.dispose()

    # Frozen stock and the confirmed state must survive another restart; the
    # expiry job then recovers the stock based purely on persisted state.
    third = Database(settings)
    initialize_database(third)
    with third.session() as session:
        from trailforge.services.reservations import ReservationService

        assert session.get(GearInventory, inventory_id).quantity_available == 1
        reservation = session.get(GearReservation, reservation_id)
        assert reservation.status == "confirmed"
        result = ReservationService(session).expire_due(
            now=expires_at + timedelta(seconds=1)
        )
        assert result.expired_reservation_ids == [reservation_id]
        assert session.get(GearInventory, inventory_id).quantity_available == 3
    third.engine.dispose()


def test_migration_0002_applies_on_pre_reservation_database(settings) -> None:
    database = Database(settings)
    initialize_database(database)
    # Simulate a 0001-era database: reservation tables absent and gear_loans
    # rebuilt without the reservation link column.
    column_fragment = ", \n\treservation_item_id INTEGER"
    fk_fragment = (
        ", \n\tCONSTRAINT fk_gear_loans_reservation_item_id_gear_reservation_items "
        "FOREIGN KEY(reservation_item_id) REFERENCES gear_reservation_items (id) "
        "ON DELETE SET NULL"
    )
    with database.engine.begin() as connection:
        create_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='gear_loans'"
        ).scalar_one()
        assert column_fragment in create_sql
        rebuilt_sql = create_sql.replace(column_fragment, "").replace(fk_fragment, "")
        connection.execute(text("DROP TABLE IF EXISTS gear_reservation_items"))
        connection.execute(text("DROP TABLE IF EXISTS gear_reservations"))
        connection.execute(text("DROP TABLE gear_loans"))
        connection.execute(text(rebuilt_sql))
        connection.execute(text("DROP INDEX IF EXISTS ix_gear_loans_reservation_item_id"))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = '0002'"))
    with database.engine.connect() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(gear_loans)")}
        assert "reservation_item_id" not in columns

    applied = initialize_database(database)
    assert applied == ["0002"]
    with database.engine.connect() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(gear_loans)")}
        assert "reservation_item_id" in columns
        table_names = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).all()
        }
        assert {"gear_reservations", "gear_reservation_items"} <= table_names
    # Re-running the migration is harmless.
    assert initialize_database(database) == []
    database.engine.dispose()
