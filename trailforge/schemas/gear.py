from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from trailforge.domain.enums import (
    ChecklistStatus,
    GearCondition,
    GearOwnership,
    LoanStatus,
    ReservationStatus,
)
from trailforge.schemas.common import (
    TimestampedResponse,
    VersionedResponse,
    clean_text,
    require_aware,
)


class GearCatalogCreate(BaseModel):
    sku: str = Field(min_length=2, max_length=80, pattern="^[A-Za-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=4000)
    default_weight_grams: int = Field(default=0, ge=0, le=1000000)
    safety_critical: bool = False
    inspection_interval_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("sku")
    @classmethod
    def normalize_sku(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("name", "category")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return clean_text(value)


class GearCatalogResponse(TimestampedResponse):
    sku: str
    name: str
    category: str
    description: str
    default_weight_grams: int
    safety_critical: bool
    inspection_interval_days: int | None


class GearInventoryCreate(BaseModel):
    catalog_id: int = Field(gt=0)
    owner_id: int | None = Field(default=None, gt=0)
    ownership: GearOwnership
    quantity_total: int = Field(ge=1, le=100000)
    condition: GearCondition = GearCondition.GOOD
    weight_grams: int | None = Field(default=None, ge=0, le=1000000)
    storage_location: str = Field(default="", max_length=160)
    acquired_at: datetime | None = None
    last_inspected_at: datetime | None = None
    notes: str = Field(default="", max_length=4000)
    actor_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=160)

    @field_validator("acquired_at", "last_inspected_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        return require_aware(value) if value is not None else None

    @model_validator(mode="after")
    def validate_owner(self) -> GearInventoryCreate:
        if self.ownership == GearOwnership.PERSONAL and self.owner_id is None:
            raise ValueError("personal inventory requires owner_id")
        if self.ownership == GearOwnership.CLUB and self.owner_id is not None:
            raise ValueError("club inventory cannot have owner_id")
        return self


class GearInventoryResponse(VersionedResponse):
    catalog_id: int
    owner_id: int | None
    ownership: GearOwnership
    quantity_total: int
    quantity_available: int
    condition: GearCondition
    weight_grams: int
    storage_location: str
    acquired_at: datetime | None
    last_inspected_at: datetime | None
    notes: str


class InventoryAdjustment(BaseModel):
    quantity_delta: int = Field(ge=-100000, le=100000)
    reason: str = Field(min_length=1, max_length=2000)
    actor_id: int = Field(gt=0)
    expected_version: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(min_length=8, max_length=160)

    @field_validator("quantity_delta")
    @classmethod
    def nonzero_delta(cls, value: int) -> int:
        if value == 0:
            raise ValueError("quantity_delta cannot be zero")
        return value


class GearLoanCreate(BaseModel):
    inventory_id: int = Field(gt=0)
    borrower_id: int = Field(gt=0)
    expedition_id: int | None = Field(default=None, gt=0)
    quantity: int = Field(gt=0, le=100000)
    loaned_at: datetime
    due_at: datetime
    notes: str = Field(default="", max_length=4000)
    actor_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=160)

    @field_validator("loaned_at", "due_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def validate_due_date(self) -> GearLoanCreate:
        if self.due_at <= self.loaned_at:
            raise ValueError("due_at must be later than loaned_at")
        return self


class GearLoanReturn(BaseModel):
    quantity: int = Field(gt=0, le=100000)
    returned_at: datetime
    condition_in: GearCondition
    notes: str = Field(default="", max_length=4000)
    actor_id: int = Field(gt=0)
    expected_version: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(min_length=8, max_length=160)

    @field_validator("returned_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return require_aware(value)


class GearLoanResponse(VersionedResponse):
    inventory_id: int
    borrower_id: int
    expedition_id: int | None
    quantity: int
    returned_quantity: int
    loaned_at: datetime
    due_at: datetime
    returned_at: datetime | None
    status: LoanStatus
    condition_out: GearCondition
    condition_in: GearCondition | None
    notes: str


class GearRequirementCreate(BaseModel):
    catalog_id: int = Field(gt=0)
    quantity_per_person: int = Field(default=0, ge=0, le=1000)
    quantity_for_group: int = Field(default=0, ge=0, le=100000)
    mandatory: bool = True
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def require_quantity(self) -> GearRequirementCreate:
        if self.quantity_per_person == 0 and self.quantity_for_group == 0:
            raise ValueError("at least one required quantity must be positive")
        return self


class GearRequirementResponse(TimestampedResponse):
    expedition_id: int
    catalog_id: int
    quantity_per_person: int
    quantity_for_group: int
    mandatory: bool
    notes: str


class GearCheckUpsert(BaseModel):
    user_id: int = Field(gt=0)
    catalog_id: int = Field(gt=0)
    quantity: int = Field(ge=0, le=100000)
    status: ChecklistStatus
    verified_by: int | None = Field(default=None, gt=0)
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def verifier_required(self) -> GearCheckUpsert:
        if self.status == ChecklistStatus.VERIFIED and self.verified_by is None:
            raise ValueError("verified status requires verified_by")
        return self


class GearCheckResponse(TimestampedResponse):
    expedition_id: int
    user_id: int
    catalog_id: int
    quantity: int
    status: ChecklistStatus
    verified_by: int | None
    verified_at: datetime | None
    notes: str


class MissingGearItem(BaseModel):
    catalog_id: int
    sku: str
    name: str
    mandatory: bool
    required_quantity: int
    packed_quantity: int
    missing_quantity: int
    group_required: int = 0
    personal_required: int = 0
    club_reserved_quantity: int = 0
    group_missing_quantity: int = 0
    personal_missing_quantity: int = 0


class MissingGearReport(BaseModel):
    expedition_id: int
    participant_count: int
    checked_at: datetime
    is_ready: bool
    missing_items: list[MissingGearItem]
    reserved_units: int = 0


class GearStatistics(BaseModel):
    total_catalog_items: int
    total_inventory_units: int
    available_inventory_units: int
    active_loans: int
    overdue_loans: int
    loaned_units: int
    utilization_rate: float
    items_by_condition: dict[str, int]
    loans_by_catalog: dict[str, int]


class ReservationItemAllocation(BaseModel):
    inventory_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=100000)
    notes: str = Field(default="", max_length=2000)


class ReservationDraftRequest(BaseModel):
    actor_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=160)
    draft_key: str = Field(
        default="default", min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$"
    )
    expires_at: datetime | None = None
    notes: str = Field(default="", max_length=4000)
    items: list[ReservationItemAllocation] = Field(min_length=1, max_length=500)

    @field_validator("expires_at")
    @classmethod
    def normalize_expiry(cls, value: datetime | None) -> datetime | None:
        return require_aware(value) if value is not None else None


class ReservationConfirmRequest(BaseModel):
    actor_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=160)
    expected_version: int | None = Field(default=None, ge=1)


