from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from tests.conftest import create_expedition, create_route, create_user
from trailforge.database.base import utc_now
from trailforge.domain.enums import (
    GearCondition,
    InventoryMovementType,
    LoanStatus,
    ReservationStatus,
)
from trailforge.errors import (
    IdempotencyConflictError,
    InvalidStateError,
    InventoryError,
)
from trailforge.models.audit import AuditLog
from trailforge.models.gear import (
    GearInventory,
    GearLoan,
    GearReservation,
    InventoryMovement,
)
from trailforge.schemas.activities import ActivityStateChange, RegistrationCreate
from trailforge.schemas.gear import (
    GearCatalogCreate,
    GearCheckUpsert,
    GearInventoryCreate,
    GearLoanReturn,
    GearRequirementCreate,
    ReservationCancelRequest,
    ReservationCheckoutItem,
    ReservationCheckoutRequest,
    ReservationConfirmRequest,
    ReservationDraftRequest,
    ReservationItemAllocation,
)
from trailforge.services.activities import ExpeditionService
from trailforge.services.gear import GearService
from trailforge.services.reservations import ReservationService

UTC = UTC


def _catalog_and_club_inventory(
    session,
    *,
    sku: str = "ROPE-60",
    quantity: int = 4,
    condition: str = "good",
    name: str = "Dynamic rope",
) -> tuple[int, int, int]:
    owner = create_user(session, email=f"{sku.lower()}@example.com", name=f"Owner {sku}")
    gear = GearService(session)
    catalog = gear.create_catalog(
        GearCatalogCreate(sku=sku, name=name, category="climbing"),
        actor_id=owner,
    )
    inventory = gear.create_inventory(
        GearInventoryCreate(
            catalog_id=catalog.id,
            ownership="club",
            quantity_total=quantity,
            condition=condition,
            actor_id=owner,
            idempotency_key=f"inv-{sku.lower()}",
        )
    )
    return owner, catalog.id, inventory.id


def _expedition(session) -> tuple[int, int, int]:
    organizer = create_user(session)
    route = create_route(session, actor_id=organizer)
    expedition = create_expedition(session, organizer_id=organizer, route_id=route)
    return organizer, route, expedition


def _draft(
    session,
    expedition_id: int,
    allocations: list[tuple[int, int]],
    *,
    actor_id: int,
    key: str = "draft-key-0001",
    draft_key: str = "main",
    expires_at: datetime | None = None,
):
    return ReservationService(session).upsert_draft(
        expedition_id,
        ReservationDraftRequest(
            actor_id=actor_id,
            idempotency_key=key,
            draft_key=draft_key,
            expires_at=expires_at,
            items=[
                ReservationItemAllocation(inventory_id=inventory_id, quantity=quantity)
                for inventory_id, quantity in allocations
            ],
        ),
    )


def _confirm(session, reservation_id: int, actor_id: int, *, key: str = "confirm-key-1"):
    return ReservationService(session).confirm(
        reservation_id,
        ReservationConfirmRequest(actor_id=actor_id, idempotency_key=key),
    )


# --------------------------------------------------------------------- planning


