from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from tests.conftest import create_expedition, create_route, create_user
from trailforge.database.migrations import initialize_database
from trailforge.database.session import Database
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
    NotFoundError,
)
from trailforge.models.audit import AuditLog
from trailforge.models.gear import GearInventory, InventoryMovement
from trailforge.schemas.gear import (
    GearCatalogCreate,
    GearCheckUpsert,
    GearInventoryCreate,
    GearLoanCreate,
    GearRequirementCreate,
    ReservationAllocationLine,
    ReservationCancelRequest,
    ReservationConfirmRequest,
    ReservationDraftCreate,
    ReservationFulfilmentLine,
    ReservationFulfilRequest,
)
from trailforge.services.gear import GearService
from trailforge.services.reservations import ReservationService

UTC = UTC


def _catalog(session, service: GearService, *, sku: str = "ROPE-60M", name: str = "Climbing rope"):
    return service.create_catalog(
        GearCatalogCreate(sku=sku, name=name, category="climbing", safety_critical=True),
        actor_id=create_user(session, email=f"{sku.lower()}@example.com", name=f"{sku} maker"),
    )


def _club_inventory(
    session,
    service: GearService,
    catalog_id: int,
    *,
    quantity: int = 3,
    actor: int | None = None,
    condition: str = "good",
    key: str = "club-stock",
):
    if actor is None:
        actor = create_user(session, email=f"admin-{key}@example.com", name="Admin")
    return service.create_inventory(
        GearInventoryCreate(
            catalog_id=catalog_id,
            ownership="club",
            quantity_total=quantity,
            condition=condition,
            actor_id=actor,
            idempotency_key=key,
        )
    )


def _expedition_with_group_requirement(
    session, *, catalog_id: int, group: int = 2, per_person: int = 0
) -> tuple[int, int, int]:
    organizer = create_user(session)
    route = create_route(session, actor_id=organizer)
    expedition = create_expedition(session, organizer_id=organizer, route_id=route)
    GearService(session).add_requirement(
        expedition,
        GearRequirementCreate(
            catalog_id=catalog_id,
            quantity_for_group=group,
            quantity_per_person=per_person,
        ),
        actor_id=organizer,
    )
    return organizer, route, expedition


def _draft_reservation(
    session,
    service: ReservationService,
    expedition_id: int,
    lines: list[tuple[int, int, int]],
    *,
    actor: int,
    key: str = "draft-key-0001",
    expires_in_hours: int = 48,
):
    return service.create_draft(
        expedition_id,
        ReservationDraftCreate(
            expires_at=datetime.now(UTC) + timedelta(hours=expires_in_hours),
            items=[
                ReservationAllocationLine(
                    catalog_id=catalog_id,
                    inventory_id=inventory_id,
                    quantity_reserved=qty,
                )
                for catalog_id, inventory_id, qty in lines
            ],
            actor_id=actor,
            idempotency_key=key,
        ),
    )


def _confirm(
    session, service: ReservationService, reservation_id: int, actor: int, *, key="confirm-0001"
):
    return service.confirm(
        reservation_id,
        ReservationConfirmRequest(actor_id=actor, idempotency_key=key),
    )


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------


def test_proposal_splits_group_and_personal_requirements(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service)
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=2, per_person=1
    )
    inventory = _club_inventory(session, service, catalog.id, quantity=1, actor=organizer)
    proposal = ReservationService(session).proposal(expedition)
    sources = {line.requirement_source: line for line in proposal.lines}
    assert set(sources) == {"group", "personal"}
    assert sources["group"].required_quantity == 2
    assert sources["group"].club_available_quantity == 1
    assert sources["group"].suggested_reservation_quantity == 1
    assert sources["group"].missing_quantity == 1
    assert sources["personal"].required_quantity == 1
    assert sources["personal"].suggested_reservation_quantity == 0
    club_option = next(
        option for option in sources["group"].options if option.inventory_id == inventory.id
    )
    assert club_option.allocatable is True


