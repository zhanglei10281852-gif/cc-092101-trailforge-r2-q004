from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from trailforge.database.base import utc_now
from trailforge.domain.enums import (
    AuditAction,
    GearCondition,
    GearOwnership,
    InventoryMovementType,
    ReservationStatus,
)
from trailforge.errors import (
    ConflictError,
    InvalidStateError,
    InventoryError,
    NotFoundError,
    ValidationError,
)
from trailforge.models.activities import Expedition
from trailforge.models.gear import (
    ActivityGearCheck,
    GearInventory,
    GearLoan,
    GearReservation,
    GearReservationItem,
    InventoryMovement,
)
from trailforge.repositories.activities import ExpeditionRepository
from trailforge.repositories.base import apply_version
from trailforge.repositories.gear import GearRepository
from trailforge.repositories.reservations import (
    BLOCKED_CONDITIONS,
    OPEN_RESERVATION_STATUSES,
    ReservationRepository,
)
from trailforge.repositories.users import UserRepository
from trailforge.schemas.common import Page
from trailforge.schemas.gear import (
    GearLoanResponse,
    MissingGearItem,
    MissingGearReport,
    ReservationCancelRequest,
    ReservationCheckoutRequest,
    ReservationConfirmRequest,
    ReservationDraftPlan,
    ReservationDraftRequest,
    ReservationExpiryResult,
    ReservationInventoryOption,
    ReservationItemResponse,
    ReservationRequirementDelta,
    ReservationRequirementDeltaItem,
    ReservationResponse,
    ReservationSuggestion,
)
from trailforge.services.base import ServiceBase

_TERMINAL_STATUSES = {
    ReservationStatus.CANCELLED,
    ReservationStatus.EXPIRED,
    ReservationStatus.CHECKED_OUT,
}


