from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from trailforge.api.dependencies import get_session
from trailforge.domain.enums import ReservationStatus
from trailforge.schemas.gear import (
    ReservationCancelRequest,
    ReservationCapacityDelta,
    ReservationConfirmRequest,
    ReservationDraftCreate,
    ReservationExpireRequest,
    ReservationFulfilRequest,
    ReservationProposal,
    ReservationResponse,
)
from trailforge.services.reservations import ReservationService

router = APIRouter(prefix="/gear", tags=["reservations"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get(
    "/expeditions/{expedition_id}/reservation-proposal",
    response_model=ReservationProposal,
)
def reservation_proposal(expedition_id: int, session: SessionDep) -> ReservationProposal:
    return ReservationService(session).proposal(expedition_id)


@router.get(
    "/expeditions/{expedition_id}/reservation-capacity-delta",
    response_model=ReservationCapacityDelta,
)
def reservation_capacity_delta(expedition_id: int, session: SessionDep) -> ReservationCapacityDelta:
    return ReservationService(session).capacity_delta(expedition_id)


@router.post(
    "/expeditions/{expedition_id}/reservations",
    response_model=ReservationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_reservation_draft(
    expedition_id: int, data: ReservationDraftCreate, session: SessionDep
) -> ReservationResponse:
    return ReservationService(session).create_draft(expedition_id, data)


@router.get("/reservations", response_model=list[ReservationResponse])
def list_reservations(
    session: SessionDep,
    expedition_id: int | None = Query(default=None, gt=0),
    reservation_status: ReservationStatus | None = Query(default=None, alias="status"),
) -> list[ReservationResponse]:
    return ReservationService(session).list(
        expedition_id=expedition_id,
        status=reservation_status,
    )


@router.get("/reservations/{reservation_id}", response_model=ReservationResponse)
def get_reservation(reservation_id: int, session: SessionDep) -> ReservationResponse:
    return ReservationService(session).get(reservation_id)


@router.post("/reservations/{reservation_id}/confirm", response_model=ReservationResponse)
def confirm_reservation(
    reservation_id: int, data: ReservationConfirmRequest, session: SessionDep
) -> ReservationResponse:
    return ReservationService(session).confirm(reservation_id, data)


@router.post("/reservations/{reservation_id}/fulfil", response_model=ReservationResponse)
def fulfil_reservation(
    reservation_id: int, data: ReservationFulfilRequest, session: SessionDep
) -> ReservationResponse:
    return ReservationService(session).fulfil(reservation_id, data)


@router.post("/reservations/{reservation_id}/cancel", response_model=ReservationResponse)
def cancel_reservation(
    reservation_id: int, data: ReservationCancelRequest, session: SessionDep
) -> ReservationResponse:
    return ReservationService(session).cancel(reservation_id, data)


@router.post("/reservations/expire-due", response_model=list[ReservationResponse])
def expire_due_reservations(
    data: ReservationExpireRequest, session: SessionDep
) -> list[ReservationResponse]:
    return ReservationService(session).expire_due(
        actor_id=data.actor_id,
        now=data.now,
        idempotency_key=data.idempotency_key,
    )
