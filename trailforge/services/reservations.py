from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from trailforge.database.base import utc_now
from trailforge.domain.enums import (
    AuditAction,
    ChecklistStatus,
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
    ActivityGearRequirement,
    GearInventory,
    GearLoan,
    GearReservation,
    GearReservationItem,
    InventoryMovement,
)
from trailforge.repositories.activities import ExpeditionRepository
from trailforge.repositories.base import apply_version
from trailforge.repositories.gear import OPEN_RESERVATION_STATUSES, GearRepository
from trailforge.repositories.users import UserRepository
from trailforge.schemas.gear import (
    ReservationAllocationLine,
    ReservationCancelRequest,
    ReservationCapacityDelta,
    ReservationCapacityDeltaLine,
    ReservationConfirmRequest,
    ReservationDraftCreate,
    ReservationFulfilmentLine,
    ReservationFulfilRequest,
    ReservationInventoryOption,
    ReservationItemResponse,
    ReservationProposal,
    ReservationProposalLine,
    ReservationResponse,
)
from trailforge.services.base import ServiceBase

UNALLOCATABLE_CONDITIONS = {GearCondition.DAMAGED, GearCondition.RETIRED}


class ReservationService(ServiceBase):
    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.gear = GearRepository(session)
        self.users = UserRepository(session)
        self.expeditions = ExpeditionRepository(session)

    # -- queries ----------------------------------------------------------

    def get(self, reservation_id: int) -> ReservationResponse:
        reservation = self._require(reservation_id)
        return self._to_response(reservation)

    def list(
        self,
        *,
        expedition_id: int | None = None,
        status: ReservationStatus | None = None,
    ) -> list[ReservationResponse]:
        reservations = self.gear.list_reservations(expedition_id=expedition_id, status=status)
        return [self._to_response(item) for item in reservations]

    def proposal(self, expedition_id: int) -> ReservationProposal:
        expedition = self._require_expedition(expedition_id)
        participants = self.gear.participant_count(expedition_id)
        packed = self._packed_quantities(expedition_id)
        existing = self.gear.get_open_reservation(expedition_id)
        lines: list[ReservationProposalLine] = []
        for requirement in self.gear.requirements(expedition_id):
            catalog = self.gear.get_catalog(requirement.catalog_id)
            if catalog is None:
                continue
            packed_quantity = packed.get(catalog.id, 0)
            personal_required = requirement.quantity_per_person * participants
            if requirement.quantity_for_group > 0:
                surplus_packed = max(packed_quantity - personal_required, 0)
                lines.append(
                    self._group_proposal_line(
                        catalog=catalog,
                        mandatory=requirement.mandatory,
                        required=requirement.quantity_for_group,
                        expedition_id=expedition_id,
                        packed_quantity=surplus_packed,
                    )
                )
            if personal_required > 0:
                lines.append(
                    self._personal_proposal_line(
                        catalog=catalog,
                        mandatory=requirement.mandatory,
                        required=personal_required,
                        packed_quantity=min(packed_quantity, personal_required),
                    )
                )
        return ReservationProposal(
            expedition_id=expedition.id,
            participant_count=participants,
            generated_at=utc_now(),
            existing_open_reservation_id=existing.id if existing is not None else None,
            lines=lines,
            fully_coverable=all(line.missing_quantity == 0 for line in lines),
        )

    def _inventory_options(self, catalog_id: int) -> tuple[list[ReservationInventoryOption], int]:
        options: list[ReservationInventoryOption] = []
        club_available = 0
        for inventory in self.gear.list_inventory_for_catalog(catalog_id):
            blocked: str | None = None
            if inventory.ownership == GearOwnership.PERSONAL:
                blocked = "personal inventory is tied to its owner and cannot be reserved"
            elif inventory.condition in UNALLOCATABLE_CONDITIONS:
                blocked = f"{inventory.condition} gear cannot be allocated"
            if blocked is None:
                club_available += inventory.quantity_available
            options.append(
                ReservationInventoryOption(
                    inventory_id=inventory.id,
                    ownership=inventory.ownership,
                    owner_id=inventory.owner_id,
                    condition=inventory.condition,
                    quantity_total=inventory.quantity_total,
                    quantity_available=inventory.quantity_available,
                    allocatable=blocked is None,
                    blocked_reason=blocked,
                )
            )
        return options, club_available

    def _group_proposal_line(
        self,
        *,
        catalog,
        mandatory: bool,
        required: int,
        expedition_id: int,
        packed_quantity: int,
    ) -> ReservationProposalLine:
        options, club_available = self._inventory_options(catalog.id)
        already_reserved = self.gear.reservation_covered_for_catalog(expedition_id, catalog.id)
        outstanding = max(required - packed_quantity - already_reserved, 0)
        suggested = min(outstanding, club_available)
        return ReservationProposalLine(
            catalog_id=catalog.id,
            sku=catalog.sku,
            name=catalog.name,
            mandatory=mandatory,
            requirement_source="group",
            required_quantity=required,
            club_available_quantity=club_available,
            personal_covered_quantity=0,
            missing_quantity=max(outstanding - club_available, 0),
            options=options,
            suggested_reservation_quantity=suggested,
        )

    def _personal_proposal_line(
        self,
        *,
        catalog,
        mandatory: bool,
        required: int,
        packed_quantity: int,
    ) -> ReservationProposalLine:
        # Personal requirements follow owner-tied inventory and are never
        # reserved from the club pool; they are covered by packed checks.
        covered = min(packed_quantity, required)
        options, _ = self._inventory_options(catalog.id)
        return ReservationProposalLine(
            catalog_id=catalog.id,
            sku=catalog.sku,
            name=catalog.name,
            mandatory=mandatory,
            requirement_source="personal",
            required_quantity=required,
            club_available_quantity=0,
            personal_covered_quantity=covered,
            missing_quantity=max(required - covered, 0),
            options=options,
            suggested_reservation_quantity=0,
        )

    def capacity_delta(self, expedition_id: int) -> ReservationCapacityDelta:
        expedition = self._require_expedition(expedition_id)
        reservation = self.gear.get_open_reservation(expedition_id)
        participants_after = self.gear.participant_count(expedition_id)
        if reservation is None:
            raise NotFoundError(
                "no open reservation exists for this expedition",
                context={"expedition_id": expedition_id},
            )
        participants_before = reservation.participant_snapshot
        # Per-catalog snapshot of group requirement captured when the
        # reservation was drafted; confirmed reservations are never mutated
        # silently, so this remains the "before" baseline.
        group_required_before: dict[int, int] = {}
        reserved_by_catalog: dict[int, int] = {}
        for item in self.gear.reservation_items(reservation.id):
            group_required_before[item.catalog_id] = item.quantity_required
            reserved_by_catalog[item.catalog_id] = (
                reserved_by_catalog.get(item.catalog_id, 0) + item.quantity_reserved
            )
        lines: list[ReservationCapacityDeltaLine] = []
        for requirement in self.gear.requirements(expedition_id):
            catalog = self.gear.get_catalog(requirement.catalog_id)
            if catalog is None:
                continue
            group_before = group_required_before.get(catalog.id, requirement.quantity_for_group)
            group_after = requirement.quantity_for_group
            reserved = reserved_by_catalog.get(catalog.id, 0)
            lines.append(
                ReservationCapacityDeltaLine(
                    catalog_id=catalog.id,
                    sku=catalog.sku,
                    name=catalog.name,
                    requirement_source="group",
                    required_before=group_before,
                    required_after=group_after,
                    required_delta=group_after - group_before,
                    reserved_quantity=reserved,
                    newly_missing_quantity=max(group_after - reserved, 0),
                    releasable_over_reservation=max(reserved - group_after, 0),
                )
            )
            personal_before = requirement.quantity_per_person * participants_before
            personal_after = requirement.quantity_per_person * participants_after
            if personal_before > 0 or personal_after > 0:
                lines.append(
                    ReservationCapacityDeltaLine(
                        catalog_id=catalog.id,
                        sku=catalog.sku,
                        name=catalog.name,
                        requirement_source="personal",
                        required_before=personal_before,
                        required_after=personal_after,
                        required_delta=personal_after - personal_before,
                        reserved_quantity=0,
                        # Personal gear follows owner-tied inventory and is
                        # covered by packing checks, never by club holds.
                        newly_missing_quantity=0,
                        releasable_over_reservation=0,
                    )
                )
        return ReservationCapacityDelta(
            expedition_id=expedition.id,
            participant_count_before=participants_before,
            participant_count_after=participants_after,
            reservation_id=reservation.id,
            reservation_status=ReservationStatus(reservation.status),
            generated_at=utc_now(),
            lines=lines,
        )

    # -- mutations --------------------------------------------------------

    def create_draft(
        self, expedition_id: int, data: ReservationDraftCreate
    ) -> ReservationResponse:
        scope = f"expedition:{expedition_id}:reservation:draft"
        prior = self.find_idempotent(scope=scope, key=data.idempotency_key, payload=data)
        if prior is not None:
            return self._to_response(self._require(prior.resource_id))
        expedition = self._require_expedition(expedition_id)
        self.users.require(data.actor_id)
        if data.expires_at <= utc_now():
            raise ValidationError("expires_at must be in the future")
        existing = self.gear.get_open_reservation(expedition_id)
        if existing is not None:
            raise ConflictError(
                "an open reservation already exists for this expedition",
                context={"reservation_id": existing.id, "status": str(existing.status)},
            )
        requirements = {
            item.catalog_id: item for item in self.gear.requirements(expedition_id)
        }
        items: list[GearReservationItem] = []
        allocated_per_catalog: dict[int, int] = {}
        seen_pairs: set[tuple[int, int]] = set()
        for line in data.items:
            self._validate_draft_line(
                expedition_id,
                requirements,
                line,
                allocated_per_catalog,
                seen_pairs,
            )
        reservation = GearReservation(
            expedition_id=expedition.id,
            status=ReservationStatus.DRAFT,
            participant_snapshot=self.gear.participant_count(expedition_id),
            expires_at=data.expires_at,
            created_by=data.actor_id,
        )
        self.session.add(reservation)
        self.session.flush()
        for line in data.items:
            requirement = requirements[line.catalog_id]
            items.append(
                GearReservationItem(
                    reservation_id=reservation.id,
                    expedition_id=expedition.id,
                    catalog_id=line.catalog_id,
                    inventory_id=line.inventory_id,
                    ownership=GearOwnership.CLUB,
                    quantity_required=requirement.quantity_for_group,
                    quantity_reserved=line.quantity_reserved,
                    requirement_source="group",
                )
            )
        self.session.add_all(items)
        self.session.flush()
        response = self._to_response(reservation, items)
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
            action=AuditAction.RESERVATION_DRAFTED,
            after=self.snapshot(reservation),
            context={"expedition_id": expedition.id, "items": len(items)},
            correlation_id=data.idempotency_key,
        )
        return response

    def _validate_draft_line(
        self,
        expedition_id: int,
        requirements: dict[int, ActivityGearRequirement],
        line: ReservationAllocationLine,
        allocated_per_catalog: dict[int, int],
        seen_pairs: set[tuple[int, int]],
    ) -> None:
        requirement = requirements.get(line.catalog_id)
        if requirement is None:
            raise ValidationError(
                "catalog item is not required by this expedition",
                context={"catalog_id": line.catalog_id},
            )
        if requirement.quantity_for_group <= 0:
            raise ValidationError(
                "only group (club) requirements can be reserved from inventory",
                context={"catalog_id": line.catalog_id},
            )
        inventory = self.gear.get_inventory(line.inventory_id)
        if inventory is None:
            raise NotFoundError(f"GearInventory {line.inventory_id} was not found")
        if inventory.catalog_id != line.catalog_id:
            raise ValidationError(
                "inventory row does not belong to the catalog item",
                context={"inventory_id": line.inventory_id, "catalog_id": line.catalog_id},
            )
        if inventory.ownership != GearOwnership.CLUB:
            raise InventoryError(
                "personal inventory cannot be reserved for an activity",
                context={"inventory_id": line.inventory_id, "ownership": str(inventory.ownership)},
            )
        if inventory.condition in UNALLOCATABLE_CONDITIONS:
            raise InventoryError(
                "damaged or retired gear cannot be allocated",
                context={"inventory_id": line.inventory_id, "condition": str(inventory.condition)},
            )
        if inventory.quantity_available < line.quantity_reserved:
            raise InventoryError(
                "insufficient available inventory for draft allocation",
                context={
                    "inventory_id": line.inventory_id,
                    "available": inventory.quantity_available,
                    "requested": line.quantity_reserved,
                },
            )
        pair = (line.catalog_id, line.inventory_id)
        if pair in seen_pairs:
            raise ValidationError(
                "duplicate allocation line for same catalog and inventory",
                context={"catalog_id": line.catalog_id, "inventory_id": line.inventory_id},
            )
        seen_pairs.add(pair)
        total = allocated_per_catalog.get(line.catalog_id, 0) + line.quantity_reserved
        if total > requirement.quantity_for_group:
            raise InventoryError(
                "allocation exceeds the group requirement",
                context={
                    "catalog_id": line.catalog_id,
                    "required": requirement.quantity_for_group,
                    "allocated": total,
                },
            )
        other_held = self.gear.reservation_covered_for_catalog(expedition_id, line.catalog_id)
        if other_held:
            raise ConflictError(
                "catalog item is already covered by another reservation",
                context={"catalog_id": line.catalog_id, "reserved_quantity": other_held},
            )
        allocated_per_catalog[line.catalog_id] = total

    def confirm(self, reservation_id: int, data: ReservationConfirmRequest) -> ReservationResponse:
        scope = f"gear:reservation:{reservation_id}:confirm"
        prior = self.find_idempotent(scope=scope, key=data.idempotency_key, payload=data)
        if prior is not None:
            return self._to_response(self._require(prior.resource_id))
        reservation = self._require(reservation_id, for_update=True)
        self.users.require(data.actor_id)
        current = ReservationStatus(reservation.status)
        if current != ReservationStatus.DRAFT:
            raise InvalidStateError(
                f"only draft reservations can be confirmed (current: {current})",
                context={"current": current.value},
            )
        if reservation.expires_at <= utc_now():
            raise InvalidStateError("reservation draft has expired")
        apply_version(reservation, data.expected_version)
        items = self.gear.reservation_items(reservation_id, for_update=True)
        # Lock every affected inventory row in a stable order before any mutation
        # so concurrent confirmations serialize on the same rows.
        inventories = self._lock_inventories(sorted({item.inventory_id for item in items}))
        inventory_by_id = {item.id: item for item in inventories}

        def _freeze_batch() -> None:
            # SAVEPOINT makes the batch atomic even for direct (non-HTTP) callers:
            # a shortfall on any line rolls back every earlier hold in the batch.
            for item in items:
                inventory = inventory_by_id[item.inventory_id]
                if inventory.condition in UNALLOCATABLE_CONDITIONS:
                    raise InventoryError(
                        "damaged or retired gear cannot be allocated",
                        context={
                            "inventory_id": inventory.id,
                            "condition": str(inventory.condition),
                        },
                    )
                new_available = self.gear.decrement_available_returning(
                    inventory.id, item.quantity_reserved
                )
                if new_available is None:
                    raise InventoryError(
                        "insufficient available inventory to confirm the reservation",
                        context={
                            "inventory_id": inventory.id,
                            "available": inventory.quantity_available,
                            "requested": item.quantity_reserved,
                        },
                    )
                self.session.add(
                    InventoryMovement(
                        inventory_id=inventory.id,
                        movement_type=InventoryMovementType.RESERVATION_HOLD,
                        quantity_delta=-item.quantity_reserved,
                        quantity_after=new_available,
                        reference_type="gear_reservation",
                        reference_id=reservation.id,
                        reason=f"Hold for expedition {reservation.expedition_id}",
                        actor_id=data.actor_id,
                    )
                )
            reservation.status = ReservationStatus.CONFIRMED
            reservation.confirmed_at = utc_now()
            apply_version(reservation, None)
            self.session.flush()

        with self.session.begin_nested():
            _freeze_batch()
        response = self._to_response(reservation, items)
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
            context={"items": len(items)},
            correlation_id=data.idempotency_key,
        )
        return response

    def fulfil(self, reservation_id: int, data: ReservationFulfilRequest) -> ReservationResponse:
        scope = f"gear:reservation:{reservation_id}:fulfil"
        prior = self.find_idempotent(scope=scope, key=data.idempotency_key, payload=data)
        if prior is not None:
            return self._to_response(self._require(prior.resource_id))
        reservation = self._require(reservation_id, for_update=True)
        self.users.require(data.actor_id)
        current = ReservationStatus(reservation.status)
        if current not in {
            ReservationStatus.CONFIRMED,
            ReservationStatus.PARTIALLY_FULFILLED,
        }:
            raise InvalidStateError(
                f"reservation in status {current} cannot be fulfilled",
                context={"current": current.value},
            )
        if reservation.expires_at <= utc_now():
            raise InvalidStateError("reservation has expired and must be renewed before fulfilment")
        locked_items = {
            item.id: item for item in self.gear.reservation_items(reservation_id, for_update=True)
        }
        for line in data.lines:
            if line.reservation_item_id not in locked_items:
                raise NotFoundError(
                    f"GearReservationItem {line.reservation_item_id} was not found",
                    context={"reservation_id": reservation_id},
                )
        inventory_ids = sorted(
            {locked_items[line.reservation_item_id].inventory_id for line in data.lines}
        )
        inventory_by_id = {
            inventory.id: inventory for inventory in self._lock_inventories(inventory_ids)
        }
        loans: list[GearLoan] = []
        now = utc_now()
        seen_borrowers: set[tuple[int, int]] = set()

        def _fulfil_batch() -> None:
            for line in data.lines:
                loans.append(
                    self._fulfil_line(
                        reservation,
                        locked_items,
                        inventory_by_id,
                        line,
                        now,
                        actor_id=data.actor_id,
                        seen_borrowers=seen_borrowers,
                    )
                )
            self.session.flush()
            self._refresh_fulfilment_status(reservation, locked_items.values())
            self.session.flush()

        # SAVEPOINT: every loan/movement in the request commits together.
        with self.session.begin_nested():
            _fulfil_batch()
        all_items = list(locked_items.values())
        response = self._to_response(reservation, all_items)
        self.save_idempotent(
            scope=scope,
            key=data.idempotency_key,
            payload=data,
            resource_type="gear_reservation",
            resource_id=reservation.id,
            response=response.model_dump(mode="json"),
        )
        for loan in loans:
            self.audit(
                actor_id=data.actor_id,
                entity_type="gear_loan",
                entity_id=loan.id,
                action=AuditAction.LOANED,
                after=self.snapshot(loan),
                context={"reservation_id": reservation.id},
                correlation_id=data.idempotency_key,
            )
        self.audit(
            actor_id=data.actor_id,
            entity_type="gear_reservation",
            entity_id=reservation.id,
            action=AuditAction.RESERVATION_FULFILLED,
            after={"status": str(reservation.status)},
            context={"loans": len(loans)},
            correlation_id=data.idempotency_key,
        )
        return response

    def _fulfil_line(
        self,
        reservation: GearReservation,
        locked_items: dict[int, GearReservationItem],
        inventory_by_id: dict[int, GearInventory],
        line: ReservationFulfilmentLine,
        now: datetime,
        *,
        actor_id: int,
        seen_borrowers: set[tuple[int, int]],
    ) -> GearLoan:
        item = locked_items.get(line.reservation_item_id)
        if item is None or item.reservation_id != reservation.id:
            raise NotFoundError(
                f"GearReservationItem {line.reservation_item_id} was not found",
                context={"reservation_id": reservation.id},
            )
        remaining = item.quantity_reserved - item.quantity_fulfilled
        if remaining <= 0:
            raise ConflictError(
                "reservation item is already fully fulfilled",
                context={"reservation_item_id": item.id},
            )
        if line.quantity > remaining:
            raise InventoryError(
                "fulfilment quantity exceeds the outstanding reservation",
                context={
                    "reservation_item_id": item.id,
                    "outstanding": remaining,
                    "requested": line.quantity,
                },
            )
        self.users.require(line.borrower_id)
        inventory = inventory_by_id[item.inventory_id]
        if inventory.condition in UNALLOCATABLE_CONDITIONS:
            raise InventoryError(
                "damaged or retired gear cannot be loaned",
                context={"inventory_id": inventory.id, "condition": str(inventory.condition)},
            )
        borrower_key = (inventory.id, line.borrower_id)
        if borrower_key in seen_borrowers:
            raise ConflictError(
                "borrower appears on multiple fulfilment lines for the same inventory",
                context={"inventory_id": inventory.id, "borrower_id": line.borrower_id},
            )
        # A borrower collecting in several partial fulfilments against the same
        # reservation item accumulates onto one loan rather than opening a
        # duplicate active loan for the inventory row.
        existing_loan = self.gear.active_reservation_loan(item.id, line.borrower_id)
        if (
            existing_loan is None
            and self.gear.active_loan(inventory.id, line.borrower_id) is not None
        ):
            raise ConflictError(
                "borrower already has an active loan for this inventory",
                context={"inventory_id": inventory.id, "borrower_id": line.borrower_id},
            )
        seen_borrowers.add(borrower_key)
        loaned_at = line.loaned_at or now
        due_at = line.due_at
        if due_at is None or due_at <= loaned_at:
            from datetime import timedelta

            # Fall back to one week out when no valid window is supplied
            # (e.g. the reservation expiry has nearly elapsed).
            due_at = loaned_at + timedelta(days=7)
        if existing_loan is not None:
            existing_loan.quantity += line.quantity
            loan = existing_loan
        else:
            loan = GearLoan(
                inventory_id=inventory.id,
                borrower_id=line.borrower_id,
                expedition_id=reservation.expedition_id,
                quantity=line.quantity,
                loaned_at=loaned_at,
                due_at=due_at,
                condition_out=inventory.condition,
                notes=line.notes,
                reservation_item_id=item.id,
            )
        self.session.add(loan)
        self.session.flush()
        # Convert the frozen hold into a loan: release the hold then loan out,
        # both as relative SQL updates so concurrent writers never lose rows.
        # Net change to quantity_available is zero.
        released_available = self.gear.increment_available_returning(
            inventory.id, line.quantity
        )
        self.session.add(
            InventoryMovement(
                inventory_id=inventory.id,
                movement_type=InventoryMovementType.RESERVATION_RELEASE,
                quantity_delta=line.quantity,
                quantity_after=released_available,
                reference_type="gear_reservation",
                reference_id=reservation.id,
                reason=f"Hold converted to loan for expedition {reservation.expedition_id}",
                actor_id=actor_id,
            )
        )
        # The released hold guarantees enough stock, but enforce the
        # non-negative invariant at SQL level too.
        loaned_available = self.gear.decrement_available_returning(inventory.id, line.quantity)
        if loaned_available is None:
            raise InventoryError(
                "inventory became unavailable during fulfilment",
                context={"inventory_id": inventory.id},
            )
        self.session.add(
            InventoryMovement(
                inventory_id=inventory.id,
                movement_type=InventoryMovementType.LOAN_OUT,
                quantity_delta=-line.quantity,
                quantity_after=loaned_available,
                reference_type="gear_loan",
                reference_id=loan.id,
                reason=f"Fulfil reservation for expedition {reservation.expedition_id}",
                actor_id=actor_id,
            )
        )
        item.quantity_fulfilled += line.quantity
        return loan

    def cancel(self, reservation_id: int, data: ReservationCancelRequest) -> ReservationResponse:
        scope = f"gear:reservation:{reservation_id}:cancel"
        prior = self.find_idempotent(scope=scope, key=data.idempotency_key, payload=data)
        if prior is not None:
            return self._to_response(self._require(prior.resource_id))
        reservation = self._require(reservation_id, for_update=True)
        self.users.require(data.actor_id)
        current = ReservationStatus(reservation.status)
        if current not in {
            ReservationStatus.DRAFT,
            ReservationStatus.CONFIRMED,
            ReservationStatus.PARTIALLY_FULFILLED,
        }:
            raise InvalidStateError(
                f"reservation in status {current} cannot be cancelled",
                context={"current": current.value},
            )
        released = 0
        if current in {
            ReservationStatus.CONFIRMED,
            ReservationStatus.PARTIALLY_FULFILLED,
        }:
            released = self._release_holds(
                reservation, actor_id=data.actor_id, reason=data.reason
            )
        reservation.status = ReservationStatus.CANCELLED
        reservation.cancelled_at = utc_now()
        reservation.cancel_reason = data.reason
        apply_version(reservation, None)
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
            before={"status": current.value},
            after={"status": ReservationStatus.CANCELLED.value},
            context={"reason": data.reason, "released_quantity": released},
            correlation_id=data.idempotency_key,
        )
        return response

    def expire_due(
        self, *, actor_id: int, now: datetime | None = None, idempotency_key: str | None = None
    ) -> list[ReservationResponse]:
        current_time = now or utc_now()
        self.users.require(actor_id)
        if idempotency_key is not None:
            scope = "gear:reservations:expire-due"
            prior = self.find_idempotent(
                scope=scope,
                key=idempotency_key,
                payload={"at": current_time.isoformat()},
            )
            if prior is not None:
                expired_ids = prior.response_json.get("expired", [prior.resource_id])
                return [self.get(reservation_id) for reservation_id in expired_ids]
        due = self.gear.expirable_reservations(current_time)
        responses: list[ReservationResponse] = []
        for reservation in due:
            current = ReservationStatus(reservation.status)
            released = 0
            if current in {
                ReservationStatus.CONFIRMED,
                ReservationStatus.PARTIALLY_FULFILLED,
            }:
                released = self._release_holds(
                    reservation,
                    actor_id=actor_id,
                    reason=f"Expired at {reservation.expires_at.isoformat()}",
                )
            reservation.status = ReservationStatus.EXPIRED
            reservation.cancelled_at = current_time
            reservation.cancel_reason = "expired"
            apply_version(reservation, None)
            self.session.flush()
            responses.append(self._to_response(reservation))
            self.audit(
                actor_id=actor_id,
                entity_type="gear_reservation",
                entity_id=reservation.id,
                action=AuditAction.RESERVATION_EXPIRED,
                before={"status": current.value},
                after={"status": ReservationStatus.EXPIRED.value},
                context={"released_quantity": released},
            )
        if idempotency_key is not None and due:
            self.save_idempotent(
                scope="gear:reservations:expire-due",
                key=idempotency_key,
                payload={"at": current_time.isoformat()},
                resource_type="gear_reservation_batch",
                resource_id=due[0].id,
                response={"expired": [item.id for item in due]},
            )
        return responses

    def release_open_for_expedition(
        self, expedition_id: int, *, actor_id: int, reason: str
    ) -> int:
        """Release every open reservation of an expedition (e.g. expedition cancellation)."""
        changed = 0
        for reservation in self.gear.list_reservations(expedition_id=expedition_id):
            status = ReservationStatus(reservation.status)
            if status not in OPEN_RESERVATION_STATUSES:
                continue
            released = 0
            if status in {
                ReservationStatus.CONFIRMED,
                ReservationStatus.PARTIALLY_FULFILLED,
            }:
                released = self._release_holds(
                    reservation, actor_id=actor_id, reason=reason
                )
            reservation.status = ReservationStatus.CANCELLED
            reservation.cancelled_at = utc_now()
            reservation.cancel_reason = reason
            apply_version(reservation, None)
            self.session.flush()
            changed += 1
            self.audit(
                actor_id=actor_id,
                entity_type="gear_reservation",
                entity_id=reservation.id,
                action=AuditAction.RESERVATION_CANCELLED,
                before={"status": status.value},
                after={"status": ReservationStatus.CANCELLED.value},
                context={"reason": reason, "cascade": "expedition_cancelled",
                         "released_quantity": released},
            )
        return changed

    # -- helpers ----------------------------------------------------------

    def _release_holds(
        self, reservation: GearReservation, *, actor_id: int, reason: str
    ) -> int:
        items = self.gear.reservation_items(reservation.id, for_update=True)
        if not items:
            return 0
        inventory_ids = sorted({item.inventory_id for item in items})
        inventory_by_id = {
            inventory.id: inventory for inventory in self._lock_inventories(inventory_ids)
        }
        released_total = 0
        for item in items:
            outstanding = item.quantity_reserved - item.quantity_fulfilled
            if outstanding <= 0:
                continue
            inventory = inventory_by_id[item.inventory_id]
            new_available = self.gear.increment_available_returning(inventory.id, outstanding)
            self.session.add(
                InventoryMovement(
                    inventory_id=inventory.id,
                    movement_type=InventoryMovementType.RESERVATION_RELEASE,
                    quantity_delta=outstanding,
                    quantity_after=new_available,
                    reference_type="gear_reservation",
                    reference_id=reservation.id,
                    reason=reason,
                    actor_id=actor_id,
                )
            )
            released_total += outstanding
        return released_total

    def _refresh_fulfilment_status(
        self,
        reservation: GearReservation,
        items: object,
    ) -> None:
        item_list = list(items)
        total_reserved = sum(item.quantity_reserved for item in item_list)
        total_fulfilled = sum(item.quantity_fulfilled for item in item_list)
        if total_fulfilled >= total_reserved:
            reservation.status = ReservationStatus.FULFILLED
        elif total_fulfilled > 0:
            reservation.status = ReservationStatus.PARTIALLY_FULFILLED
        apply_version(reservation, None)

    def _lock_inventories(self, inventory_ids: list[int]) -> list[GearInventory]:
        locked: list[GearInventory] = []
        for inventory_id in inventory_ids:
            inventory = self.gear.get_inventory(inventory_id, for_update=True)
            if inventory is None:
                raise NotFoundError(f"GearInventory {inventory_id} was not found")
            locked.append(inventory)
        return locked

    def _packed_quantities(self, expedition_id: int) -> dict[int, int]:
        packed: dict[int, int] = {}
        accepted = {ChecklistStatus.PACKED, ChecklistStatus.VERIFIED}
        for check in self.gear.gear_checks(expedition_id):
            if check.status in accepted:
                packed[check.catalog_id] = packed.get(check.catalog_id, 0) + check.quantity
        return packed

    def _require_expedition(self, expedition_id: int) -> Expedition:
        expedition = self.expeditions.get(expedition_id)
        if expedition is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        return expedition

    def _require(self, reservation_id: int, *, for_update: bool = False) -> GearReservation:
        reservation = self.gear.get_reservation(reservation_id, for_update=for_update)
        if reservation is None:
            raise NotFoundError(f"GearReservation {reservation_id} was not found")
        return reservation

    def _to_response(
        self,
        reservation: GearReservation,
        items: list[GearReservationItem] | None = None,
    ) -> ReservationResponse:
        if items is None:
            items = self.gear.reservation_items(reservation.id)
        return ReservationResponse(
            id=reservation.id,
            created_at=reservation.created_at,
            updated_at=reservation.updated_at,
            version=reservation.version,
            expedition_id=reservation.expedition_id,
            status=ReservationStatus(reservation.status),
            participant_snapshot=reservation.participant_snapshot,
            expires_at=reservation.expires_at,
            confirmed_at=reservation.confirmed_at,
            cancelled_at=reservation.cancelled_at,
            cancel_reason=reservation.cancel_reason,
            created_by=reservation.created_by,
            items=[
                ReservationItemResponse(
                    id=item.id,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    reservation_id=item.reservation_id,
                    expedition_id=item.expedition_id,
                    catalog_id=item.catalog_id,
                    inventory_id=item.inventory_id,
                    ownership=item.ownership,
                    quantity_required=item.quantity_required,
                    quantity_reserved=item.quantity_reserved,
                    quantity_fulfilled=item.quantity_fulfilled,
                    requirement_source=item.requirement_source,
                )
                for item in items
            ],
        )