class ReservationService(ServiceBase):
    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.reservations = ReservationRepository(session)
        self.gear = GearRepository(session)
        self.users = UserRepository(session)
        self.expeditions = ExpeditionRepository(session)

    # ------------------------------------------------------------------ queries

    def get_reservation(self, reservation_id: int) -> ReservationResponse:
        reservation = self.reservations.get_detail(reservation_id)
        if reservation is None:
            raise NotFoundError(f"GearReservation {reservation_id} was not found")
        return self._to_response(reservation)

    def list_reservations(
        self,
        *,
        expedition_id: int | None = None,
        status: ReservationStatus | None = None,
        page: int = 1,
        page_size: int = 20,
        sort: str = "created_at",
        direction: str = "asc",
    ) -> Page[ReservationResponse]:
        if expedition_id is not None and self.expeditions.get(expedition_id) is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        result = self.reservations.list_reservations(
            expedition_id=expedition_id,
            status=status,
            page=page,
            page_size=page_size,
            sort=sort,
            direction=direction,
        )
        return Page[ReservationResponse].build(
            [self._to_response(item) for item in result.items],
            page=result.page,
            page_size=result.page_size,
            total=result.total,
        )

    def draft_plan(self, expedition_id: int) -> ReservationDraftPlan:
        """Generate a reservation proposal from activity gear requirements.

        Only ``quantity_for_group`` (shared club gear) is reservable. Personal
        ``quantity_per_person`` demand belongs to members' own packed gear and
        is tracked through the missing report.
        """
        expedition = self.expeditions.get(expedition_id)
        if expedition is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        participant_count = self.gear.participant_count(expedition_id)
        reserved = self.reservations.open_reserved_quantities(expedition_id)
        suggestions: list[ReservationSuggestion] = []
        for requirement in self.gear.requirements(expedition_id):
            if requirement.quantity_for_group <= 0:
                continue
            catalog = self.gear.get_catalog(requirement.catalog_id)
            if catalog is None:
                continue
            club_inventories = self.reservations.club_inventories(catalog.id)
            options = [self._inventory_option(item) for item in club_inventories]
            club_available = sum(
                item.quantity_available
                for item in club_inventories
                if item.condition not in BLOCKED_CONDITIONS
            )
            already_reserved = reserved.get(catalog.id, 0)
            missing = max(requirement.quantity_for_group - already_reserved, 0)
            suggestions.append(
                ReservationSuggestion(
                    catalog_id=catalog.id,
                    sku=catalog.sku,
                    name=catalog.name,
                    category=catalog.category,
                    group_required=requirement.quantity_for_group,
                    club_reserved_quantity=already_reserved,
                    open_reservation_quantity=already_reserved,
                    club_available_quantity=club_available,
                    suggested_quantity=min(missing, club_available),
                    options=options,
                )
            )
        return ReservationDraftPlan(
            expedition_id=expedition_id,
            participant_count=participant_count,
            generated_at=utc_now(),
            items=suggestions,
        )

    def requirement_delta(
        self, expedition_id: int, *, participant_count_before: int
    ) -> ReservationRequirementDelta:
        """Compute the gap after activity capacity/roster or requirement change.

        Confirmed reservations are never modified here; the report only shows
        how much more club stock must be reserved and how personal packing
        demand moved with the participant count.
        """
        expedition = self.expeditions.get(expedition_id)
        if expedition is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        if participant_count_before < 0:
            raise ValidationError("participant_count_before cannot be negative")
        after = self.gear.participant_count(expedition_id)
        reserved = self.reservations.open_reserved_quantities(expedition_id)
        packed = self._packed_quantities(expedition_id)
        items: list[ReservationRequirementDeltaItem] = []
        for requirement in self.gear.requirements(expedition_id):
            catalog = self.gear.get_catalog(requirement.catalog_id)
            if catalog is None:
                continue
            personal_before = requirement.quantity_per_person * participant_count_before
            personal_after = requirement.quantity_per_person * after
            personal_packed = packed.get(catalog.id, 0)
            items.append(
                ReservationRequirementDeltaItem(
                    catalog_id=catalog.id,
                    sku=catalog.sku,
                    name=catalog.name,
                    group_required=requirement.quantity_for_group,
                    confirmed_reserved_quantity=reserved.get(catalog.id, 0),
                    group_delta=max(
                        requirement.quantity_for_group - reserved.get(catalog.id, 0), 0
                    ),
                    personal_required_before=personal_before,
                    personal_required_after=personal_after,
                    personal_packed_quantity=personal_packed,
                    personal_delta=max(personal_after - personal_packed, 0),
                )
            )
        return ReservationRequirementDelta(
            expedition_id=expedition_id,
            participant_count_before=participant_count_before,
            participant_count_after=after,
            changed_at=utc_now(),
            items=items,
        )

    def missing_report(
        self, expedition_id: int, *, now: datetime | None = None
    ) -> MissingGearReport:
        """Missing report that treats club reservations and personal packing separately."""
        expedition = self.expeditions.get(expedition_id)
        if expedition is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        participant_count = self.gear.participant_count(expedition_id)
        packed = self._packed_quantities(expedition_id)
        reserved = self.reservations.open_reserved_quantities(expedition_id)
        missing_items: list[MissingGearItem] = []
        reserved_units = 0
        for requirement in self.gear.requirements(expedition_id):
            group_required = requirement.quantity_for_group
            personal_required = requirement.quantity_per_person * participant_count
            packed_quantity = packed.get(requirement.catalog_id, 0)
            club_reserved = reserved.get(requirement.catalog_id, 0)
            reserved_units += club_reserved
            group_missing = max(group_required - club_reserved, 0)
            personal_missing = max(personal_required - packed_quantity, 0)
            missing_quantity = group_missing + personal_missing
            if missing_quantity <= 0 or not requirement.mandatory:
                continue
            catalog = self.gear.get_catalog(requirement.catalog_id)
            if catalog is None:
                continue
            missing_items.append(
                MissingGearItem(
                    catalog_id=catalog.id,
                    sku=catalog.sku,
                    name=catalog.name,
                    mandatory=requirement.mandatory,
                    required_quantity=group_required + personal_required,
                    packed_quantity=packed_quantity,
                    missing_quantity=missing_quantity,
                    group_required=group_required,
                    personal_required=personal_required,
                    club_reserved_quantity=club_reserved,
                    group_missing_quantity=group_missing,
                    personal_missing_quantity=personal_missing,
                )
            )
        return MissingGearReport(
            expedition_id=expedition_id,
            participant_count=participant_count,
            checked_at=now or utc_now(),
            is_ready=not missing_items,
            missing_items=missing_items,
            reserved_units=reserved_units,
        )

    # ------------------------------------------------------------------ draft

    def upsert_draft(
        self, expedition_id: int, data: ReservationDraftRequest
    ) -> ReservationResponse:
        scope = f"expedition:{expedition_id}:reservation:draft"
        prior = self.find_idempotent(scope=scope, key=data.idempotency_key, payload=data)
        if prior is not None:
            existing = self.reservations.get_detail(prior.resource_id)
            if existing is None:
                raise ConflictError("idempotency record references a missing reservation")
            return self._to_response(existing)
        if self.expeditions.get(expedition_id) is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        self.users.require(data.actor_id)
        reservation = self.reservations.get_by_draft_key(
            expedition_id, data.draft_key, for_update=True
        )
        action = AuditAction.RESERVATION_DRAFTED
        if reservation is None:
            reservation = GearReservation(
                expedition_id=expedition_id,
                status=ReservationStatus.DRAFT,
                draft_key=data.draft_key,
                expires_at=data.expires_at,
                notes=data.notes,
            )
            self.session.add(reservation)
            self.session.flush()
        else:
            if reservation.status != ReservationStatus.DRAFT:
                raise ConflictError(
                    "a non-draft reservation already uses this draft key",
                    context={"reservation_id": reservation.id, "status": reservation.status},
                )
            action = AuditAction.UPDATED
            apply_version(reservation, None)
            reservation.expires_at = data.expires_at
            reservation.notes = data.notes
            for item in list(reservation.items):
                self.session.delete(item)
            reservation.items.clear()
            self.session.flush()
        allocations = self._validate_allocations(data)
        for inventory, quantity, notes in allocations:
            reservation.items.append(
                GearReservationItem(
                    inventory_id=inventory.id,
                    catalog_id=inventory.catalog_id,
                    quantity=quantity,
                    notes=notes,
                )
            )
        self.session.flush()
        response = self._to_response(reservation)
        self.save_idempotent(
            scope=scope,
            key=data.idempotency_key,
            payload=data,
            resource_type="gear_reservation",
            resource_id=reservation.id,
            response=response.model_dump(mode="json"),
        )
        self.audit(
            actor_id=data.actor_id,
            entity_type="gear_reservation",
            entity_id=reservation.id,
            action=action,
            after=self.snapshot(reservation),
            context={"expedition_id": expedition_id, "items": len(allocations)},
            correlation_id=data.idempotency_key,
        )
        return response

    # ------------------------------------------------------------------ confirm

    def confirm(self, reservation_id: int, data: ReservationConfirmRequest) -> ReservationResponse:
        scope = f"gear_reservation:{reservation_id}:confirm"
        prior = self.find_idempotent(scope=scope, key=data.idempotency_key, payload=data)
        if prior is not None:
            reservation = self.reservations.get_detail(prior.resource_id)
            if reservation is None:
                raise ConflictError("idempotency record references a missing reservation")
            return self._to_response(reservation)
        reservation = self.reservations.get_detail(reservation_id, for_update=True)
        if reservation is None:
            raise NotFoundError(f"GearReservation {reservation_id} was not found")
        self.users.require(data.actor_id)
        if reservation.status != ReservationStatus.DRAFT:
            raise InvalidStateError(
                f"only draft reservations can be confirmed (current: {reservation.status})"
            )
        if not reservation.items:
            raise ValidationError("cannot confirm an empty reservation")
        if reservation.expires_at is not None and reservation.expires_at <= utc_now():
            raise InvalidStateError("reservation draft has already expired")
        apply_version(reservation, data.expected_version)
        # Validate every line first so the batch freezes all stock or none.
        requested: dict[int, int] = {}
        inventories: dict[int, GearInventory] = {}
        for item in reservation.items:
            inventory = self.gear.get_inventory(item.inventory_id, for_update=True)
            if inventory is None:
                raise NotFoundError(f"GearInventory {item.inventory_id} was not found")
            if inventory.ownership != GearOwnership.CLUB:
                raise InventoryError(
                    "personal inventory cannot be reserved as shared activity gear",
                    context={"inventory_id": inventory.id, "ownership": inventory.ownership},
                )
            if inventory.condition in BLOCKED_CONDITIONS:
                raise InventoryError(
                    "damaged or retired gear cannot be allocated",
                    context={"inventory_id": inventory.id, "condition": inventory.condition},
                )
            requested[inventory.id] = requested.get(inventory.id, 0) + item.quantity
            inventories[inventory.id] = inventory
        for inventory_id, quantity in requested.items():
            inventory = inventories[inventory_id]
            if inventory.quantity_available < quantity:
                raise InventoryError(
                    "insufficient available inventory for reservation batch",
                    context={
                        "inventory_id": inventory_id,
                        "available": inventory.quantity_available,
                        "requested": quantity,
                    },
                )
        # Atomic conditional freeze: concurrent contenders cannot drive stock negative.
        frozen_after: dict[int, int] = {}
        for inventory_id, quantity in requested.items():
            result = self.session.execute(
                update(GearInventory)
                .where(
                    GearInventory.id == inventory_id,
                    GearInventory.quantity_available >= quantity,
                )
                .values(
                    quantity_available=GearInventory.quantity_available - quantity,
                    version=GearInventory.version + 1,
                )
            )
            if result.rowcount != 1:
                raise InventoryError(
                    "reservation batch lost the inventory race",
                    context={"inventory_id": inventory_id, "requested": quantity},
                )
            self.session.refresh(inventories[inventory_id])
            frozen_after[inventory_id] = inventories[inventory_id].quantity_available
        for item in reservation.items:
            self.session.add(
                InventoryMovement(
                    inventory_id=item.inventory_id,
                    movement_type=InventoryMovementType.RESERVED,
                    quantity_delta=-item.quantity,
                    quantity_after=frozen_after[item.inventory_id],
                    reference_type="gear_reservation",
                    reference_id=reservation.id,
                    reason=f"Reserved for expedition {reservation.expedition_id}",
                    actor_id=data.actor_id,
                )
            )
        reservation.status = ReservationStatus.CONFIRMED
        reservation.confirmed_at = utc_now()
        self.session.flush()
        response = self._to_response(reservation)
        self.save_idempotent(
            scope=scope,
            key=data.idempotency_key,
            payload=data,
            resource_type="gear_reservation",
            resource_id=reservation.id,
            response=response.model_dump(mode="json"),
        )
        self.audit(
            actor_id=data.actor_id,
            entity_type="gear_reservation",
            entity_id=reservation.id,
            action=AuditAction.RESERVATION_CONFIRMED,
            before={"status": ReservationStatus.DRAFT.value},
            after={"status": ReservationStatus.CONFIRMED.value},
            context={"frozen_units": sum(requested.values())},
            correlation_id=data.idempotency_key,
        )
        return response

    # ------------------------------------------------------------------ checkout

    def checkout(
        self, reservation_id: int, data: ReservationCheckoutRequest
    ) -> tuple[ReservationResponse, list[GearLoanResponse]]:
        scope = f"gear_reservation:{reservation_id}:checkout"
        prior = self.find_idempotent(scope=scope, key=data.idempotency_key, payload=data)
        if prior is not None:
            reservation = self.reservations.get_detail(prior.resource_id)
            if reservation is None:
                raise ConflictError("idempotency record references a missing reservation")
            loan_ids = list(prior.response_json.get("loan_ids", []))
            loans = [
                GearLoanResponse.model_validate(loan)
                for loan_id in loan_ids
                if (loan := self.session.get(GearLoan, loan_id)) is not None
            ]
            return self._to_response(reservation), loans
        reservation = self.reservations.get_detail(reservation_id, for_update=True)
        if reservation is None:
            raise NotFoundError(f"GearReservation {reservation_id} was not found")
        self.users.require(data.actor_id)
        if reservation.status not in OPEN_RESERVATION_STATUSES:
            raise InvalidStateError(
                f"reservation is not open for checkout (current: {reservation.status})"
            )
        items_by_id = {item.id: item for item in reservation.items}
        requested_per_item: dict[int, int] = {}
        for entry in data.loans:
            item = items_by_id.get(entry.item_id)
            if item is None:
                raise ValidationError(
                    "loan entry references an item from another reservation",
                    context={"item_id": entry.item_id},
                )
            self.users.require(entry.borrower_id)
            if self.reservations.active_item_loan(item.id, entry.borrower_id) is not None:
                raise ConflictError(
                    "borrower already has an active loan against this reservation item",
                    context={"item_id": item.id, "borrower_id": entry.borrower_id},
                )
            requested_per_item[item.id] = requested_per_item.get(item.id, 0) + entry.quantity
        locks: dict[int, GearInventory] = {}
        for item_id, quantity in requested_per_item.items():
            item = items_by_id[item_id]
            outstanding = self._outstanding(item)
            if quantity > outstanding:
                raise InventoryError(
                    "checkout quantity exceeds outstanding reserved quantity",
                    context={"item_id": item_id, "outstanding": outstanding, "requested": quantity},
                )
            locks[item.inventory_id] = self.gear.get_inventory(
                item.inventory_id, for_update=True
            )
        loans: list[GearLoan] = []
        for entry in data.loans:
            item = items_by_id[entry.item_id]
            inventory = locks[item.inventory_id]
            if inventory is None:
                raise NotFoundError(f"GearInventory {item.inventory_id} was not found")
            if inventory.condition in BLOCKED_CONDITIONS:
                raise InventoryError(
                    "damaged or retired gear cannot be loaned",
                    context={"inventory_id": inventory.id, "condition": inventory.condition},
                )
            # Atomic guard against concurrent checkouts of the same frozen line.
            outstanding_expr = (
                GearReservationItem.quantity
                - GearReservationItem.checked_out_quantity
                - GearReservationItem.released_quantity
            )
            guarded = self.session.execute(
                update(GearReservationItem)
                .where(
                    GearReservationItem.id == item.id,
                    outstanding_expr >= entry.quantity,
                )
                .values(
                    checked_out_quantity=GearReservationItem.checked_out_quantity + entry.quantity
                )
            )
            if guarded.rowcount != 1:
                raise InventoryError(
                    "reservation line lost the checkout race",
                    context={"item_id": item.id, "requested": entry.quantity},
                )
            self.session.refresh(item)
            # Frozen units re-enter the available pool and leave as a loan in
            # the same transaction, so the inventory ledger shows both steps.
            inventory.quantity_available += entry.quantity
            apply_version(inventory, None)
            release_movement = InventoryMovement(
                inventory_id=inventory.id,
                movement_type=InventoryMovementType.RESERVATION_RELEASED,
                quantity_delta=entry.quantity,
                quantity_after=inventory.quantity_available,
                reference_type="gear_reservation_item",
                reference_id=item.id,
                reason="Reservation converted to loan",
                actor_id=data.actor_id,
            )
            loan = GearLoan(
                inventory_id=inventory.id,
                borrower_id=entry.borrower_id,
                expedition_id=reservation.expedition_id,
                reservation_item_id=item.id,
                quantity=entry.quantity,
                loaned_at=entry.loaned_at,
                due_at=entry.due_at,
                condition_out=inventory.condition,
                notes=entry.notes,
            )
            self.session.add(loan)
            self.session.flush()
            inventory.quantity_available -= entry.quantity
            loan_movement = InventoryMovement(
                inventory_id=inventory.id,
                movement_type=InventoryMovementType.LOAN_OUT,
                quantity_delta=-entry.quantity,
                quantity_after=inventory.quantity_available,
                reference_type="gear_loan",
                reference_id=loan.id,
                reason=entry.notes or "Reservation checked out",
                actor_id=data.actor_id,
            )
            self.session.add_all([release_movement, loan_movement])
            self.audit(
                actor_id=data.actor_id,
                entity_type="gear_loan",
                entity_id=loan.id,
                action=AuditAction.LOANED,
                after=self.snapshot(loan),
                context={"reservation_id": reservation.id, "item_id": item.id},
                correlation_id=data.idempotency_key,
            )
            loans.append(loan)
        self.session.flush()
        self._finalize_checkout_state(reservation)
        apply_version(reservation, None)
        response = self._to_response(reservation)
        loan_responses = [GearLoanResponse.model_validate(loan) for loan in loans]
        self.save_idempotent(
            scope=scope,
            key=data.idempotency_key,
            payload=data,
            resource_type="gear_reservation",
            resource_id=reservation.id,
            response={
                "reservation": response.model_dump(mode="json"),
                "loan_ids": [loan.id for loan in loans],
            },
        )
        self.audit(
            actor_id=data.actor_id,
            entity_type="gear_reservation",
            entity_id=reservation.id,
            action=AuditAction.RESERVATION_CHECKED_OUT,
            after={"status": reservation.status},
            context={"loan_ids": [loan.id for loan in loans]},
            correlation_id=data.idempotency_key,
        )
        return response, loan_responses

    # ------------------------------------------------------------------ cancel

    def cancel(self, reservation_id: int, data: ReservationCancelRequest) -> ReservationResponse:
        scope = f"gear_reservation:{reservation_id}:cancel"
        prior = self.find_idempotent(scope=scope, key=data.idempotency_key, payload=data)
        if prior is not None:
            reservation = self.reservations.get_detail(prior.resource_id)
            if reservation is None:
                raise ConflictError("idempotency record references a missing reservation")
            return self._to_response(reservation)
        reservation = self.reservations.get_detail(reservation_id, for_update=True)
        if reservation is None:
            raise NotFoundError(f"GearReservation {reservation_id} was not found")
        self.users.require(data.actor_id)
        if reservation.status in _TERMINAL_STATUSES:
            raise InvalidStateError(
                f"reservation is already terminal (current: {reservation.status})"
            )
        apply_version(reservation, data.expected_version)
        # Drafts never froze stock, so only confirmed/partial reservations release units.
        if reservation.status == ReservationStatus.DRAFT:
            released_units = 0
        else:
            released_units = self._release_outstanding(reservation, actor_id=data.actor_id)
        reservation.status = ReservationStatus.CANCELLED
        reservation.cancelled_at = utc_now()
        reservation.cancel_reason = data.reason.strip()
        self.session.flush()
        response = self._to_response(reservation)
        self.save_idempotent(
            scope=scope,
            key=data.idempotency_key,
            payload=data,
            resource_type="gear_reservation",
            resource_id=reservation.id,
            response=response.model_dump(mode="json"),
        )
        self.audit(
            actor_id=data.actor_id,
            entity_type="gear_reservation",
            entity_id=reservation.id,
            action=AuditAction.RESERVATION_CANCELLED,
            after={"status": ReservationStatus.CANCELLED.value},
            context={"reason": data.reason, "released_units": released_units},
            correlation_id=data.idempotency_key,
        )
        return response

    # ------------------------------------------------------------------ expiry

    def expire_due(self, *, now: datetime | None = None) -> ReservationExpiryResult:
        """Release every confirmed/partial reservation whose expiry has passed."""
        current = now or utc_now()
        expired_ids: list[int] = []
        released_total = 0
        for reservation in self.reservations.expirable(current):
            released = self._release_outstanding(reservation, actor_id=None)
            reservation.status = ReservationStatus.EXPIRED
            reservation.expired_at = current
            apply_version(reservation, None)
            self.session.flush()
            expired_ids.append(reservation.id)
            released_total += released
            self.audit(
                actor_id=None,
                entity_type="gear_reservation",
                entity_id=reservation.id,
                action=AuditAction.RESERVATION_EXPIRED,
                after={"status": ReservationStatus.EXPIRED.value},
                context={"released_units": released, "expires_at": reservation.expires_at},
            )
        self.session.flush()
        return ReservationExpiryResult(
            expired_reservation_ids=expired_ids,
            released_units=released_total,
        )

    def cancel_open_for_expedition(
        self, expedition_id: int, *, reason: str, actor_id: int | None
    ) -> int:
        """Cancel drafts and release open reservations when the activity is cancelled."""
        released_count = 0
        for reservation in self.reservations.list_active_for_expedition(expedition_id):
            if reservation.status == ReservationStatus.DRAFT:
                released_units = 0
            else:
                released_units = self._release_outstanding(reservation, actor_id=actor_id)
            reservation.status = ReservationStatus.CANCELLED
            reservation.cancelled_at = utc_now()
            reservation.cancel_reason = reason
            apply_version(reservation, None)
            self.session.flush()
            released_count += 1
            self.audit(
                actor_id=actor_id,
                entity_type="gear_reservation",
                entity_id=reservation.id,
                action=AuditAction.RESERVATION_CANCELLED,
                after={"status": ReservationStatus.CANCELLED.value},
                context={
                    "reason": reason,
                    "cascade": "expedition_cancelled",
                    "released_units": released_units,
                },
            )
        return released_count

    # ------------------------------------------------------------------ helpers

    def _validate_allocations(
        self, data: ReservationDraftRequest
    ) -> list[tuple[GearInventory, int, str]]:
        seen: set[int] = set()
        allocations: list[tuple[GearInventory, int, str]] = []
        for line in data.items:
            if line.inventory_id in seen:
                raise ValidationError(
                    "each inventory may appear only once in a reservation batch",
                    context={"inventory_id": line.inventory_id},
                )
            seen.add(line.inventory_id)
            inventory = self.gear.get_inventory(line.inventory_id)
            if inventory is None:
                raise NotFoundError(f"GearInventory {line.inventory_id} was not found")
            if inventory.ownership != GearOwnership.CLUB:
                raise InventoryError(
                    "personal inventory cannot be reserved as shared activity gear",
                    context={"inventory_id": inventory.id, "ownership": inventory.ownership},
                )
            if inventory.condition in BLOCKED_CONDITIONS:
                raise InventoryError(
                    "damaged or retired gear cannot be allocated",
                    context={"inventory_id": inventory.id, "condition": inventory.condition},
                )
            allocations.append((inventory, line.quantity, line.notes))
        return allocations

    def _inventory_option(self, inventory: GearInventory) -> ReservationInventoryOption:
        blocked: str | None = None
        if inventory.ownership != GearOwnership.CLUB:
            blocked = "personal ownership is not reservable for group gear"
        elif inventory.condition in BLOCKED_CONDITIONS:
            blocked = f"inventory is {inventory.condition}"
        return ReservationInventoryOption(
            inventory_id=inventory.id,
            ownership=GearOwnership(inventory.ownership),
            condition=GearCondition(inventory.condition),
            quantity_total=inventory.quantity_total,
            quantity_available=inventory.quantity_available,
            storage_location=inventory.storage_location,
            allocatable=blocked is None,
            blocked_reason=blocked,
        )

    def _packed_quantities(self, expedition_id: int) -> dict[int, int]:
        packed: dict[int, int] = {}
        checks = self.session.scalars(
            select(ActivityGearCheck).where(ActivityGearCheck.expedition_id == expedition_id)
        )
        for check in checks:
            if check.status in {"packed", "verified"}:
                packed[check.catalog_id] = packed.get(check.catalog_id, 0) + check.quantity
        return packed

    def _release_outstanding(self, reservation: GearReservation, *, actor_id: int | None) -> int:
        """Return still-frozen units to the available pool.

        Only confirmed/partially checked-out reservations ever froze stock, so
        drafts never reach this path. The conditional UPDATE is authoritative:
        a concurrent checkout or a competing release cannot make this release
        return units twice.
        """
        expedition = self.session.get(Expedition, reservation.expedition_id)
        movement_actor = actor_id if actor_id is not None else (
            expedition.organizer_id if expedition is not None else None
        )
        if movement_actor is None:
            raise ValidationError("releasing a reservation requires an actor_id")
        released_total = 0
        for loaded in reservation.items:
            previously_released = loaded.released_quantity
            result = self.session.execute(
                update(GearReservationItem)
                .where(
                    GearReservationItem.id == loaded.id,
                    GearReservationItem.quantity
                    - GearReservationItem.checked_out_quantity
                    - GearReservationItem.released_quantity
                    > 0,
                )
                .values(
                    released_quantity=(
                        GearReservationItem.quantity - GearReservationItem.checked_out_quantity
                    )
                )
            )
            if result.rowcount != 1:
                continue
            item = self.reservations.get_item(loaded.id)
            if item is None:
                continue
            released = item.released_quantity - previously_released
            if released <= 0:
                continue
            inventory = self.gear.get_inventory(item.inventory_id, for_update=True)
            if inventory is None:
                raise NotFoundError(f"GearInventory {item.inventory_id} was not found")
            inventory.quantity_available += released
            apply_version(inventory, None)
            self.session.add(
                InventoryMovement(
                    inventory_id=inventory.id,
                    movement_type=InventoryMovementType.RESERVATION_RELEASED,
                    quantity_delta=released,
                    quantity_after=inventory.quantity_available,
                    reference_type="gear_reservation",
                    reference_id=reservation.id,
                    reason=f"Reservation {reservation.status} released",
                    actor_id=movement_actor,
                )
            )
            loaded.released_quantity = item.released_quantity
            released_total += released
        self.session.flush()
        return released_total

    def _finalize_checkout_state(self, reservation: GearReservation) -> None:
        outstanding_total = sum(self._outstanding(item) for item in reservation.items)
        if outstanding_total == 0:
            reservation.status = ReservationStatus.CHECKED_OUT
            reservation.checked_out_at = utc_now()
        else:
            reservation.status = ReservationStatus.PARTIALLY_CHECKED_OUT

    @staticmethod
    def _outstanding(item: GearReservationItem) -> int:
        return item.quantity - item.checked_out_quantity - item.released_quantity

    def _to_response(self, reservation: GearReservation) -> ReservationResponse:
        items = [
            ReservationItemResponse(
                id=item.id,
                created_at=item.created_at,
                updated_at=item.updated_at,
                reservation_id=item.reservation_id,
                inventory_id=item.inventory_id,
                catalog_id=item.catalog_id,
                quantity=item.quantity,
                checked_out_quantity=item.checked_out_quantity,
                released_quantity=item.released_quantity,
                outstanding_quantity=self._outstanding(item),
                notes=item.notes,
            )
            for item in sorted(reservation.items, key=lambda item: item.id)
        ]
        return ReservationResponse(
            id=reservation.id,
            created_at=reservation.created_at,
            updated_at=reservation.updated_at,
            version=reservation.version,
            expedition_id=reservation.expedition_id,
            status=ReservationStatus(reservation.status),
            draft_key=reservation.draft_key,
            expires_at=reservation.expires_at,
            confirmed_at=reservation.confirmed_at,
            checked_out_at=reservation.checked_out_at,
            cancelled_at=reservation.cancelled_at,
            expired_at=reservation.expired_at,
            cancel_reason=reservation.cancel_reason,
            notes=reservation.notes,
            items=items,
        )
