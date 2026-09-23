from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trailforge.database.base import Base, UTCDateTime
from trailforge.domain.enums import (
    ChecklistStatus,
    GearCondition,
    GearOwnership,
    InventoryMovementType,
    LoanStatus,
    ReservationStatus,
)
from trailforge.models.mixins import IntegerPrimaryKeyMixin, TimestampMixin, VersionMixin


class GearCatalog(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "gear_catalog"
    __table_args__ = (CheckConstraint("default_weight_grams >= 0", name="weight_nonnegative"),)

    sku: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    default_weight_grams: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    safety_critical: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    inspection_interval_days: Mapped[int | None] = mapped_column(Integer)


class GearInventory(IntegerPrimaryKeyMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "gear_inventory"
    __table_args__ = (
        UniqueConstraint("catalog_id", "owner_id", "ownership", name="uq_inventory_owner_item"),
        CheckConstraint("quantity_total >= 0", name="total_nonnegative"),
        CheckConstraint("quantity_available >= 0", name="available_nonnegative"),
        CheckConstraint("quantity_available <= quantity_total", name="available_within_total"),
        CheckConstraint("weight_grams >= 0", name="weight_nonnegative"),
    )

    catalog_id: Mapped[int] = mapped_column(ForeignKey("gear_catalog.id", ondelete="RESTRICT"))
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    ownership: Mapped[GearOwnership] = mapped_column(String(24), nullable=False, index=True)
    quantity_total: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_available: Mapped[int] = mapped_column(Integer, nullable=False)
    condition: Mapped[GearCondition] = mapped_column(String(24), nullable=False, index=True)
    weight_grams: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    storage_location: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    acquired_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_inspected_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    catalog: Mapped[GearCatalog] = relationship()
    movements: Mapped[list[InventoryMovement]] = relationship(
        back_populates="inventory",
        cascade="all, delete-orphan",
    )


class InventoryMovement(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "inventory_movements"
    __table_args__ = (
        CheckConstraint("quantity_delta != 0", name="delta_nonzero"),
        CheckConstraint("quantity_after >= 0", name="after_nonnegative"),
    )

    inventory_id: Mapped[int] = mapped_column(
        ForeignKey("gear_inventory.id", ondelete="CASCADE"), index=True
    )
    movement_type: Mapped[InventoryMovementType] = mapped_column(String(24), nullable=False)
    quantity_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_type: Mapped[str] = mapped_column(String(60), nullable=False)
    reference_id: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))

    inventory: Mapped[GearInventory] = relationship(back_populates="movements")


class GearLoan(IntegerPrimaryKeyMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "gear_loans"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("due_at > loaned_at", name="due_after_loan"),
        CheckConstraint("returned_quantity >= 0", name="returned_nonnegative"),
        CheckConstraint("returned_quantity <= quantity", name="returned_within_quantity"),
    )

    inventory_id: Mapped[int] = mapped_column(ForeignKey("gear_inventory.id", ondelete="RESTRICT"))
    borrower_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    expedition_id: Mapped[int | None] = mapped_column(
        ForeignKey("expeditions.id", ondelete="SET NULL")
    )
    reservation_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("gear_reservation_items.id", ondelete="SET NULL"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    returned_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    loaned_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    returned_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[LoanStatus] = mapped_column(
        String(24), default=LoanStatus.ACTIVE, nullable=False, index=True
    )
    condition_out: Mapped[GearCondition] = mapped_column(String(24), nullable=False)
    condition_in: Mapped[GearCondition | None] = mapped_column(String(24))
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)


class ActivityGearRequirement(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "activity_gear_requirements"
    __table_args__ = (
        UniqueConstraint("expedition_id", "catalog_id", name="uq_requirement_expedition_catalog"),
        CheckConstraint("quantity_per_person >= 0", name="per_person_nonnegative"),
        CheckConstraint("quantity_for_group >= 0", name="group_nonnegative"),
    )

    expedition_id: Mapped[int] = mapped_column(
        ForeignKey("expeditions.id", ondelete="CASCADE"), index=True
    )
    catalog_id: Mapped[int] = mapped_column(ForeignKey("gear_catalog.id", ondelete="RESTRICT"))
    quantity_per_person: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quantity_for_group: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mandatory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)


class ActivityGearCheck(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "activity_gear_checks"
    __table_args__ = (
        UniqueConstraint(
            "expedition_id", "user_id", "catalog_id", name="uq_gear_check_expedition_user_catalog"
        ),
        CheckConstraint("quantity >= 0", name="quantity_nonnegative"),
    )

    expedition_id: Mapped[int] = mapped_column(
        ForeignKey("expeditions.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    catalog_id: Mapped[int] = mapped_column(ForeignKey("gear_catalog.id", ondelete="RESTRICT"))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ChecklistStatus] = mapped_column(String(24), nullable=False, index=True)
    verified_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)


class GearReservation(IntegerPrimaryKeyMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "gear_reservations"
    __table_args__ = (
        Index(
            "uq_reservation_expedition_draft",
            "expedition_id",
            "draft_key",
            unique=True,
            sqlite_where=text("status = 'draft'"),
        ),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at", name="expiry_after_creation"
        ),
    )

    expedition_id: Mapped[int] = mapped_column(
        ForeignKey("expeditions.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[ReservationStatus] = mapped_column(
        String(24), default=ReservationStatus.DRAFT, nullable=False, index=True
    )
    draft_key: Mapped[str] = mapped_column(String(80), default="default", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    checked_out_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expired_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancel_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    items: Mapped[list[GearReservationItem]] = relationship(
        back_populates="reservation",
        cascade="all, delete-orphan",
    )


class GearReservationItem(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "gear_reservation_items"
    __table_args__ = (
        UniqueConstraint("reservation_id", "inventory_id", name="uq_reservation_item_inventory"),
        CheckConstraint("quantity > 0", name="reserved_quantity_positive"),
        CheckConstraint("checked_out_quantity >= 0", name="checked_out_nonnegative"),
        CheckConstraint("released_quantity >= 0", name="released_nonnegative"),
        CheckConstraint(
            "checked_out_quantity + released_quantity <= quantity",
            name="reservation_item_within_quantity",
        ),
    )

    reservation_id: Mapped[int] = mapped_column(
        ForeignKey("gear_reservations.id", ondelete="CASCADE"), index=True
    )
    inventory_id: Mapped[int] = mapped_column(
        ForeignKey("gear_inventory.id", ondelete="RESTRICT"), index=True
    )
    catalog_id: Mapped[int] = mapped_column(ForeignKey("gear_catalog.id", ondelete="RESTRICT"))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    checked_out_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    released_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    reservation: Mapped[GearReservation] = relationship(back_populates="items")
