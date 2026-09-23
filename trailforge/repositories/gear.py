from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update

from trailforge.domain.enums import (
    LoanStatus,
    RegistrationStatus,
    ReservationStatus,
)
from trailforge.models.activities import ExpeditionRegistration
from trailforge.models.gear import (
    ActivityGearCheck,
    ActivityGearRequirement,
    GearCatalog,
    GearInventory,
    GearLoan,
    GearReservation,
    GearReservationItem,
    InventoryMovement,
)
from trailforge.repositories.base import BaseRepository

OPEN_RESERVATION_STATUSES = {
    ReservationStatus.DRAFT,
    ReservationStatus.CONFIRMED,
    ReservationStatus.PARTIALLY_FULFILLED,
}

RESERVATION_COVERED_STATUSES = {
    ReservationStatus.CONFIRMED,
    ReservationStatus.PARTIALLY_FULFILLED,
    ReservationStatus.FULFILLED,
}


class GearRepository(BaseRepository[GearInventory]):
    model = GearInventory
    sortable = {
        "created_at": GearInventory.created_at,
        "updated_at": GearInventory.updated_at,
        "quantity_available": GearInventory.quantity_available,
        "condition": GearInventory.condition,
    }

    def get_catalog(self, catalog_id: int) -> GearCatalog | None:
        return self.session.get(GearCatalog, catalog_id)

    def get_catalog_by_sku(self, sku: str) -> GearCatalog | None:
        return self.session.scalar(select(GearCatalog).where(GearCatalog.sku == sku.upper()))

    def list_catalog(self, category: str | None = None) -> list[GearCatalog]:
        statement = select(GearCatalog)
        if category:
            statement = statement.where(GearCatalog.category == category)
        return list(
            self.session.scalars(statement.order_by(GearCatalog.category, GearCatalog.name))
        )

    def get_inventory(self, inventory_id: int, *, for_update: bool = False) -> GearInventory | None:
        statement = select(GearInventory).where(GearInventory.id == inventory_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def get_loan(self, loan_id: int, *, for_update: bool = False) -> GearLoan | None:
        statement = select(GearLoan).where(GearLoan.id == loan_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def active_loan(self, inventory_id: int, borrower_id: int) -> GearLoan | None:
        return self.session.scalar(
            select(GearLoan).where(
                GearLoan.inventory_id == inventory_id,
                GearLoan.borrower_id == borrower_id,
                GearLoan.status.in_({LoanStatus.ACTIVE, LoanStatus.OVERDUE}),
            )
        )

    def active_reservation_loan(
        self, reservation_item_id: int, borrower_id: int
    ) -> GearLoan | None:
        return self.session.scalar(
            select(GearLoan).where(
                GearLoan.reservation_item_id == reservation_item_id,
                GearLoan.borrower_id == borrower_id,
                GearLoan.status.in_({LoanStatus.ACTIVE, LoanStatus.OVERDUE}),
            )
        )

    def list_loans(
        self,
        *,
        borrower_id: int | None = None,
        status: LoanStatus | None = None,
    ) -> list[GearLoan]:
        statement = select(GearLoan)
        if borrower_id is not None:
            statement = statement.where(GearLoan.borrower_id == borrower_id)
        if status is not None:
            statement = statement.where(GearLoan.status == status)
        return list(self.session.scalars(statement.order_by(GearLoan.due_at)))

    def requirements(self, expedition_id: int) -> list[ActivityGearRequirement]:
        return list(
            self.session.scalars(
                select(ActivityGearRequirement)
                .where(ActivityGearRequirement.expedition_id == expedition_id)
                .order_by(ActivityGearRequirement.catalog_id)
            )
        )

    def get_requirement(
        self, expedition_id: int, catalog_id: int
    ) -> ActivityGearRequirement | None:
        return self.session.scalar(
            select(ActivityGearRequirement).where(
                ActivityGearRequirement.expedition_id == expedition_id,
                ActivityGearRequirement.catalog_id == catalog_id,
            )
        )

    def gear_checks(self, expedition_id: int) -> list[ActivityGearCheck]:
        return list(
            self.session.scalars(
                select(ActivityGearCheck).where(ActivityGearCheck.expedition_id == expedition_id)
            )
        )

    def get_gear_check(
        self, expedition_id: int, user_id: int, catalog_id: int
    ) -> ActivityGearCheck | None:
        return self.session.scalar(
            select(ActivityGearCheck).where(
                ActivityGearCheck.expedition_id == expedition_id,
                ActivityGearCheck.user_id == user_id,
                ActivityGearCheck.catalog_id == catalog_id,
            )
        )

    def participant_count(self, expedition_id: int) -> int:
        return int(
            self.session.scalar(
                select(func.count()).where(
                    ExpeditionRegistration.expedition_id == expedition_id,
                    ExpeditionRegistration.status == RegistrationStatus.CONFIRMED,
                )
            )
            or 0
        )

    def movements(
        self,
        inventory_id: int,
        *,
        since: datetime | None = None,
    ) -> list[InventoryMovement]:
        statement = select(InventoryMovement).where(InventoryMovement.inventory_id == inventory_id)
        if since is not None:
            statement = statement.where(InventoryMovement.created_at >= since)
        return list(self.session.scalars(statement.order_by(InventoryMovement.created_at)))

    # -- reservations -----------------------------------------------------

    def list_inventory_for_catalog(
        self, catalog_id: int, *, for_update: bool = False
    ) -> list[GearInventory]:
        statement = select(GearInventory).where(GearInventory.catalog_id == catalog_id)
        if for_update:
            statement = statement.with_for_update()
        return list(
            self.session.scalars(
                statement.order_by(GearInventory.ownership, GearInventory.id)
            )
        )

    def decrement_available_returning(self, inventory_id: int, quantity: int) -> int | None:
        """Atomically decrement only when enough stock remains; return the new value."""
        return self.session.scalar(
            update(GearInventory)
            .where(
                GearInventory.id == inventory_id,
                GearInventory.quantity_available >= quantity,
            )
            .values(quantity_available=GearInventory.quantity_available - quantity)
            .returning(GearInventory.quantity_available)
        )

    def increment_available_returning(self, inventory_id: int, quantity: int) -> int:
        value = self.session.scalar(
            update(GearInventory)
            .where(GearInventory.id == inventory_id)
            .values(quantity_available=GearInventory.quantity_available + quantity)
            .returning(GearInventory.quantity_available)
        )
        return int(value)

    def get_reservation(
        self, reservation_id: int, *, for_update: bool = False
    ) -> GearReservation | None:
        statement = (
            select(GearReservation)
            .where(GearReservation.id == reservation_id)
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def get_open_reservation(self, expedition_id: int) -> GearReservation | None:
        return self.session.scalar(
            select(GearReservation)
            .where(
                GearReservation.expedition_id == expedition_id,
                GearReservation.status.in_(OPEN_RESERVATION_STATUSES),
            )
            .order_by(GearReservation.id.desc())
            .limit(1)
        )

    def list_reservations(
        self,
        *,
        expedition_id: int | None = None,
        status: ReservationStatus | None = None,
    ) -> list[GearReservation]:
        statement = select(GearReservation)
        if expedition_id is not None:
            statement = statement.where(GearReservation.expedition_id == expedition_id)
        if status is not None:
            statement = statement.where(GearReservation.status == status)
        return list(
            self.session.scalars(statement.order_by(GearReservation.expires_at, GearReservation.id))
        )

    def reservation_items(
        self, reservation_id: int, *, for_update: bool = False
    ) -> list[GearReservationItem]:
        statement = select(GearReservationItem).where(
            GearReservationItem.reservation_id == reservation_id
        )
        if for_update:
            statement = statement.with_for_update()
        return list(self.session.scalars(statement.order_by(GearReservationItem.id)))

    def get_reservation_item(
        self, item_id: int, *, for_update: bool = False
    ) -> GearReservationItem | None:
        statement = select(GearReservationItem).where(GearReservationItem.id == item_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def reservation_covered_for_catalog(
        self, expedition_id: int, catalog_id: int
    ) -> int:
        """Quantity already frozen/fulfilled by non-draft reservations for one catalog item."""
        return int(
            self.session.scalar(
                select(func.coalesce(func.sum(GearReservationItem.quantity_reserved), 0))
                .select_from(GearReservationItem)
                .join(GearReservation, GearReservation.id == GearReservationItem.reservation_id)
                .where(
                    GearReservationItem.expedition_id == expedition_id,
                    GearReservationItem.catalog_id == catalog_id,
                    GearReservation.status.in_(RESERVATION_COVERED_STATUSES),
                )
            ) or 0
        )

    def expirable_reservations(self, now: datetime) -> list[GearReservation]:
        return list(
            self.session.scalars(
                select(GearReservation)
                .where(
                    GearReservation.status.in_(
                        {
                            ReservationStatus.DRAFT,
                            ReservationStatus.CONFIRMED,
                            ReservationStatus.PARTIALLY_FULFILLED,
                        }
                    ),
                    GearReservation.expires_at <= now,
                )
                .with_for_update()
            )
        )