def test_draft_plan_only_covers_group_requirements(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, rope_catalog, _ = _catalog_and_club_inventory(session, sku="ROPE-1", quantity=5)
    _, helmet_catalog, _ = _catalog_and_club_inventory(
        session, sku="HELMET-1", quantity=2, name="Helmet"
    )
    gear = GearService(session)
    gear.add_requirement(
        expedition,
        GearRequirementCreate(catalog_id=rope_catalog, quantity_for_group=2),
        actor_id=organizer,
    )
    gear.add_requirement(
        expedition,
        GearRequirementCreate(catalog_id=helmet_catalog, quantity_per_person=1),
        actor_id=organizer,
    )
    plan = ReservationService(session).draft_plan(expedition)
    assert plan.participant_count == 1
    assert len(plan.items) == 1
    suggestion = plan.items[0]
    assert suggestion.catalog_id == rope_catalog
    assert suggestion.group_required == 2
    assert suggestion.club_available_quantity == 5
    assert suggestion.suggested_quantity == 2
    assert suggestion.options[0].allocatable is True


def test_draft_plan_deducts_existing_open_reservation(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, catalog, inventory_id = _catalog_and_club_inventory(session, sku="ROPE-2", quantity=5)
    gear = GearService(session)
    gear.add_requirement(
        expedition,
        GearRequirementCreate(catalog_id=catalog, quantity_for_group=4),
        actor_id=organizer,
    )
    draft = _draft(session, expedition, [(inventory_id, 3)], actor_id=organizer)
    _confirm(session, draft.id, organizer)
    plan = ReservationService(session).draft_plan(expedition)
    assert plan.items[0].club_reserved_quantity == 3
    assert plan.items[0].suggested_quantity == 1


# ----------------------------------------------------------------- confirm/freeze


def test_batch_confirm_freezes_multiple_inventories_atomically(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, _, rope_inv = _catalog_and_club_inventory(session, sku="ROPE-3", quantity=3)
    _, _, tent_inv = _catalog_and_club_inventory(session, sku="TENT-3", quantity=2)
    draft = _draft(
        session, expedition, [(rope_inv, 2), (tent_inv, 1)], actor_id=organizer
    )
    confirmed = _confirm(session, draft.id, organizer)
    assert confirmed.status == ReservationStatus.CONFIRMED
    assert session.get(GearInventory, rope_inv).quantity_available == 1
    assert session.get(GearInventory, tent_inv).quantity_available == 1
    movements = list(
        session.scalars(
            select(InventoryMovement).where(
                InventoryMovement.reference_type == "gear_reservation",
                InventoryMovement.reference_id == confirmed.id,
            )
        )
    )
    assert sorted(m.quantity_delta for m in movements) == [-2, -1]
    assert all(m.movement_type == InventoryMovementType.RESERVED for m in movements)


def test_batch_confirm_is_all_or_nothing(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, _, rope_inv = _catalog_and_club_inventory(session, sku="ROPE-4", quantity=1)
    _, _, tent_inv = _catalog_and_club_inventory(session, sku="TENT-4", quantity=5)
    draft = _draft(
        session, expedition, [(rope_inv, 2), (tent_inv, 1)], actor_id=organizer
    )
    with pytest.raises(InventoryError, match="insufficient"):
        _confirm(session, draft.id, organizer)
    assert session.get(GearInventory, rope_inv).quantity_available == 1
    assert session.get(GearInventory, tent_inv).quantity_available == 5
    assert (
        session.scalar(
            select(func.count())
            .select_from(InventoryMovement)
            .where(InventoryMovement.movement_type == InventoryMovementType.RESERVED)
        )
        == 0
    )
    reservation = session.get(GearReservation, draft.id)
    assert reservation.status == ReservationStatus.DRAFT


def test_personal_damaged_or_retired_inventory_cannot_be_allocated(session) -> None:
    organizer, _, expedition = _expedition(session)
    gear = GearService(session)
    catalog = gear.create_catalog(
        GearCatalogCreate(sku="BIKE-1", name="Bike", category="bike"),
        actor_id=organizer,
    )
    rider = create_user(session, email="rider@example.com", name="Rider")
    personal = gear.create_inventory(
        GearInventoryCreate(
            catalog_id=catalog.id,
            owner_id=rider,
            ownership="personal",
            quantity_total=1,
            actor_id=organizer,
            idempotency_key="personal-bike-01",
        )
    )
    with pytest.raises(InventoryError, match="personal inventory"):
        _draft(session, expedition, [(personal.id, 1)], actor_id=organizer)
    damaged = gear.create_inventory(
        GearInventoryCreate(
            catalog_id=catalog.id,
            ownership="club",
            quantity_total=2,
            condition=GearCondition.DAMAGED,
            actor_id=organizer,
            idempotency_key="damaged-bike-01",
        )
    )
    with pytest.raises(InventoryError, match="damaged or retired"):
        _draft(
            session,
            expedition,
            [(damaged.id, 1)],
            actor_id=organizer,
            key="draft-damaged-01",
        )


# ------------------------------------------------------------------ idempotency


def test_draft_and_confirm_are_idempotent(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, _, inventory_id = _catalog_and_club_inventory(session, sku="ROPE-5", quantity=4)
    request = ReservationDraftRequest(
        actor_id=organizer,
        idempotency_key="idempotent-draft",
        items=[ReservationItemAllocation(inventory_id=inventory_id, quantity=2)],
    )
    service = ReservationService(session)
    first = service.upsert_draft(expedition, request)
    second = service.upsert_draft(expedition, request)
    assert first.id == second.id
    confirm_request = ReservationConfirmRequest(
        actor_id=organizer, idempotency_key="idempotent-confirm"
    )
    confirmed = service.confirm(first.id, confirm_request)
    replayed = service.confirm(first.id, confirm_request)
    assert confirmed.version == replayed.version
    assert session.get(GearInventory, inventory_id).quantity_available == 2
    assert (
        session.scalar(
            select(func.count())
            .select_from(InventoryMovement)
            .where(InventoryMovement.movement_type == InventoryMovementType.RESERVED)
        )
        == 1
    )


def test_same_idempotency_key_with_changed_body_conflicts(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, _, inventory_id = _catalog_and_club_inventory(session, sku="ROPE-6", quantity=4)
    service = ReservationService(session)
    service.upsert_draft(
        expedition,
        ReservationDraftRequest(
            actor_id=organizer,
            idempotency_key="shared-draft-key",
            items=[ReservationItemAllocation(inventory_id=inventory_id, quantity=1)],
        ),
    )
    with pytest.raises(IdempotencyConflictError):
        service.upsert_draft(
            expedition,
            ReservationDraftRequest(
                actor_id=organizer,
                idempotency_key="shared-draft-key",
                items=[ReservationItemAllocation(inventory_id=inventory_id, quantity=2)],
            ),
        )


# --------------------------------------------------------------------- checkout


def test_partial_checkout_converts_reservation_to_loans(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, _, inventory_id = _catalog_and_club_inventory(session, sku="ROPE-7", quantity=4)
    borrower = create_user(session, email="climber@example.com", name="Climber")
    draft = _draft(session, expedition, [(inventory_id, 3)], actor_id=organizer)
    confirmed = _confirm(session, draft.id, organizer)
    assert session.get(GearInventory, inventory_id).quantity_available == 1
    now = datetime.now(UTC)
    reservation, loans = ReservationService(session).checkout(
        confirmed.id,
        ReservationCheckoutRequest(
            actor_id=organizer,
            idempotency_key="checkout-key-1",
            loans=[
                ReservationCheckoutItem(
                    item_id=confirmed.items[0].id,
                    borrower_id=borrower,
                    quantity=2,
                    loaned_at=now,
                    due_at=now + timedelta(days=2),
                )
            ],
        ),
    )
    assert reservation.status == ReservationStatus.PARTIALLY_CHECKED_OUT
    assert len(loans) == 1
    loan = loans[0]
    assert loan.quantity == 2
    assert loan.status == LoanStatus.ACTIVE
    assert loan.expedition_id == expedition
    # Frozen units are released then loaned out inside one transaction.
    inventory = session.get(GearInventory, inventory_id)
    assert inventory.quantity_available == 1
    deltas = [
        (m.movement_type, m.quantity_delta)
        for m in session.scalars(
            select(InventoryMovement)
            .where(InventoryMovement.inventory_id == inventory_id)
            .order_by(InventoryMovement.id)
        )
    ]
    assert (InventoryMovementType.RESERVED, -3) in deltas
    assert (InventoryMovementType.RESERVATION_RELEASED, 2) in deltas
    assert (InventoryMovementType.LOAN_OUT, -2) in deltas
    stored_loan = session.scalar(select(GearLoan).where(GearLoan.id == loan.id))
    assert stored_loan.reservation_item_id == confirmed.items[0].id


def test_full_checkout_closes_reservation_and_return_works(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, _, inventory_id = _catalog_and_club_inventory(session, sku="ROPE-8", quantity=2)
    borrower = create_user(session, email="finisher@example.com", name="Finisher")
    draft = _draft(session, expedition, [(inventory_id, 2)], actor_id=organizer)
    confirmed = _confirm(session, draft.id, organizer)
    now = datetime.now(UTC)
    reservation, loans = ReservationService(session).checkout(
        confirmed.id,
        ReservationCheckoutRequest(
            actor_id=organizer,
            idempotency_key="checkout-full-1",
            loans=[
                ReservationCheckoutItem(
                    item_id=confirmed.items[0].id,
                    borrower_id=borrower,
                    quantity=2,
                    loaned_at=now,
                    due_at=now + timedelta(days=1),
                )
            ],
        ),
    )
    assert reservation.status == ReservationStatus.CHECKED_OUT
    assert reservation.items[0].outstanding_quantity == 0
    GearService(session).return_loan(
        loans[0].id,
        GearLoanReturn(
            quantity=2,
            returned_at=now + timedelta(hours=4),
            condition_in="good",
            actor_id=organizer,
            idempotency_key="return-after-reservation",
        ),
    )
    assert session.get(GearInventory, inventory_id).quantity_available == 2


def test_checkout_cannot_exceed_outstanding_reservation(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, _, inventory_id = _catalog_and_club_inventory(session, sku="ROPE-9", quantity=3)
    borrower = create_user(session, email="over@example.com", name="Over")
    draft = _draft(session, expedition, [(inventory_id, 1)], actor_id=organizer)
    confirmed = _confirm(session, draft.id, organizer)
    now = datetime.now(UTC)
    with pytest.raises(InventoryError, match="exceeds outstanding"):
        ReservationService(session).checkout(
            confirmed.id,
            ReservationCheckoutRequest(
                actor_id=organizer,
                idempotency_key="checkout-over-1",
                loans=[
                    ReservationCheckoutItem(
                        item_id=confirmed.items[0].id,
                        borrower_id=borrower,
                        quantity=2,
                        loaned_at=now,
                        due_at=now + timedelta(days=1),
                    )
                ],
            ),
        )
    assert session.get(GearInventory, inventory_id).quantity_available == 2


# --------------------------------------------------------------- cancel/expiry


def test_cancel_confirmed_reservation_releases_frozen_stock(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, _, rope_inv = _catalog_and_club_inventory(session, sku="ROPE-A", quantity=3)
    _, _, tent_inv = _catalog_and_club_inventory(session, sku="TENT-A", quantity=2)
    draft = _draft(
        session, expedition, [(rope_inv, 2), (tent_inv, 1)], actor_id=organizer
    )
    confirmed = _confirm(session, draft.id, organizer)
    cancelled = ReservationService(session).cancel(
        confirmed.id,
        ReservationCancelRequest(
            actor_id=organizer,
            reason="Event postponed",
            idempotency_key="cancel-key-1",
        ),
    )
    assert cancelled.status == ReservationStatus.CANCELLED
    assert session.get(GearInventory, rope_inv).quantity_available == 3
    assert session.get(GearInventory, tent_inv).quantity_available == 2
    release_count = session.scalar(
        select(func.count())
        .select_from(InventoryMovement)
        .where(
            InventoryMovement.movement_type == InventoryMovementType.RESERVATION_RELEASED
        )
    )
    assert release_count == 2


def test_cancel_after_partial_checkout_releases_only_remaining(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, _, inventory_id = _catalog_and_club_inventory(session, sku="ROPE-B", quantity=4)
    borrower = create_user(session, email="partial@example.com", name="Partial")
    draft = _draft(session, expedition, [(inventory_id, 3)], actor_id=organizer)
    confirmed = _confirm(session, draft.id, organizer)
    now = datetime.now(UTC)
    service = ReservationService(session)
    service.checkout(
        confirmed.id,
        ReservationCheckoutRequest(
            actor_id=organizer,
            idempotency_key="partial-checkout-1",
            loans=[
                ReservationCheckoutItem(
                    item_id=confirmed.items[0].id,
                    borrower_id=borrower,
                    quantity=2,
                    loaned_at=now,
                    due_at=now + timedelta(days=1),
                )
            ],
        ),
    )
    cancelled = service.cancel(
        confirmed.id,
        ReservationCancelRequest(
            actor_id=organizer,
            reason="Trip trimmed",
            idempotency_key="partial-cancel-1",
        ),
    )
    assert cancelled.status == ReservationStatus.CANCELLED
    # 4 total - 2 still on loan = 2 available
    assert session.get(GearInventory, inventory_id).quantity_available == 2


def test_expiry_job_releases_overdue_reservations(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, _, inventory_id = _catalog_and_club_inventory(session, sku="ROPE-C", quantity=2)
    deadline = utc_now() + timedelta(hours=1)
    draft = _draft(
        session,
        expedition,
        [(inventory_id, 2)],
        actor_id=organizer,
        expires_at=deadline,
    )
    confirmed = _confirm(session, draft.id, organizer)
    result = ReservationService(session).expire_due(now=deadline + timedelta(minutes=1))
    assert result.expired_reservation_ids == [confirmed.id]
    assert result.released_units == 2
    assert session.get(GearInventory, inventory_id).quantity_available == 2
    refreshed = session.get(GearReservation, confirmed.id)
    assert refreshed.status == ReservationStatus.EXPIRED
    # Running the job again is a no-op (restart-safe).
    again = ReservationService(session).expire_due(now=deadline + timedelta(minutes=2))
    assert again.expired_reservation_ids == []


def test_cancelling_expedition_cascades_reservation_release(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, _, inventory_id = _catalog_and_club_inventory(session, sku="ROPE-D", quantity=2)
    draft = _draft(session, expedition, [(inventory_id, 1)], actor_id=organizer)
    _confirm(session, draft.id, organizer)
    ExpeditionService(session).change_status(
        expedition,
        ActivityStateChange(
            target_status="cancelled",
            reason="Weather window lost",
            actor_id=organizer,
        ),
    )
    assert session.get(GearInventory, inventory_id).quantity_available == 2
    reservation = session.get(GearReservation, draft.id)
    assert reservation.status == ReservationStatus.CANCELLED


def test_cancelling_expedition_closes_unconfirmed_draft(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, _, inventory_id = _catalog_and_club_inventory(session, sku="ROPE-I", quantity=2)
    draft = _draft(
        session, expedition, [(inventory_id, 1)], actor_id=organizer, key="draft-i-key"
    )
    ExpeditionService(session).change_status(
        expedition,
        ActivityStateChange(
            target_status="cancelled",
            reason="Never confirmed",
            actor_id=organizer,
        ),
    )
    # No stock was ever frozen, so availability is untouched and the draft closes.
    assert session.get(GearInventory, inventory_id).quantity_available == 2
    assert session.get(GearReservation, draft.id).status == ReservationStatus.CANCELLED


# --------------------------------------------------------------- delta/report


def test_requirement_delta_does_not_touch_confirmed_reservation(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, catalog, inventory_id = _catalog_and_club_inventory(session, sku="ROPE-E", quantity=6)
    gear = GearService(session)
    gear.add_requirement(
        expedition,
        GearRequirementCreate(catalog_id=catalog, quantity_for_group=2, quantity_per_person=1),
        actor_id=organizer,
    )
    draft = _draft(session, expedition, [(inventory_id, 2)], actor_id=organizer)
    confirmed = _confirm(session, draft.id, organizer)
    version_before = session.get(GearReservation, confirmed.id).version
    # A second member registers and group need grows: personal demand changes,
    # the group gap widens, but the confirmed reservation stays untouched.
    second = create_user(session, email="second@example.com", name="Second")
    ExpeditionService(session).change_status(
        expedition,
        ActivityStateChange(target_status="open", actor_id=organizer),
    )
    ExpeditionService(session).register(
        expedition,
        RegistrationCreate(user_id=second, idempotency_key="second-member-reg"),
    )
    gear.add_requirement(
        expedition,
        GearRequirementCreate(catalog_id=catalog, quantity_for_group=4, quantity_per_person=1),
        actor_id=organizer,
    )
    delta = ReservationService(session).requirement_delta(
        expedition, participant_count_before=1
    )
    item = next(row for row in delta.items if row.catalog_id == catalog)
    assert item.group_required == 4
    assert item.confirmed_reserved_quantity == 2
    assert item.group_delta == 2
    assert item.personal_required_before == 1
    assert item.personal_required_after == 2
    # Confirmed reservation is untouched.
    assert session.get(GearReservation, confirmed.id).version == version_before
    assert session.get(GearInventory, inventory_id).quantity_available == 4


def test_missing_report_separates_club_reservation_and_personal_packing(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, rope_catalog, rope_inv = _catalog_and_club_inventory(
        session, sku="ROPE-F", quantity=3, name="Rope F"
    )
    _, helmet_catalog, _ = _catalog_and_club_inventory(
        session, sku="HELM-F", quantity=1, name="Helmet F"
    )
    gear = GearService(session)
    gear.add_requirement(
        expedition,
        GearRequirementCreate(catalog_id=rope_catalog, quantity_for_group=2),
        actor_id=organizer,
    )
    gear.add_requirement(
        expedition,
        GearRequirementCreate(catalog_id=helmet_catalog, quantity_per_person=1),
        actor_id=organizer,
    )
    draft = _draft(session, expedition, [(rope_inv, 2)], actor_id=organizer)
    _confirm(session, draft.id, organizer)
    report = gear.missing_report(expedition)
    assert report.reserved_units == 2
    assert report.is_ready is False
    helmet_row = next(item for item in report.missing_items if item.catalog_id == helmet_catalog)
    assert helmet_row.personal_missing_quantity == 1
    assert helmet_row.group_missing_quantity == 0
    rope_rows = [item for item in report.missing_items if item.catalog_id == rope_catalog]
    assert rope_rows == []
    # After packing the personal helmet the report is ready.
    gear.upsert_check(
        expedition,
        GearCheckUpsert(
            user_id=organizer,
            catalog_id=helmet_catalog,
            quantity=1,
            status="verified",
            verified_by=organizer,
        ),
    )
    assert gear.missing_report(expedition).is_ready is True


# ----------------------------------------------------------------- concurrency


def test_concurrent_confirms_never_drive_inventory_negative(database) -> None:
    with database.session() as session:
        organizer, catalog_id, inventory_id = _catalog_and_club_inventory(
            session, sku="ROPE-X", quantity=1
        )
        _, _, expedition_a = _expedition(session)
        organizer_b = create_user(
            session, email="organizer-b@example.com", name="Organizer B"
        )
        route_b = create_route(session, actor_id=organizer_b, name="Race Ridge B")
        expedition_b = create_expedition(
            session, organizer_id=organizer_b, route_id=route_b
        )
        GearService(session).add_requirement(
            expedition_a,
            GearRequirementCreate(catalog_id=catalog_id, quantity_for_group=1),
            actor_id=organizer,
        )
        GearService(session).add_requirement(
            expedition_b,
            GearRequirementCreate(catalog_id=catalog_id, quantity_for_group=1),
            actor_id=organizer_b,
        )
        draft_a = _draft(
            session,
            expedition_a,
            [(inventory_id, 1)],
            actor_id=organizer,
            key="draft-a-key",
        )
        draft_b = _draft(
            session,
            expedition_b,
            [(inventory_id, 1)],
            actor_id=organizer_b,
            key="draft-b-key",
        )
        draft_a_id, draft_b_id = draft_a.id, draft_b.id

    def attempt(args: tuple[int, int, str]) -> str:
        reservation_id, actor_id, key = args

        def operation(session) -> str:
            try:
                _confirm(session, reservation_id, actor_id, key=key)
            except InventoryError:
                return "lost"
            return "won"

        return database.run_write(operation)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                attempt,
                [
                    (draft_a_id, organizer, "race-confirm-a"),
                    (draft_b_id, organizer_b, "race-confirm-b"),
                ],
            )
        )
    assert sorted(outcomes) == ["lost", "won"]
    with database.session() as session:
        assert session.get(GearInventory, inventory_id).quantity_available == 0
        statuses = {
            session.get(GearReservation, draft_a_id).status,
            session.get(GearReservation, draft_b_id).status,
        }
        assert ReservationStatus.CONFIRMED in statuses
        assert ReservationStatus.DRAFT in statuses


def test_concurrent_checkout_and_cancel_do_not_double_release(database) -> None:
    with database.session() as session:
        organizer, _, inventory_id = _catalog_and_club_inventory(
            session, sku="ROPE-Y", quantity=1
        )
        _, _, expedition = _expedition(session)
        draft = _draft(
            session, expedition, [(inventory_id, 1)], actor_id=organizer, key="yc-draft"
        )
        _confirm(session, draft.id, organizer, key="yc-confirm")
        reservation_id, item_id = draft.id, draft.items[0].id

    now = datetime.now(UTC)

    def checkout_operation(session) -> str:
        try:
            ReservationService(session).checkout(
                reservation_id,
                ReservationCheckoutRequest(
                    actor_id=organizer,
                    idempotency_key="yc-checkout",
                    loans=[
                        ReservationCheckoutItem(
                            item_id=item_id,
                            borrower_id=organizer,
                            quantity=1,
                            loaned_at=now,
                            due_at=now + timedelta(days=1),
                        )
                    ],
                ),
            )
        except (InventoryError, InvalidStateError):
            return "checkout-lost"
        return "checkout-won"

    def cancel_operation(session) -> str:
        try:
            ReservationService(session).cancel(
                reservation_id,
                ReservationCancelRequest(
                    actor_id=organizer,
                    reason="Concurrent cancellation",
                    idempotency_key="yc-cancel",
                ),
            )
        except (InventoryError, InvalidStateError):
            return "cancel-lost"
        return "cancel-won"

    with ThreadPoolExecutor(max_workers=2) as pool:
        checkout_future = pool.submit(lambda: database.run_write(checkout_operation))
        cancel_future = pool.submit(lambda: database.run_write(cancel_operation))
        outcomes = {checkout_future.result(), cancel_future.result()}
        assert outcomes <= {
            "checkout-won",
            "checkout-lost",
            "cancel-won",
            "cancel-lost",
        }

    with database.session() as session:
        # Whatever wins, available stock must never exceed the physical total.
        inventory = session.get(GearInventory, inventory_id)
        assert inventory.quantity_available in {0, 1}
        assert inventory.quantity_available <= inventory.quantity_total
        # The unit is either loaned out or released, never both.
        loans = list(
            session.scalars(select(GearLoan).where(GearLoan.inventory_id == inventory_id))
        )
        active_loans = [loan for loan in loans if loan.status == LoanStatus.ACTIVE]
        reservation = session.get(GearReservation, reservation_id)
        if active_loans:
            assert inventory.quantity_available == 0
            assert len(active_loans) == 1
        else:
            assert reservation.status == ReservationStatus.CANCELLED
            assert inventory.quantity_available == 1


# --------------------------------------------------------------- audit linkage


def test_reservation_lifecycle_writes_linked_audit_logs(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, _, inventory_id = _catalog_and_club_inventory(session, sku="ROPE-G", quantity=2)
    draft = _draft(
        session, expedition, [(inventory_id, 1)], actor_id=organizer, key="audit-draft"
    )
    _confirm(session, draft.id, organizer, key="audit-confirm")
    ReservationService(session).cancel(
        draft.id,
        ReservationCancelRequest(
            actor_id=organizer,
            reason="Audit trail check",
            idempotency_key="audit-cancel",
        ),
    )
    actions = {
        str(row.action)
        for row in session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == "gear_reservation",
                AuditLog.entity_id == draft.id,
            )
        )
    }
    assert {
        "reservation_drafted",
        "reservation_confirmed",
        "reservation_cancelled",
    }.issubset(actions)


def test_draft_cancel_does_not_invent_stock_movements(session) -> None:
    organizer, _, expedition = _expedition(session)
    _, _, inventory_id = _catalog_and_club_inventory(session, sku="ROPE-H", quantity=2)
    draft = _draft(
        session, expedition, [(inventory_id, 1)], actor_id=organizer, key="never-confirmed"
    )
    cancelled = ReservationService(session).cancel(
        draft.id,
        ReservationCancelRequest(
            actor_id=organizer,
            reason="Changed plans before confirmation",
            idempotency_key="cancel-draft-1",
        ),
    )
    assert cancelled.status == ReservationStatus.CANCELLED
    assert session.get(GearInventory, inventory_id).quantity_available == 2
    assert (
        session.scalar(
            select(func.count())
            .select_from(InventoryMovement)
            .where(
                InventoryMovement.movement_type == InventoryMovementType.RESERVATION_RELEASED
            )
        )
        == 0
    )
    with pytest.raises(InvalidStateError):
        ReservationService(session).cancel(
            draft.id,
            ReservationCancelRequest(
                actor_id=organizer,
                reason="Again",
                idempotency_key="cancel-draft-2",
            ),
        )