def test_proposal_marks_personal_damaged_and_retired_inventory_unallocatable(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service, sku="HELMET", name="Helmet")
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=1
    )
    club = _club_inventory(
        session, service, catalog.id, quantity=2, actor=organizer, key="club-good"
    )
    member = create_user(session, email="member@example.com", name="Member")
    service.create_inventory(
        GearInventoryCreate(
            catalog_id=catalog.id,
            owner_id=member,
            ownership="personal",
            quantity_total=1,
            actor_id=organizer,
            idempotency_key="personal-helmet",
        )
    )
    # Mark the club row damaged; the proposal must flag it unallocatable.
    session.get(GearInventory, club.id).condition = GearCondition.DAMAGED
    session.flush()
    proposal = ReservationService(session).proposal(expedition)
    line = next(item for item in proposal.lines if item.requirement_source == "group")
    by_id = {option.inventory_id: option for option in line.options}
    assert by_id[club.id].allocatable is False
    assert "damaged" in (by_id[club.id].blocked_reason or "")
    personal_option = next(option for option in line.options if option.ownership == "personal")
    assert personal_option.allocatable is False
    assert personal_option.blocked_reason is not None
    assert line.club_available_quantity == 0


# ---------------------------------------------------------------------------
# Draft + confirm freezing
# ---------------------------------------------------------------------------


def test_confirm_freezes_available_and_records_hold_movement(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service)
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=2
    )
    inventory = _club_inventory(session, service, catalog.id, quantity=3, actor=organizer)
    reservations = ReservationService(session)
    draft = _draft_reservation(
        session,
        reservations,
        expedition,
        [(catalog.id, inventory.id, 2)],
        actor=organizer,
    )
    assert draft.status == ReservationStatus.DRAFT
    # A draft must not freeze anything yet.
    assert session.get(GearInventory, inventory.id).quantity_available == 3
    confirmed = _confirm(session, reservations, draft.id, organizer)
    assert confirmed.status == ReservationStatus.CONFIRMED
    assert confirmed.confirmed_at is not None
    assert session.get(GearInventory, inventory.id).quantity_available == 1
    hold = session.scalar(
        select(InventoryMovement).where(
            InventoryMovement.reference_type == "gear_reservation",
            InventoryMovement.reference_id == draft.id,
            InventoryMovement.movement_type == InventoryMovementType.RESERVATION_HOLD,
        )
    )
    assert hold.quantity_delta == -2
    assert hold.quantity_after == 1
    assert hold.actor_id == organizer


def test_draft_rejects_personal_inventory_and_unallocatable_condition(session) -> None:
    service = GearService(session)
    organizer = create_user(session, email="harness-org@example.com", name="Harness Org")
    route = create_route(session, actor_id=organizer)
    catalog_personal = _catalog(session, service, sku="HARNESS", name="Harness")
    catalog_retired = _catalog(session, service, sku="CRAMPON", name="Crampons")
    expedition_personal = create_expedition(
        session, organizer_id=organizer, route_id=route, offset_days=10
    )
    expedition_retired = create_expedition(
        session, organizer_id=organizer, route_id=route, offset_days=20
    )
    service.add_requirement(
        expedition_personal,
        GearRequirementCreate(catalog_id=catalog_personal.id, quantity_for_group=2),
        actor_id=organizer,
    )
    service.add_requirement(
        expedition_retired,
        GearRequirementCreate(catalog_id=catalog_retired.id, quantity_for_group=1),
        actor_id=organizer,
    )
    member = create_user(session, email="owner@example.com", name="Owner")
    personal = service.create_inventory(
        GearInventoryCreate(
            catalog_id=catalog_personal.id,
            owner_id=member,
            ownership="personal",
            quantity_total=2,
            actor_id=organizer,
            idempotency_key="personal-harness",
        )
    )
    retired = service.create_inventory(
        GearInventoryCreate(
            catalog_id=catalog_retired.id,
            ownership="club",
            quantity_total=1,
            condition="retired",
            actor_id=organizer,
            idempotency_key="retired-crampon",
        )
    )
    reservations = ReservationService(session)
    with pytest.raises(InventoryError, match="personal inventory"):
        _draft_reservation(
            session,
            reservations,
            expedition_personal,
            [(catalog_personal.id, personal.id, 1)],
            actor=organizer,
        )
    with pytest.raises(InventoryError, match="damaged or retired"):
        _draft_reservation(
            session,
            reservations,
            expedition_retired,
            [(catalog_retired.id, retired.id, 1)],
            actor=organizer,
            key="draft-retired-0002",
        )


