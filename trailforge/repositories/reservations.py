from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import selectinload

from trailforge.domain.enums import (
    GearCondition,
    GearOwnership,
    LoanStatus,
    ReservationStatus,
)
from trailforge.models.gear import (
    GearInventory,
    GearLoan,
    GearReservation,
    GearReservationItem,
)
from trailforge.repositories.base import BaseRepository, PageResult

# States in which a reservation still holds frozen stock.
OPEN_RESERVATION_STATUSES = {
    ReservationStatus.CONFIRMED,
    ReservationStatus.PARTIALLY_CHECKED_OUT,
}
# Drafts hold no stock but are still live working documents.
RESERVATION_ACTIVE_STATUSES = OPEN_RESERVATION_STATUSES | {ReservationStatus.DRAFT}

BLOCKED_CONDITIONS = {GearCondition.DAMAGED, GearCondition.RETIRED}


class ReservationRepository(BaseRepository[GearReservation]):
    model = GearReservation
    sortable = {
        "created_at": GearReservation.created_at,
        "expires_at": GearReservation.expires_at,
        "status": GearReservation.status,
    }

    def get_detail(
        self, reservation_id: int, *, for_update: bool = False
    ) -> GearReservation | None:
        statement = (
            select(GearReservation)
            .options(selectinload(GearReservation.items))
            .where(GearReservation.id == reservation_id)
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def get_by_draft_key(
        self, expedition_id: int, draft_key: str, *, for_update: bool = False
    ) -> GearReservation | None:
        statement = select(GearReservation).where(
            GearReservation.expedition_id == expedition_id,
            GearReservation.draft_key == draft_key,
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def list_reservations(
        self,
        *,
        expedition_id: int | None = None,
        status: ReservationStatus | None = None,
        page: int = 1,
        page_size: int = 20,
        sort: str = "created_at",
        direction: str = "asc",
    ) -> PageResult[GearReservation]:
        statement: Select = select(GearReservation).options(
            selectinload(GearReservation.items)
        )
        if expedition_id is not None:
            statement = statement.where(GearReservation.expedition_id == expedition_id)
        if status is not None:
            statement = statement.where(GearReservation.status == status)
        return self.paginate(
            statement,
            page=page,
            page_size=page_size,
            sort=sort,
            direction=direction,
        )

    def get_item(self, item_id: int, *, for_update: bool = False) -> GearReservationItem | None:
        statement = select(GearReservationItem).where(GearReservationItem.id == item_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def open_reserved_quantity(
        self,
        catalog_id: int,
        *,
        expedition_id: int | None = None,
        exclude_reservation_id: int | None = None,
    ) -> int:
        """Outstanding frozen units for a catalog across open reservations."""
        outstanding = (
            GearReservationItem.quantity
            - GearReservationItem.checked_out_quantity
            - GearReservationItem.released_quantity
        )
        statement = (
            select(func.coalesce(func.sum(outstanding), 0))
            .select_from(GearReservationItem)
            .join(GearReservation, GearReservation.id == GearReservationItem.reservation_id)
            .where(
                GearReservationItem.catalog_id == catalog_id,
                GearReservation.status.in_(
                    [status.value for status in OPEN_RESERVATION_STATUSES]
                ),
            )
        )
        if expedition_id is not None:
            statement = statement.where(GearReservation.expedition_id == expedition_id)
        if exclude_reservation_id is not None:
            statement = statement.where(GearReservation.id != exclude_reservation_id)
        return int(self.session.scalar(statement) or 0)

    def open_reserved_quantities(self, expedition_id: int) -> dict[int, int]:
        outstanding = (
            GearReservationItem.quantity
            - GearReservationItem.checked_out_quantity
            - GearReservationItem.released_quantity
        )
        rows = self.session.execute(
            select(GearReservationItem.catalog_id, func.coalesce(func.sum(outstanding), 0))
            .select_from(GearReservationItem)
            .join(GearReservation, GearReservation.id == GearReservationItem.reservation_id)
            .where(
                GearReservation.expedition_id == expedition_id,
                GearReservation.status.in_(
                    [status.value for status in OPEN_RESERVATION_STATUSES]
                ),
            )
            .group_by(GearReservationItem.catalog_id)
        )
        return {int(catalog_id): int(quantity) for catalog_id, quantity in rows}

    def club_inventories(self, catalog_id: int) -> list[GearInventory]:
        return list(
            self.session.scalars(
                select(GearInventory)
                .where(
                    GearInventory.catalog_id == catalog_id,
                    GearInventory.ownership == GearOwnership.CLUB.value,
                )
                .order_by(GearInventory.id)
            )
        )

    def personal_inventories(self, catalog_id: int) -> list[GearInventory]:
        return list(
            self.session.scalars(
                select(GearInventory)
                .where(
                    GearInventory.catalog_id == catalog_id,
                    GearInventory.ownership == GearOwnership.PERSONAL.value,
                )
                .order_by(GearInventory.id)
            )
        )

    def expirable(self, now: datetime) -> list[GearReservation]:
        return list(
            self.session.scalars(
                select(GearReservation)
                .options(selectinload(GearReservation.items))
                .where(
                    GearReservation.status.in_(
                        [status.value for status in OPEN_RESERVATION_STATUSES]
                    ),
                    GearReservation.expires_at.is_not(None),
                    GearReservation.expires_at <= now,
                )
                .order_by(GearReservation.expires_at, GearReservation.id)
            )
        )

    def list_active_for_expedition(self, expedition_id: int) -> list[GearReservation]:
        return list(
            self.session.scalars(
                select(GearReservation)
                .options(selectinload(GearReservation.items))
                .where(
                    GearReservation.expedition_id == expedition_id,
                    GearReservation.status.in_(
                        [status.value for status in RESERVATION_ACTIVE_STATUSES]
                    ),
                )
                .order_by(GearReservation.id)
            )
        )

    def active_item_loan(
        self, item_id: int, borrower_id: int
    ) -> GearLoan | None:
        return self.session.scalar(
            select(GearLoan).where(
                GearLoan.reservation_item_id == item_id,
                GearLoan.borrower_id == borrower_id,
                GearLoan.status.in_([LoanStatus.ACTIVE.value, LoanStatus.OVERDUE.value]),
            )
        )
