"""API-level tests for grouping catalogue entries that are one physical part."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine


def _part(client: TestClient, mpn: str, *, manufacturer: str | None = None) -> int:
    ctype = client.post("/api/types", json={"name": f"type-{mpn}"}).json()
    body: dict[str, object] = {"type_id": ctype["id"], "mpn": mpn}
    if manufacturer is not None:
        body["manufacturer"] = manufacturer
    return int(client.post("/api/components", json=body).json()["id"])


def _stock(client: TestClient, component_id: int, quantity: int) -> None:
    location = client.post(
        "/api/locations", json={"type": "drawer", "name": f"D{component_id}"}
    ).json()
    client.post(
        "/api/stock/add",
        json={
            "component_id": component_id,
            "location_id": location["id"],
            "quantity": quantity,
        },
    )


def _account_headers(
    client: TestClient, anon_client: TestClient, *, role: str, username: str
) -> dict[str, str]:
    client.post(
        "/api/admin/users",
        json={"username": username, "password": "password123", "role": role},
    )
    token = anon_client.post(
        "/api/auth/token", json={"username": username, "password": "password123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_an_ungrouped_part_reads_as_a_group_of_one(client: TestClient) -> None:
    part = _part(client, "AO3400A")
    _stock(client, part, 40)

    body = client.get(f"/api/components/{part}/equivalents").json()

    # No special case for the client: the component itself is always a member, so
    # the panel renders one shape whether or not anything is grouped.
    assert body["group_id"] is None
    assert body["notes"] is None
    assert [m["component_id"] for m in body["members"]] == [part]
    assert body["total_stock"] == 40


def test_adding_an_equivalent_returns_the_whole_group_with_summed_stock(
    client: TestClient,
) -> None:
    bulk = _part(client, "AO3400A")
    tape = _part(client, "AO3400A-TR")
    _stock(client, bulk, 40)
    _stock(client, tape, 400)

    resp = client.post(
        f"/api/components/{bulk}/equivalents",
        json={"component_id": tape, "notes": "tape and bulk of the same die"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["group_id"] is not None
    assert body["notes"] == "tape and bulk of the same die"
    # The number a BOM line will read: 40 under one index and 400 under the other
    # is 440 of one part.
    assert body["total_stock"] == 440
    assert {m["component_id"] for m in body["members"]} == {bulk, tape}
    assert {m["stock"] for m in body["members"]} == {40, 400}


def test_a_retired_member_stays_listed_and_marked(client: TestClient) -> None:
    bulk = _part(client, "AO3400A")
    tape = _part(client, "AO3400A-TR")
    _stock(client, bulk, 40)
    client.post(f"/api/components/{bulk}/equivalents", json={"component_id": tape})

    gone = client.request(
        "DELETE",
        f"/api/admin/components/{tape}",
        json={"reason": "discontinued"},
    )

    assert gone.status_code == 204
    body = client.get(f"/api/components/{bulk}/equivalents").json()
    retired = next(m for m in body["members"] if m["component_id"] == tape)
    # Still on screen, and marked: the decision that these are one part was made
    # by a person, taking a part out of use is reversible, and a row that quietly
    # vanished would leave the group looking like something nobody chose.
    assert retired["deleted"] is True
    # Nothing to add: a part cannot be taken out of use while its stock is on the
    # shelf, so a retired member always holds zero.
    assert retired["stock"] == 0
    assert body["total_stock"] == 40


def test_the_group_is_read_from_either_member(client: TestClient) -> None:
    bulk = _part(client, "AO3400A")
    tape = _part(client, "AO3400A-TR")
    client.post(f"/api/components/{bulk}/equivalents", json={"component_id": tape})

    from_other_side = client.get(f"/api/components/{tape}/equivalents").json()

    assert {m["component_id"] for m in from_other_side["members"]} == {bulk, tape}


def test_adding_a_part_from_another_group_is_refused(client: TestClient) -> None:
    bulk = _part(client, "AO3400A")
    tape = _part(client, "AO3400A-TR")
    other = _part(client, "SI2302")
    spare = _part(client, "SI2302-TR")
    client.post(f"/api/components/{bulk}/equivalents", json={"component_id": tape})
    client.post(f"/api/components/{other}/equivalents", json={"component_id": spare})

    resp = client.post(
        f"/api/components/{bulk}/equivalents", json={"component_id": other}
    )

    assert resp.status_code == 422
    assert "SI2302" in resp.json()["detail"]


def test_removing_a_member_dissolves_a_pair(client: TestClient) -> None:
    bulk = _part(client, "AO3400A")
    tape = _part(client, "AO3400A-TR")
    client.post(f"/api/components/{bulk}/equivalents", json={"component_id": tape})

    resp = client.delete(f"/api/components/{bulk}/equivalents/{tape}")

    assert resp.status_code == 204
    body = client.get(f"/api/components/{bulk}/equivalents").json()
    assert body["group_id"] is None
    assert [m["component_id"] for m in body["members"]] == [bulk]


def test_removing_a_part_that_is_not_in_the_group_is_refused(
    client: TestClient,
) -> None:
    bulk = _part(client, "AO3400A")
    tape = _part(client, "AO3400A-TR")
    stranger = _part(client, "SI2302")
    partner = _part(client, "SI2302-TR")
    client.post(f"/api/components/{bulk}/equivalents", json={"component_id": tape})
    # The stranger is in a group of its OWN — otherwise the removal would be
    # refused for simply not being grouped, and this would prove nothing about
    # the group it was addressed through.
    client.post(
        f"/api/components/{stranger}/equivalents",
        json={"component_id": partner},
    )

    resp = client.delete(f"/api/components/{bulk}/equivalents/{stranger}")

    # A stale panel must not be able to dissolve a group elsewhere in the
    # catalogue just by naming an id.
    assert resp.status_code == 422
    other = client.get(f"/api/components/{stranger}/equivalents").json()
    assert {m["component_id"] for m in other["members"]} == {stranger, partner}


def test_notes_can_be_rewritten_and_cleared(client: TestClient) -> None:
    bulk = _part(client, "AO3400A")
    tape = _part(client, "AO3400A-TR")
    client.post(
        f"/api/components/{bulk}/equivalents",
        json={"component_id": tape, "notes": "first"},
    )

    rewritten = client.put(
        f"/api/components/{bulk}/equivalents/notes", json={"notes": "second"}
    )
    assert rewritten.status_code == 200
    assert rewritten.json()["notes"] == "second"

    cleared = client.put(
        f"/api/components/{bulk}/equivalents/notes", json={"notes": ""}
    )
    assert cleared.json()["notes"] is None


def test_notes_on_an_ungrouped_part_are_refused(client: TestClient) -> None:
    part = _part(client, "AO3400A")
    resp = client.put(
        f"/api/components/{part}/equivalents/notes", json={"notes": "x"}
    )
    assert resp.status_code == 422


def test_candidates_search_by_substring_and_flag_grouped_ones(
    client: TestClient,
) -> None:
    bulk = _part(client, "AO3400A")
    tape = _part(client, "AO3400A-TR")
    tray = _part(client, "AO3400A/TRAY")
    spare = _part(client, "AO3400A/T&R")
    _stock(client, tape, 400)
    client.post(f"/api/components/{tray}/equivalents", json={"component_id": spare})

    rows = client.get(
        f"/api/components/{bulk}/equivalents/candidates", params={"q": "AO3400"}
    ).json()

    by_id = {row["component_id"]: row for row in rows}
    assert bulk not in by_id  # never offered as a variant of itself
    assert by_id[tape]["grouped"] is False
    assert by_id[tape]["stock"] == 400
    # Listed but marked, so the panel can say why it cannot be picked instead of
    # hiding a part that is plainly in the catalogue.
    assert by_id[tray]["grouped"] is True


def test_an_unknown_component_is_a_404(client: TestClient) -> None:
    assert client.get("/api/components/9999/equivalents").status_code == 404
    assert (
        client.get(
            "/api/components/9999/equivalents/candidates", params={"q": "x"}
        ).status_code
        == 404
    )


def test_a_retired_part_page_can_be_read_but_not_written(client: TestClient) -> None:
    bulk = _part(client, "AO3400A")
    tape = _part(client, "AO3400A-TR")
    spare = _part(client, "AO3400A/TRAY")
    client.post(f"/api/components/{bulk}/equivalents", json={"component_id": tape})
    client.request(
        "DELETE", f"/api/admin/components/{bulk}", json={"reason": "discontinued"}
    )

    # Looking is fine — the group is part of what the page says about the part.
    assert client.get(f"/api/components/{bulk}/equivalents").status_code == 200
    # Every write is not. The component page hides its write controls once a part
    # is out of use, and this panel must not be the one place that stayed open:
    # the membership it would drop is exactly what a restore is supposed to bring
    # back.
    assert (
        client.post(
            f"/api/components/{bulk}/equivalents", json={"component_id": spare}
        ).status_code
        == 422
    )
    assert (
        client.put(
            f"/api/components/{bulk}/equivalents/notes", json={"notes": "x"}
        ).status_code
        == 422
    )
    assert (
        client.delete(f"/api/components/{bulk}/equivalents/{tape}").status_code == 422
    )
    still_there = client.get(f"/api/components/{bulk}/equivalents").json()
    assert {m["component_id"] for m in still_there["members"]} == {bulk, tape}


def test_a_retired_variant_can_still_be_dropped_from_a_live_part(
    client: TestClient,
) -> None:
    bulk = _part(client, "AO3400A")
    tape = _part(client, "AO3400A-TR")
    client.post(f"/api/components/{bulk}/equivalents", json={"component_id": tape})
    client.request(
        "DELETE", f"/api/admin/components/{tape}", json={"reason": "discontinued"}
    )

    resp = client.delete(f"/api/components/{bulk}/equivalents/{tape}")

    # The refusal above is about the page's own part, never the other one: saying
    # "that discontinued entry is not the same part after all" is a decision about
    # the live part, made from its page, and it has to stay possible.
    assert resp.status_code == 204
    assert client.get(f"/api/components/{bulk}/equivalents").json()["group_id"] is None


def _count_selects(engine: Engine, tables: set[str]) -> tuple[dict[str, int], object]:
    """Count SELECTs per table while the returned listener is attached."""
    counts = dict.fromkeys(tables, 0)

    def listen(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        normalized = statement.lstrip().lower()
        if not normalized.startswith("select"):
            return
        for table in tables:
            if table in normalized:
                counts[table] += 1

    event.listen(engine, "before_cursor_execute", listen)
    return counts, listen


def test_the_candidate_search_does_not_query_per_row(
    client: TestClient, engine: Engine
) -> None:
    """It runs on every pause in typing, so per-row lookups multiply fast."""
    part = _part(client, "AO3400A")
    for suffix in ("-TR", "/TRAY", "/BULK", "-T&R", "-TU"):
        variant = _part(client, f"AO3400A{suffix}")
        _stock(client, variant, 10)

    counts, listener = _count_selects(
        engine, {"component_locations", "component_equivalence_members"}
    )
    try:
        rows = client.get(
            f"/api/components/{part}/equivalents/candidates", params={"q": "AO3400"}
        ).json()
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    assert len(rows) == 5
    # One grouped aggregate for the stock and one lookup for the memberships,
    # however many rows come back — not two more queries per candidate.
    assert counts["component_locations"] == 1
    assert counts["component_equivalence_members"] == 2  # the exclusions, then these


def test_reading_a_group_does_not_query_per_member(
    client: TestClient, engine: Engine
) -> None:
    ids = [_part(client, f"AO3400A-{n}") for n in range(4)]
    for other in ids[1:]:
        client.post(
            f"/api/components/{ids[0]}/equivalents", json={"component_id": other}
        )
    _stock(client, ids[0], 40)

    counts, listener = _count_selects(engine, {"component_locations", "components"})
    try:
        body = client.get(f"/api/components/{ids[0]}/equivalents").json()
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    assert len(body["members"]) == 4
    assert counts["component_locations"] == 1  # one grouped sum for the whole panel
    # The page's own component, then every member in one go — not one `get` each.
    assert counts["components"] == 2


def test_read_only_can_look_but_not_group(
    client: TestClient, anon_client: TestClient
) -> None:
    bulk = _part(client, "AO3400A")
    tape = _part(client, "AO3400A-TR")
    headers = _account_headers(
        client, anon_client, role="read-only", username="viewer"
    )

    assert (
        anon_client.get(
            f"/api/components/{bulk}/equivalents", headers=headers
        ).status_code
        == 200
    )
    assert (
        anon_client.post(
            f"/api/components/{bulk}/equivalents",
            json={"component_id": tape},
            headers=headers,
        ).status_code
        == 403
    )
    client.post(f"/api/components/{bulk}/equivalents", json={"component_id": tape})
    assert (
        anon_client.delete(
            f"/api/components/{bulk}/equivalents/{tape}", headers=headers
        ).status_code
        == 403
    )
    assert (
        anon_client.put(
            f"/api/components/{bulk}/equivalents/notes",
            json={"notes": "x"},
            headers=headers,
        ).status_code
        == 403
    )