def test_confirm_is_atomic_all_or_nothing_when_one_line_runs_out(session) -> None:
    service = GearService(session)
    catalog_a = _catalog(session, service, sku="ROPE-A", name="Rope A")
    catalog_b = _catalog(session, service, sku="ROPE-B", name="Rope B")
    organizer = create_user(session)
    route = create_route(session, actor_id=organizer)
    expedition = create_expedition(session, organizer_id=organizer, route_id=route)
    service.add_requirement(
        expedition,
        GearRequirementCreate(catalog_id=catalog_a.id, quantity_for_group=2),
        actor_id=organizer,
    )
    service.add_requirement(
        expedition,
        GearRequirementCreate(catalog_id=catalog_b.id, quantity_for_group=1),
        actor_id=organizer,
    )
    stock_a = _club_inventory(
        session, service, catalog_a.id, quantity=2, actor=organizer, key="stock-alpha"
    )
    stock_b = _club_inventory(
        session, service, catalog_b.id, quantity=1, actor=organizer, key="stock-bravo"
    )
    reservations = ReservationService(session)
    draft = reservations.create_draft(
        expedition,
        ReservationDraftCreate(
            expires_at=datetime.now(UTC) + timedelta(hours=24),
            items=[
                ReservationAllocationLine(
                    catalog_id=catalog_a.id, inventory_id=stock_a.id, quantity_reserved=2
                ),
                ReservationAllocationLine(
                    catalog_id=catalog_b.id, inventory_id=stock_b.id, quantity_reserved=1
                ),
            ],
            actor_id=organizer,
            idempotency_key="multi-line-draft",
        ),
    )
    # Simulate a racing loan consuming the last unit of B between draft and
    # confirm: drafts never hold stock, so confirm must re-validate atomically.
    session.get(GearInventory, stock_b.id).quantity_available = 0
    session.flush()
    with pytest.raises(InventoryError, match="insufficient"):
        reservations.confirm(
            draft.id,
            ReservationConfirmRequest(actor_id=organizer, idempotency_key="multi-confirm"),
        )
    # Full rollback: stock A was never frozen, reservation stays draft.
    assert session.get(GearInventory, stock_a.id).quantity_available == 2
    assert session.get(GearInventory, stock_b.id).quantity_available == 0
    refreshed = reservations.get(draft.id)
    assert refreshed.status == ReservationStatus.DRAFT
    holds = session.scalar(
        select(func.count())
        .select_from(InventoryMovement)
        .where(InventoryMovement.movement_type == InventoryMovementType.RESERVATION_HOLD)
    )
    assert holds == 0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_confirm_and_cancel_are_idempotent(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service)
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=1
    )
    inventory = _club_inventory(session, service, catalog.id, quantity=2, actor=organizer)
    reservations = ReservationService(session)
    draft = _draft_reservation(
        session, reservations, expedition, [(catalog.id, inventory.id, 1)],
        actor=organizer,
    )
    confirm_request = ReservationConfirmRequest(
        actor_id=organizer, idempotency_key="confirm-idem"
    )
    first = reservations.confirm(draft.id, confirm_request)
    second = reservations.confirm(draft.id, confirm_request)
    assert first.version == second.version
    assert session.get(GearInventory, inventory.id).quantity_available == 1
    cancel_request = ReservationCancelRequest(
        reason="Plan changed", actor_id=organizer, idempotency_key="cancel-idem"
    )
    cancelled = reservations.cancel(draft.id, cancel_request)
    assert cancelled.status == ReservationStatus.CANCELLED
    reservations.cancel(draft.id, cancel_request)
    assert session.get(GearInventory, inventory.id).quantity_available == 2
    releases = session.scalar(
        select(func.count())
        .select_from(InventoryMovement)
        .where(
            InventoryMovement.movement_type == InventoryMovementType.RESERVATION_RELEASE,
            InventoryMovement.reference_id == draft.id,
        )
    )
    assert releases == 1


def test_draft_creation_is_idempotent(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service, sku="STOVE-X", name="Trail stove")
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=1
    )
    inventory = _club_inventory(
        session, service, catalog.id, quantity=1, actor=organizer, key="stove-x-stock"
    )
    reservations = ReservationService(session)
    payload = ReservationDraftCreate(
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        items=[
            ReservationAllocationLine(
                catalog_id=catalog.id, inventory_id=inventory.id, quantity_reserved=1
            )
        ],
        actor_id=organizer,
        idempotency_key="draft-idem-key",
    )
    first = reservations.create_draft(expedition, payload)
    second = reservations.create_draft(expedition, payload)
    assert first.id == second.id
    assert session.get(GearInventory, inventory.id).quantity_available == 1


