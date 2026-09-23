from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _route_payload() -> dict:
    return {
        "name": "Reservation Ridge",
        "region": "Reservation Mountains",
        "distance_km": 9,
        "elevation_gain_m": 450,
        "elevation_loss_m": 450,
        "min_altitude_m": 100,
        "max_altitude_m": 550,
        "estimated_duration_minutes": 240,
        "difficulty": "moderate",
        "is_loop": True,
        "is_published": True,
        "segments": [
            {
                "sequence": 1,
                "name": "Main",
                "distance_km": 9,
                "elevation_gain_m": 450,
                "estimated_duration_minutes": 240,
                "difficulty": "moderate",
                "start_latitude": 30,
                "start_longitude": 120,
                "end_latitude": 30,
                "end_longitude": 120,
            }
        ],
        "points": [],
        "risk_tag_ids": [],
    }


def _setup_group_gear(client) -> tuple[int, int, int, int, int]:
    organizer = client.post(
        "/api/v1/users",
        json={"email": "res-org@example.com", "display_name": "Res Org"},
    ).json()["id"]
    borrower = client.post(
        "/api/v1/users",
        json={"email": "res-borrower@example.com", "display_name": "Res Borrower"},
    ).json()["id"]
    route = client.post(
        "/api/v1/routes", params={"actor_id": organizer}, json=_route_payload()
    ).json()["id"]
    start = datetime.now(UTC) + timedelta(days=10)
    expedition = client.post(
        "/api/v1/expeditions",
        json={
            "organizer_id": organizer,
            "route_id": route,
            "name": "Reservation Expedition",
            "meeting_location": "Trailhead",
            "meeting_at": (start - timedelta(hours=1)).isoformat(),
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=6)).isoformat(),
            "registration_deadline": (start - timedelta(days=1)).isoformat(),
            "capacity": 5,
            "minimum_fitness_level": 1,
            "risk_level": "moderate",
        },
    ).json()["id"]
    catalog = client.post(
        "/api/v1/gear/catalog",
        params={"actor_id": organizer},
        json={
            "sku": "FIRSTAID-3",
            "name": "First aid kit",
            "category": "medical",
            "safety_critical": True,
        },
    ).json()["id"]
    inventory = client.post(
        "/api/v1/gear/inventory",
        json={
            "catalog_id": catalog,
            "ownership": "club",
            "quantity_total": 2,
            "condition": "good",
            "actor_id": organizer,
            "idempotency_key": "firstaid-stock",
        },
    ).json()["id"]
    requirement = client.put(
        f"/api/v1/gear/expeditions/{expedition}/requirements",
        params={"actor_id": organizer},
        json={"catalog_id": catalog, "quantity_for_group": 2},
    )
    assert requirement.status_code == 200
    return organizer, borrower, expedition, catalog, inventory  # type: ignore[return-value]


def test_reservation_api_full_lifecycle_and_queries(client) -> None:
    organizer, borrower, expedition, catalog, inventory = _setup_group_gear(client)

    proposal = client.get(f"/api/v1/gear/expeditions/{expedition}/reservation-proposal")
    assert proposal.status_code == 200
    group_line = next(
        line for line in proposal.json()["lines"] if line["requirement_source"] == "group"
    )
    assert group_line["required_quantity"] == 2
    assert group_line["suggested_reservation_quantity"] == 2

    draft = client.post(
        f"/api/v1/gear/expeditions/{expedition}/reservations",
        json={
            "expires_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
            "items": [
                {"catalog_id": catalog, "inventory_id": inventory, "quantity_reserved": 2}
            ],
            "actor_id": organizer,
            "idempotency_key": "api-draft-key",
        },
    )
    assert draft.status_code == 201, draft.text
    reservation_id = draft.json()["id"]
    assert draft.json()["status"] == "draft"

    confirm_payload = {"actor_id": organizer, "idempotency_key": "api-confirm-key"}
    confirmed = client.post(
        f"/api/v1/gear/reservations/{reservation_id}/confirm", json=confirm_payload
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    # Idempotent replay returns the same resource without double-freezing.
    replay = client.post(
        f"/api/v1/gear/reservations/{reservation_id}/confirm", json=confirm_payload
    )
    assert replay.status_code == 200
    assert replay.json()["version"] == confirmed.json()["version"]

    # Query endpoints.
    detail = client.get(f"/api/v1/gear/reservations/{reservation_id}")
    assert detail.status_code == 200
    assert detail.json()["items"][0]["quantity_reserved"] == 2
    listing = client.get(
        "/api/v1/gear/reservations",
        params={"expedition_id": expedition, "status": "confirmed"},
    )
    assert listing.status_code == 200
    assert len(listing.json()) == 1

    # Capacity delta is computed without mutating the confirmed reservation.
    delta = client.get(f"/api/v1/gear/expeditions/{expedition}/reservation-capacity-delta")
    assert delta.status_code == 200
    assert delta.json()["reservation_status"] == "confirmed"
    assert delta.json()["participant_count_before"] == 1

    # Missing report treats the confirmed hold as covered.
    missing = client.get(f"/api/v1/gear/expeditions/{expedition}/missing")
    assert missing.status_code == 200
    assert missing.json()["is_ready"] is True

    # Partial fulfilment converts one held unit into a loan.
    item_id = detail.json()["items"][0]["id"]
    fulfil = client.post(
        f"/api/v1/gear/reservations/{reservation_id}/fulfil",
        json={
            "lines": [
                {"reservation_item_id": item_id, "borrower_id": borrower, "quantity": 1}
            ],
            "actor_id": organizer,
            "idempotency_key": "api-fulfil-key",
        },
    )
    assert fulfil.status_code == 200, fulfil.text
    assert fulfil.json()["status"] == "partially_fulfilled"

    # Cancelling releases only the unfulfilled remainder.
    cancelled = client.post(
        f"/api/v1/gear/reservations/{reservation_id}/cancel",
        json={
            "reason": "Weather",
            "actor_id": organizer,
            "idempotency_key": "api-cancel-key",
        },
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    database = client.app.state.database
    from trailforge.models.gear import GearInventory

    with database.session() as session:
        row = session.get(GearInventory, inventory)
        # One unit remains out on loan; the other hold returned to the pool.
        assert row.quantity_available == 1


def test_reservation_api_expire_due_releases_holds(client) -> None:
    organizer, _, expedition, catalog, inventory = _setup_group_gear(client)
    draft = client.post(
        f"/api/v1/gear/expeditions/{expedition}/reservations",
        json={
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "items": [
                {"catalog_id": catalog, "inventory_id": inventory, "quantity_reserved": 2}
            ],
            "actor_id": organizer,
            "idempotency_key": "expire-draft-key",
        },
    )
    reservation_id = draft.json()["id"]
    client.post(
        f"/api/v1/gear/reservations/{reservation_id}/confirm",
        json={"actor_id": organizer, "idempotency_key": "expire-confirm-key"},
    )
    later = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    expired = client.post(
        "/api/v1/gear/reservations/expire-due",
        json={"actor_id": organizer, "now": later},
    )
    assert expired.status_code == 200
    statuses = {item["id"]: item["status"] for item in expired.json()}
    assert statuses[reservation_id] == "expired"

    from trailforge.models.gear import GearInventory

    with client.app.state.database.session() as session:
        assert session.get(GearInventory, inventory).quantity_available == 2
