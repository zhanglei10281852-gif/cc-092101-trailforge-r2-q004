from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from trailforge.api.dependencies import get_session
from trailforge.domain.enums import ReservationStatus
from trailforge.schemas.common import Page
from trailforge.schemas.gear import (
    ReservationCancelRequest,
    ReservationCheckoutRequest,
    ReservationCheckoutResponse,
    ReservationConfirmRequest,
    ReservationDraftPlan,
    ReservationDraftRequest,
    ReservationExpiryResult,
    ReservationRequirementDelta,
    ReservationResponse,
)
from trailforge.services.reservations import ReservationService

router = APIRouter(prefix="/gear", tags=["gear-reservations"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get(
    "/expeditions/{expedition_id}/reservations/plan",
    response_model=ReservationDraftPlan,
)
def reservation_plan(expedition_id: int, session: SessionDep) -> ReservationDraftPlan:
    return ReservationService(session).draft_plan(expedition_id)


@router.get(
    "/expeditions/{expedition_id}/reservations/delta",
    response_model=ReservationRequirementDelta,
)
def reservation_requirement_delta(
    expedition_id: int,
    session: SessionDep,
    participant_count_before: int = Query(ge=0),
) -> ReservationRequirementDelta:
    return ReservationService(session).requirement_delta(
        expedition_id, participant_count_before=participant_count_before
    )


@router.put(
    "/expeditions/{expedition_id}/reservations/draft",
    response_model=ReservationResponse,
)
def upsert_reservation_draft(
    expedition_id: int,
    data: ReservationDraftRequest,
    session: SessionDep,
) -> ReservationResponse:
    return ReservationService(session).upsert_draft(expedition_id, data)


@router.get("/reservations", response_model=Page[ReservationResponse])
def list_reservations(
    session: SessionDep,
    expedition_id: int | None = Query(default=None, gt=0),
    reservation_status: ReservationStatus | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    sort: str = "created_at",
    direction: str = Query(default="asc", pattern="^(asc|desc)$"),
) -> Page[ReservationResponse]:
    return ReservationService(session).list_reservations(
        expedition_id=expedition_id,
        status=reservation_status,
        page=page,
        page_size=page_size,
        sort=sort,
        direction=direction,
    )


@router.post("/reservations/expire", response_model=ReservationExpiryResult)
def expire_reservations(session: SessionDep) -> ReservationExpiryResult:
    return ReservationService(session).expire_due()


@router.get("/reservations/{reservation_id}", response_model=ReservationResponse)
def get_reservation(reservation_id: int, session: SessionDep) -> ReservationResponse:
    return ReservationService(session).get_reservation(reservation_id)


@router.post(
    "/reservations/{reservation_id}/confirm",
    response_model=ReservationResponse,
)
def confirm_reservation(
    reservation_id: int,
    data: ReservationConfirmRequest,
    session: SessionDep,
) -> ReservationResponse:
    return ReservationService(session).confirm(reservation_id, data)


@router.post(
    "/reservations/{reservation_id}/checkout",
    response_model=ReservationCheckoutResponse,
    status_code=status.HTTP_201_CREATED,
)
def checkout_reservation(
    reservation_id: int,
    data: ReservationCheckoutRequest,
    session: SessionDep,
) -> ReservationCheckoutResponse:
    reservation, loans = ReservationService(session).checkout(reservation_id, data)
    return ReservationCheckoutResponse(reservation=reservation, loans=loans)


@router.post(
    "/reservations/{reservation_id}/cancel",
    response_model=ReservationResponse,
)
def cancel_reservation(
    reservation_id: int,
    data: ReservationCancelRequest,
    session: SessionDep,
) -> ReservationResponse:
    return ReservationService(session).cancel(reservation_id, data)