def test_confirm_idempotency_rejects_changed_request(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service)
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=1
    )
    inventory = _club_inventory(session, service, catalog.id, quantity=2, actor=organizer)
    reservations = ReservationService(session)
    draft = _draft_reservation(
        session, reservations, expedition, [(catalog.id, inventory.id, 1)],
        actor=organizer,
    )
    reservations.confirm(
        draft.id,
        ReservationConfirmRequest(actor_id=organizer, idempotency_key="shared-confirm"),
    )
    with pytest.raises(IdempotencyConflictError):
        reservations.confirm(
            draft.id,
            ReservationConfirmRequest(
                actor_id=organizer, idempotency_key="shared-confirm", expected_version=2
            ),
        )


# ---------------------------------------------------------------------------
# Fulfilment (partial + full)
# ---------------------------------------------------------------------------


def test_partial_then_full_fulfilment_converts_holds_to_loans(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service)
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=2
    )
    inventory = _club_inventory(session, service, catalog.id, quantity=2, actor=organizer)
    borrower = create_user(session, email="borrower@example.com", name="Borrower")
    reservations = ReservationService(session)
    draft = _draft_reservation(
        session, reservations, expedition, [(catalog.id, inventory.id, 2)],
        actor=organizer,
    )
    _confirm(session, reservations, draft.id, organizer)
    assert session.get(GearInventory, inventory.id).quantity_available == 0
    item_id = draft.items[0].id
    now = datetime.now(UTC)
    partial = reservations.fulfil(
        draft.id,
        ReservationFulfilRequest(
            lines=[
                ReservationFulfilmentLine(
                    reservation_item_id=item_id,
                    borrower_id=borrower,
                    quantity=1,
                    loaned_at=now,
                    due_at=now + timedelta(days=2),
                )
            ],
            actor_id=organizer,
            idempotency_key="fulfil-partial",
        ),
    )
    assert partial.status == ReservationStatus.PARTIALLY_FULFILLED
    assert partial.items[0].quantity_fulfilled == 1
    # Hold converted to loan: release (+1) then loan_out (-1), net zero.
    assert session.get(GearInventory, inventory.id).quantity_available == 0
    movements = list(
        session.scalars(
            select(InventoryMovement)
            .where(InventoryMovement.inventory_id == inventory.id)
            .order_by(InventoryMovement.id)
        )
    )
    deltas = [(m.movement_type, m.quantity_delta, m.quantity_after) for m in movements]
    assert (InventoryMovementType.RESERVATION_HOLD, -2, 0) in deltas
    assert (InventoryMovementType.RESERVATION_RELEASE, 1, 1) in deltas
    assert (InventoryMovementType.LOAN_OUT, -1, 0) in deltas
    full = reservations.fulfil(
        draft.id,
        ReservationFulfilRequest(
            lines=[
                ReservationFulfilmentLine(
                    reservation_item_id=item_id,
                    borrower_id=borrower,
                    quantity=1,
                    loaned_at=now + timedelta(hours=1),
                    due_at=now + timedelta(days=3),
                )
            ],
            actor_id=organizer,
            idempotency_key="fulfil-final",
        ),
    )
    assert full.status == ReservationStatus.FULFILLED
    loans = service.gear.list_loans(borrower_id=borrower)
    assert len(loans) == 1
    assert loans[0].quantity == 2
    assert loans[0].returned_quantity == 0
    assert loans[0].status == LoanStatus.ACTIVE
    assert loans[0].reservation_item_id == item_id


def test_fulfilment_cannot_exceed_reserved_quantity(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service)
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=1
    )
    inventory = _club_inventory(session, service, catalog.id, quantity=1, actor=organizer)
    borrower = create_user(session, email="b2@example.com", name="B2")
    reservations = ReservationService(session)
    draft = _draft_reservation(
        session, reservations, expedition, [(catalog.id, inventory.id, 1)],
        actor=organizer,
    )
    _confirm(session, reservations, draft.id, organizer, key="confirm-x1")
    with pytest.raises(InventoryError, match="outstanding reservation"):
        reservations.fulfil(
            draft.id,
            ReservationFulfilRequest(
                lines=[
                    ReservationFulfilmentLine(
                        reservation_item_id=draft.items[0].id,
                        borrower_id=borrower,
                        quantity=2,
                    )
                ],
                actor_id=organizer,
                idempotency_key="over-fulfil",
            ),
        )


# ---------------------------------------------------------------------------
# Cancel / expire rollback
# ---------------------------------------------------------------------------