class ReservationCancelRequest(BaseModel):
    actor_id: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=2000)
    expected_version: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(min_length=8, max_length=160)


class ReservationCheckoutItem(BaseModel):
    item_id: int = Field(gt=0)
    borrower_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=100000)
    loaned_at: datetime
    due_at: datetime
    notes: str = Field(default="", max_length=4000)

    @field_validator("loaned_at", "due_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def validate_due_date(self) -> ReservationCheckoutItem:
        if self.due_at <= self.loaned_at:
            raise ValueError("due_at must be later than loaned_at")
        return self


class ReservationCheckoutRequest(BaseModel):
    actor_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=160)
    loans: list[ReservationCheckoutItem] = Field(min_length=1, max_length=500)


class ReservationItemResponse(TimestampedResponse):
    reservation_id: int
    inventory_id: int
    catalog_id: int
    quantity: int
    checked_out_quantity: int
    released_quantity: int
    outstanding_quantity: int
    notes: str


class ReservationResponse(VersionedResponse):
    expedition_id: int
    status: ReservationStatus
    draft_key: str
    expires_at: datetime | None
    confirmed_at: datetime | None
    checked_out_at: datetime | None
    cancelled_at: datetime | None
    expired_at: datetime | None
    cancel_reason: str
    notes: str
    items: list[ReservationItemResponse]


class ReservationInventoryOption(BaseModel):
    inventory_id: int
    ownership: GearOwnership
    condition: GearCondition
    quantity_total: int
    quantity_available: int
    storage_location: str
    allocatable: bool
    blocked_reason: str | None = None


class ReservationSuggestion(BaseModel):
    catalog_id: int
    sku: str
    name: str
    category: str
    group_required: int
    club_reserved_quantity: int
    open_reservation_quantity: int
    club_available_quantity: int
    suggested_quantity: int
    options: list[ReservationInventoryOption] = []


class ReservationDraftPlan(BaseModel):
    expedition_id: int
    participant_count: int
    generated_at: datetime
    items: list[ReservationSuggestion]


class ReservationExpiryResult(BaseModel):
    expired_reservation_ids: list[int]
    released_units: int


class ReservationRequirementDeltaItem(BaseModel):
    catalog_id: int
    sku: str
    name: str
    group_required: int
    confirmed_reserved_quantity: int
    group_delta: int
    personal_required_before: int
    personal_required_after: int
    personal_packed_quantity: int
    personal_delta: int


class ReservationRequirementDelta(BaseModel):
    expedition_id: int
    participant_count_before: int
    participant_count_after: int
    changed_at: datetime
    items: list[ReservationRequirementDeltaItem]


class ReservationCheckoutResponse(BaseModel):
    reservation: ReservationResponse
    loans: list[GearLoanResponse]