def test_cancel_confirmed_reservation_releases_frozen_stock(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service)
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=2
    )
    inventory = _club_inventory(session, service, catalog.id, quantity=3, actor=organizer)
    reservations = ReservationService(session)
    draft = _draft_reservation(
        session, reservations, expedition, [(catalog.id, inventory.id, 2)],
        actor=organizer,
    )
    _confirm(session, reservations, draft.id, organizer, key="confirm-cancel")
    cancelled = reservations.cancel(
        draft.id,
        ReservationCancelRequest(
            reason="Trip postponed", actor_id=organizer, idempotency_key="cancel-1"
        ),
    )
    assert cancelled.status == ReservationStatus.CANCELLED
    assert cancelled.cancel_reason == "Trip postponed"
    assert session.get(GearInventory, inventory.id).quantity_available == 3
    release = session.scalar(
        select(InventoryMovement).where(
            InventoryMovement.movement_type == InventoryMovementType.RESERVATION_RELEASE,
            InventoryMovement.reference_id == draft.id,
        )
    )
    assert release.quantity_delta == 2
    assert release.quantity_after == 3


def test_cancel_partially_fulfilled_only_releases_remainder(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service)
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=2
    )
    inventory = _club_inventory(session, service, catalog.id, quantity=2, actor=organizer)
    borrower = create_user(session, email="b3@example.com", name="B3")
    reservations = ReservationService(session)
    draft = _draft_reservation(
        session, reservations, expedition, [(catalog.id, inventory.id, 2)],
        actor=organizer,
    )
    _confirm(session, reservations, draft.id, organizer, key="confirm-pc")
    reservations.fulfil(
        draft.id,
        ReservationFulfilRequest(
            lines=[
                ReservationFulfilmentLine(
                    reservation_item_id=draft.items[0].id,
                    borrower_id=borrower,
                    quantity=1,
                )
            ],
            actor_id=organizer,
            idempotency_key="fulfil-pc",
        ),
    )
    reservations.cancel(
        draft.id,
        ReservationCancelRequest(
            reason="Weather window lost", actor_id=organizer, idempotency_key="cancel-pc"
        ),
    )
    # One unit is out on loan; only the unfulfilled unit returns to the pool.
    assert session.get(GearInventory, inventory.id).quantity_available == 1
    loan = service.gear.list_loans(borrower_id=borrower)[0]
    assert loan.status == LoanStatus.ACTIVE


def test_cancelling_draft_does_not_touch_inventory(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service)
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=1
    )
    inventory = _club_inventory(session, service, catalog.id, quantity=2, actor=organizer)
    reservations = ReservationService(session)
    draft = _draft_reservation(
        session, reservations, expedition, [(catalog.id, inventory.id, 1)],
        actor=organizer,
    )
    reservations.cancel(
        draft.id,
        ReservationCancelRequest(
            reason="Edited", actor_id=organizer, idempotency_key="cancel-draft"
        ),
    )
    assert session.get(GearInventory, inventory.id).quantity_available == 2
    assert (
        session.scalar(
            select(func.count())
            .select_from(InventoryMovement)
            .where(InventoryMovement.movement_type == InventoryMovementType.RESERVATION_RELEASE)
        )
        == 0
    )


def test_expire_due_releases_confirmed_holds_and_marks_expired(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service, sku="BEACON", name="Avalanche beacon")
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=1
    )
    inventory = _club_inventory(
        session, service, catalog.id, quantity=2, actor=organizer, key="beacon-stock"
    )
    reservations = ReservationService(session)
    draft = _draft_reservation(
        session,
        reservations,
        expedition,
        [(catalog.id, inventory.id, 1)],
        actor=organizer,
        key="expiring-draft",
        expires_in_hours=48,
    )
    _confirm(session, reservations, draft.id, organizer, key="confirm-exp")
    deadline = draft.expires_at + timedelta(minutes=1)
    expired = reservations.expire_due(
        actor_id=organizer, now=deadline, idempotency_key="expire-batch-key"
    )
    assert len(expired) == 1
    assert expired[0].status == ReservationStatus.EXPIRED
    assert session.get(GearInventory, inventory.id).quantity_available == 2
    # Replaying the same batch request is idempotent and does not release twice.
    replay = reservations.expire_due(
        actor_id=organizer, now=deadline, idempotency_key="expire-batch-key"
    )
    assert [item.id for item in replay] == [draft.id]
    releases = session.scalar(
        select(func.count())
        .select_from(InventoryMovement)
        .where(
            InventoryMovement.movement_type == InventoryMovementType.RESERVATION_RELEASE,
            InventoryMovement.reference_id == draft.id,
        )
    )
    assert releases == 1
    assert session.get(GearInventory, inventory.id).quantity_available == 2


def test_confirmed_reservation_cannot_be_confirmed_again(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service, sku="GAITER", name="Gaiters")
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=1
    )
    inventory = _club_inventory(
        session, service, catalog.id, quantity=1, actor=organizer, key="gaiter-stock"
    )
    reservations = ReservationService(session)
    draft = _draft_reservation(
        session, reservations, expedition, [(catalog.id, inventory.id, 1)],
        actor=organizer, key="gaiter-draft",
    )
    _confirm(session, reservations, draft.id, organizer, key="gaiter-confirm")
    with pytest.raises(InvalidStateError, match="only draft"):
        reservations.confirm(
            draft.id,
            ReservationConfirmRequest(actor_id=organizer, idempotency_key="double-confirm"),
        )


# ---------------------------------------------------------------------------
# Capacity delta does not silently mutate confirmed reservations
# ---------------------------------------------------------------------------


def test_capacity_delta_reports_difference_without_mutating_reservation(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service, sku="LAMP-2", name="Headlamp")
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=1, per_person=1
    )
    inventory = _club_inventory(
        session, service, catalog.id, quantity=2, actor=organizer, key="lamp-stock"
    )
    reservations = ReservationService(session)
    draft = _draft_reservation(
        session, reservations, expedition, [(catalog.id, inventory.id, 1)],
        actor=organizer, key="lamp-draft",
    )
    _confirm(session, reservations, draft.id, organizer, key="lamp-confirm")
    # Simulate a capacity change: add two more confirmed participants.
    from trailforge.database.base import utc_now
    from trailforge.domain.enums import RegistrationStatus, TeamRole
    from trailforge.models.activities import ExpeditionRegistration

    for index in range(2):
        extra = create_user(
            session, email=f"extra-{index}@example.com", name=f"Extra {index}"
        )
        session.add(
            ExpeditionRegistration(
                expedition_id=expedition,
                user_id=extra,
                role=TeamRole.MEMBER,
                status=RegistrationStatus.CONFIRMED,
                registered_at=utc_now(),
            )
        )
    session.flush()
    delta = reservations.capacity_delta(expedition)
    assert delta.participant_count_before == 1
    assert delta.participant_count_after == 3
    personal_line = next(line for line in delta.lines if line.requirement_source == "personal")
    assert personal_line.required_before == 1
    assert personal_line.required_after == 3
    assert personal_line.required_delta == 2
    group_line = next(line for line in delta.lines if line.requirement_source == "group")
    assert group_line.required_delta == 0
    # The confirmed reservation itself is untouched.
    untouched = reservations.get(draft.id)
    assert untouched.status == ReservationStatus.CONFIRMED
    assert untouched.participant_snapshot == 1
    assert untouched.items[0].quantity_reserved == 1
    assert session.get(GearInventory, inventory.id).quantity_available == 1


def test_capacity_delta_requires_an_open_reservation(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service, sku="MAP-9", name="Topo map")
    _, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=1
    )
    with pytest.raises(NotFoundError, match="no open reservation"):
        ReservationService(session).capacity_delta(expedition)


# ---------------------------------------------------------------------------
# Missing report integration
# ---------------------------------------------------------------------------


def test_missing_report_counts_confirmed_reservation_as_covered(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service, sku="SHOVEL", name="Snow shovel")
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=2
    )
    inventory = _club_inventory(
        session, service, catalog.id, quantity=2, actor=organizer, key="shovel-stock"
    )
    reservations = ReservationService(session)
    draft = _draft_reservation(
        session, reservations, expedition, [(catalog.id, inventory.id, 2)],
        actor=organizer, key="shovel-draft",
    )
    # Before confirmation the club gear is still missing.
    assert any(
        item.requirement_source == "group"
        for item in service.missing_report(expedition).missing_items
    )
    _confirm(session, reservations, draft.id, organizer, key="shovel-confirm")
    report = service.missing_report(expedition)
    assert all(item.requirement_source != "group" for item in report.missing_items)
    assert report.is_ready is True


def test_missing_report_personal_gear_not_covered_by_reservation(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service, sku="GLOVES", name="Gloves")
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=0, per_person=1
    )
    _club_inventory(
        session, service, catalog.id, quantity=5, actor=organizer, key="glove-stock"
    )
    report = service.missing_report(expedition)
    assert len(report.missing_items) == 1
    assert report.missing_items[0].requirement_source == "personal"
    assert report.missing_items[0].reserved_quantity == 0
    # A member packing their own pair clears the personal shortage.
    GearService(session).upsert_check(
        expedition,
        GearCheckUpsert(
            user_id=organizer,
            catalog_id=catalog.id,
            quantity=1,
            status="verified",
            verified_by=organizer,
        ),
    )
    assert service.missing_report(expedition).is_ready is True


# ---------------------------------------------------------------------------
# Expedition cancellation cascade
# ---------------------------------------------------------------------------


def test_cancelling_expedition_cascades_reservation_release(session) -> None:
    from trailforge.domain.enums import ActivityStatus
    from trailforge.schemas.activities import ActivityStateChange

    service = GearService(session)
    catalog = _catalog(session, service, sku="PROBE", name="Avalanche probe")
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=1
    )
    inventory = _club_inventory(
        session, service, catalog.id, quantity=2, actor=organizer, key="probe-stock"
    )
    reservations = ReservationService(session)
    draft = _draft_reservation(
        session, reservations, expedition, [(catalog.id, inventory.id, 1)],
        actor=organizer, key="probe-draft",
    )
    _confirm(session, reservations, draft.id, organizer, key="probe-confirm")
    # Open registration so the activity can move through its lifecycle to cancel.
    from trailforge.services.activities import ExpeditionService

    expeditions = ExpeditionService(session)
    # Draft -> open is allowed directly; then cancel from open.
    expeditions.change_status(
        expedition,
        ActivityStateChange(
            target_status=ActivityStatus.OPEN.value,
            actor_id=organizer,
            reason="",
        ),
    )
    expeditions.change_status(
        expedition,
        ActivityStateChange(
            target_status=ActivityStatus.CANCELLED.value,
            actor_id=organizer,
            reason="Route unsafe",
        ),
    )
    assert reservations.get(draft.id).status == ReservationStatus.CANCELLED
    assert session.get(GearInventory, inventory.id).quantity_available == 2


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


def test_reservation_lifecycle_writes_structured_audit_logs(session) -> None:
    service = GearService(session)
    catalog = _catalog(session, service, sku="WACKY", name="Wacky bags")
    organizer, _, expedition = _expedition_with_group_requirement(
        session, catalog_id=catalog.id, group=1
    )
    inventory = _club_inventory(
        session, service, catalog.id, quantity=1, actor=organizer, key="wacky-stock"
    )
    reservations = ReservationService(session)
    draft = _draft_reservation(
        session, reservations, expedition, [(catalog.id, inventory.id, 1)],
        actor=organizer, key="wacky-draft",
    )
    _confirm(session, reservations, draft.id, organizer, key="wacky-confirm")
    reservations.cancel(
        draft.id,
        ReservationCancelRequest(
            reason="No go", actor_id=organizer, idempotency_key="wacky-cancel"
        ),
    )
    actions = {
        str(row.action)
        for row in session.scalars(
            select(AuditLog).where(AuditLog.entity_type == "gear_reservation")
        )
    }
    assert "reservation_drafted" in actions
    assert "reservation_confirmed" in actions
    assert "reservation_cancelled" in actions


# ---------------------------------------------------------------------------
# Concurrency: two activities racing for the last unit
# ---------------------------------------------------------------------------


def _setup_contention(database: Database) -> tuple[int, int, int, int, int]:
    with database.session() as session:
        service = GearService(session)
        catalog = service.create_catalog(
            GearCatalogCreate(sku="RACE-KIT", name="Race kit", category="racing"),
            actor_id=create_user(session, email="race-admin@example.com", name="Race Admin"),
        )
        organizer = create_user(session, email="race-org@example.com", name="Org")
        route = create_route(session, actor_id=organizer)
        expedition_one = create_expedition(
            session, organizer_id=organizer, route_id=route, offset_days=10
        )
        expedition_two = create_expedition(
            session, organizer_id=organizer, route_id=route, offset_days=20
        )
        for expedition_id in (expedition_one, expedition_two):
            service.add_requirement(
                expedition_id,
                GearRequirementCreate(catalog_id=catalog.id, quantity_for_group=1),
                actor_id=organizer,
            )
        inventory = service.create_inventory(
            GearInventoryCreate(
                catalog_id=catalog.id,
                ownership="club",
                quantity_total=1,
                actor_id=organizer,
                idempotency_key="race-single-unit",
            )
        )
        reservations = ReservationService(session)
        drafts = []
        for index, expedition_id in enumerate((expedition_one, expedition_two)):
            draft = reservations.create_draft(
                expedition_id,
                ReservationDraftCreate(
                    expires_at=datetime.now(UTC) + timedelta(hours=24),
                    items=[
                        ReservationAllocationLine(
                            catalog_id=catalog.id,
                            inventory_id=inventory.id,
                            quantity_reserved=1,
                        )
                    ],
                    actor_id=organizer,
                    idempotency_key=f"race-draft-{index}",
                ),
            )
            drafts.append(draft.id)
        return organizer, catalog.id, inventory.id, drafts[0], drafts[1]


def test_concurrent_confirmation_of_last_unit_never_goes_negative(database: Database) -> None:
    organizer, _, inventory_id, draft_one, draft_two = _setup_contention(database)

    def attempt(draft_id: int, key: str) -> str:
        def operation(session) -> str:
            ReservationService(session).confirm(
                draft_id,
                ReservationConfirmRequest(actor_id=organizer, idempotency_key=key),
            )
            return "confirmed"

        try:
            return database.run_write(operation)
        except InventoryError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(attempt, draft_one, "race-confirm-one"),
            pool.submit(attempt, draft_two, "race-confirm-two"),
        ]
        outcomes = sorted(future.result() for future in futures)
    assert outcomes == ["confirmed", "rejected"]
    with database.session() as session:
        available = session.get(GearInventory, inventory_id).quantity_available
        total = session.get(GearInventory, inventory_id).quantity_total
        assert available == 0
        assert total == 1
        confirmed = ReservationService(session).list(
            status=ReservationStatus.CONFIRMED
        )
        drafts = ReservationService(session).list(status=ReservationStatus.DRAFT)
        assert len(confirmed) == 1
        assert len(drafts) == 1


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------


def test_confirmed_reservation_survives_engine_restart(settings) -> None:
    first = Database(settings)
    initialize_database(first)
    with first.session() as session:
        service = GearService(session)
        catalog = service.create_catalog(
            GearCatalogCreate(sku="CAMP-STOVE", name="Camp stove", category="cooking"),
            actor_id=create_user(session, email="stove-admin@example.com", name="Stove Admin"),
        )
        organizer = create_user(session, email="stove-org@example.com", name="Stove Org")
        route = create_route(session, actor_id=organizer)
        expedition = create_expedition(session, organizer_id=organizer, route_id=route)
        service.add_requirement(
            expedition,
            GearRequirementCreate(catalog_id=catalog.id, quantity_for_group=1),
            actor_id=organizer,
        )
        inventory = service.create_inventory(
            GearInventoryCreate(
                catalog_id=catalog.id,
                ownership="club",
                quantity_total=2,
                actor_id=organizer,
                idempotency_key="stove-stock",
            )
        )
        reservations = ReservationService(session)
        draft = reservations.create_draft(
            expedition,
            ReservationDraftCreate(
                expires_at=datetime.now(UTC) + timedelta(hours=24),
                items=[
                    ReservationAllocationLine(
                        catalog_id=catalog.id,
                        inventory_id=inventory.id,
                        quantity_reserved=1,
                    )
                ],
                actor_id=organizer,
                idempotency_key="stove-draft",
            ),
        )
        reservations.confirm(
            draft.id,
            ReservationConfirmRequest(actor_id=organizer, idempotency_key="stove-confirm"),
        )
        reservation_id = draft.id
        inventory_id = inventory.id
    first.engine.dispose()

    second = Database(settings)
    initialize_database(second)
    with second.session() as session:
        restored = ReservationService(session).get(reservation_id)
        assert restored.status == ReservationStatus.CONFIRMED
        assert restored.items[0].quantity_reserved == 1
        assert session.get(GearInventory, inventory_id).quantity_available == 1
        # The frozen unit is still honoured: a direct loan cannot exceed stock.
        with pytest.raises(InventoryError, match="insufficient"):
            GearService(session).loan(
                GearLoanCreate(
                    inventory_id=inventory_id,
                    borrower_id=create_user(session, email="late@example.com", name="Late"),
                    quantity=2,
                    loaned_at=datetime.now(UTC),
                    due_at=datetime.now(UTC) + timedelta(days=1),
                    actor_id=restored.created_by,
                    idempotency_key="loan-after-restart",
                )
            )
    second.engine.dispose()
