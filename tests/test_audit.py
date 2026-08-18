"""Tests for audit logging across services (spec §19, decision D9)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from app.models.enums import LocationType, ParameterDataType
from app.models.invoice import InvoiceImportLine
from app.seed import ensure_system_user
from app.services import audit_service as audit
from app.services import component_service as cs
from app.services import invoice_import_service as imp
from app.services import invoice_service as inv
from app.services import location_service as ls
from app.services import stock_service as ss
from app.services.errors import InsufficientStockError
from fastapi.testclient import TestClient
from sqlmodel import Session


@pytest.fixture
def ctx(session: Session) -> dict[str, int]:
    """A user, component, location and a table parameter for audit tests."""
    user = ensure_system_user(session)
    ctype = cs.create_type(session, "resistor")
    definition = cs.add_parameter_definition(
        session,
        ctype.id,
        name="resistance",
        label="Resistance",
        data_type=ParameterDataType.NUMBER,
        unit="ohm",
    )
    component = cs.create_component(session, ctype.id)
    location = ls.create_location(session, type=LocationType.DRAWER, name="D1")
    return {
        "user_id": user.id,
        "component_id": component.id,
        "location_id": location.id,
        "definition_id": definition.id,
    }


def test_stock_movement_is_audited(ctx, session: Session) -> None:
    ss.add_stock(
        session,
        component_id=ctx["component_id"],
        location_id=ctx["location_id"],
        quantity=100,
        user_id=ctx["user_id"],
    )
    entries = audit.list_entries(
        session, entity_type="component", entity_id=ctx["component_id"]
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry.field == f"quantity@location:{ctx['location_id']}"
    assert entry.old_value == "0"
    assert entry.new_value == "100"
    assert entry.user_id == ctx["user_id"]


def test_failed_movement_writes_no_audit(ctx, session: Session) -> None:
    """A rejected change leaves no audit row (recorded only after it applies)."""
    ss.add_stock(
        session,
        component_id=ctx["component_id"],
        location_id=ctx["location_id"],
        quantity=5,
        user_id=ctx["user_id"],
    )
    with pytest.raises(InsufficientStockError):
        ss.remove_stock(
            session,
            component_id=ctx["component_id"],
            location_id=ctx["location_id"],
            quantity=6,
            user_id=ctx["user_id"],
        )
    entries = audit.list_entries(
        session, entity_type="component", entity_id=ctx["component_id"]
    )
    assert len(entries) == 1  # only the successful add


def test_parameter_change_is_audited_only_with_user(ctx, session: Session) -> None:
    # Without a user id nothing is logged (system/seed context).
    cs.set_parameter_value(session, ctx["component_id"], ctx["definition_id"], 4700.0)
    assert audit.list_entries(session, entity_type="component") == []

    # With a user, each real change is recorded old -> new.
    for value in (2200.0, 1000.0):
        cs.set_parameter_value(
            session,
            ctx["component_id"],
            ctx["definition_id"],
            value,
            user_id=ctx["user_id"],
        )
    entries = audit.list_entries(session, entity_type="component")
    assert [e.field for e in entries] == [
        "parameter:resistance",
        "parameter:resistance",
    ]
    # Most recent first: the update from 2200 -> 1000.
    assert entries[0].old_value == "2200.0"
    assert entries[0].new_value == "1000.0"


def test_parameter_no_op_is_not_audited(ctx, session: Session) -> None:
    """Setting the same normalized value again writes no phantom audit row."""
    cs.set_parameter_value(
        session,
        ctx["component_id"],
        ctx["definition_id"],
        4700.0,
        user_id=ctx["user_id"],
    )
    # int 4700 normalizes to the stored float 4700.0 -> no change, no new row.
    cs.set_parameter_value(
        session,
        ctx["component_id"],
        ctx["definition_id"],
        4700,
        user_id=ctx["user_id"],
    )
    assert len(audit.list_entries(session, entity_type="component")) == 1


def test_line_location_change_is_audited(ctx, session: Session) -> None:
    invoice = inv.create_invoice(
        session,
        supplier="Mouser",
        invoice_number="INV-1",
        invoice_date=date(2026, 7, 8),
        currency="EUR",
    )
    line = inv.add_line(
        session,
        invoice.id,
        component_id=ctx["component_id"],
        quantity=10,
        unit_price=Decimal("1.00"),
    )
    inv.set_line_location(
        session, invoice.id, line.id, ctx["location_id"], user_id=ctx["user_id"]
    )
    entries = audit.list_entries(session, entity_type="invoice_line", entity_id=line.id)
    assert len(entries) == 1
    assert entries[0].field == "location_id"
    assert entries[0].old_value is None
    assert entries[0].new_value == str(ctx["location_id"])

    # Re-assigning the same location is idempotent: no second audit row.
    inv.set_line_location(
        session, invoice.id, line.id, ctx["location_id"], user_id=ctx["user_id"]
    )
    entries = audit.list_entries(session, entity_type="invoice_line", entity_id=line.id)
    assert len(entries) == 1


def test_component_deletion_is_audited(ctx, session: Session) -> None:
    """A hard delete leaves an audit row even though the component is gone."""
    cs.hard_delete_component(session, ctx["component_id"], user_id=ctx["user_id"])
    entries = audit.list_entries(
        session, entity_type="component", entity_id=ctx["component_id"]
    )
    assert len(entries) == 1
    assert entries[0].field == "deleted"
    assert entries[0].new_value == "true"
    assert entries[0].user_id == ctx["user_id"]


def test_line_removal_is_audited(ctx, session: Session) -> None:
    invoice = inv.create_invoice(
        session,
        supplier="Mouser",
        invoice_number="INV-1",
        invoice_date=date(2026, 7, 8),
        currency="EUR",
    )
    line = inv.add_line(
        session,
        invoice.id,
        component_id=ctx["component_id"],
        quantity=10,
        unit_price=Decimal("1.00"),
    )
    inv.remove_line(session, invoice.id, line.id, user_id=ctx["user_id"])
    entries = audit.list_entries(session, entity_type="invoice_line", entity_id=line.id)
    assert len(entries) == 1
    assert entries[0].field == "deleted"
    assert entries[0].new_value == "true"


def test_finalization_is_audited(ctx, session: Session) -> None:
    invoice = inv.create_invoice(
        session,
        supplier="Mouser",
        invoice_number="INV-1",
        invoice_date=date(2026, 7, 8),
        currency="EUR",
    )
    inv.add_line(
        session,
        invoice.id,
        component_id=ctx["component_id"],
        quantity=10,
        unit_price=Decimal("1.50"),
        location_id=ctx["location_id"],
    )
    inv.finalize_invoice(session, invoice.id, user_id=ctx["user_id"])

    entries = audit.list_entries(session, entity_type="invoice", entity_id=invoice.id)
    fields = {e.field: e for e in entries}
    assert fields["is_finalized"].old_value == "false"
    assert fields["is_finalized"].new_value == "true"
    # The prior gross is the real stored zero, not a placeholder ``None``.
    assert fields["total_gross"].old_value == "0.000000"
    assert fields["total_gross"].new_value == "15.000000"


def _staged_line(session: Session, number: str = "INV-STAGE") -> tuple[int, int]:
    """A draft invoice with one staged import row: (invoice id, row id)."""
    invoice = inv.create_invoice(
        session,
        supplier="TME",
        invoice_number=number,
        invoice_date=date(2026, 7, 8),
        currency="PLN",
    )
    staging = InvoiceImportLine(
        invoice_id=invoice.id,
        line_no=1,
        mpn="R1",
        manufacturer="Acme",
        quantity=100,
        unit_price=Decimal("1.00"),
        shop_key="tme",
        reason="",
    )
    session.add(staging)
    session.commit()
    session.refresh(staging)
    return int(invoice.id), int(staging.id)


def test_staged_line_review_edits_are_audited(ctx, session: Session) -> None:
    # What scan putaway writes on a draft invoice: the bag's shelf and, when the
    # count came up short, its quantity. The staged row is deleted at finalize,
    # so the log is the only lasting record of who decided either.
    invoice_id, staged_id = _staged_line(session)
    imp.update_pending(
        session,
        invoice_id,
        staged_id,
        location_id=ctx["location_id"],
        quantity=98,
        user_id=ctx["user_id"],
    )

    # Keyed by (invoice, line_no) — a staging id is reused, see the id-reuse test.
    entries = audit.list_entries(session, entity_type="invoice", entity_id=invoice_id)
    fields = {e.field: e for e in entries}
    assert set(fields) == {"import-line:1:location_id", "import-line:1:quantity"}
    fields = {audit.import_line_of(k)[1]: v for k, v in fields.items()}
    assert fields["location_id"].old_value is None
    assert fields["location_id"].new_value == str(ctx["location_id"])
    assert fields["quantity"].old_value == "100"
    assert fields["quantity"].new_value == "98"
    assert all(e.user_id == ctx["user_id"] for e in entries)


def test_staged_line_no_op_edit_writes_nothing(ctx, session: Session) -> None:
    invoice_id, staged_id = _staged_line(session)
    # Re-sending the value it already holds is not a change — a scan rescanning
    # the shelf a row already has.
    imp.update_pending(
        session, invoice_id, staged_id, quantity=100, user_id=ctx["user_id"]
    )

    assert (
        audit.list_entries(session, entity_type="invoice", entity_id=invoice_id) == []
    )


def test_dismissed_staged_line_is_audited(ctx, session: Session) -> None:
    invoice_id, staged_id = _staged_line(session)
    imp.dismiss_pending(session, invoice_id, staged_id, user_id=ctx["user_id"])

    entries = audit.list_entries(session, entity_type="invoice", entity_id=invoice_id)
    assert len(entries) == 1
    assert entries[0].field == "import-line:1:deleted"
    assert entries[0].new_value == "true"


def test_staged_line_histories_survive_a_reused_row_id(ctx, session: Session) -> None:
    """Two invoices, one recycled staging id, two separate histories.

    ``invoice_import_lines`` has a plain INTEGER PRIMARY KEY and finalize deletes
    every staged row, so SQLite hands the next import the same ids back. Keyed by
    the row id, the two invoices' edits would splice into one unreadable history.
    """
    first_invoice, first_row = _staged_line(session, number="FV-1")
    imp.dismiss_pending(session, first_invoice, first_row, user_id=ctx["user_id"])
    second_invoice, second_row = _staged_line(session, number="FV-2")
    assert second_row == first_row  # the id really is handed out again
    imp.update_pending(
        session, second_invoice, second_row, quantity=7, user_id=ctx["user_id"]
    )

    first = audit.list_entries(session, entity_type="invoice", entity_id=first_invoice)
    second = audit.list_entries(
        session, entity_type="invoice", entity_id=second_invoice
    )
    assert [e.field for e in first] == ["import-line:1:deleted"]
    assert [e.field for e in second] == ["import-line:1:quantity"]


def test_location_rename_and_move_are_audited(ctx, session: Session) -> None:
    parent = ls.create_location(session, type=LocationType.RACK, name="Rack A")
    ls.update_location(
        session,
        ctx["location_id"],
        name="D2",
        parent_id=parent.id,
        user_id=ctx["user_id"],
    )

    entries = audit.list_entries(
        session, entity_type="location", entity_id=ctx["location_id"]
    )
    fields = {e.field: e for e in entries}
    assert set(fields) == {"name", "parent_id"}  # the type never changed
    assert fields["name"].old_value == "D1"
    assert fields["name"].new_value == "D2"
    assert fields["parent_id"].old_value is None
    assert fields["parent_id"].new_value == str(parent.id)

    # Setting the same values again changes nothing, so it logs nothing.
    ls.update_location(
        session,
        ctx["location_id"],
        name="D2",
        parent_id=parent.id,
        user_id=ctx["user_id"],
    )
    again = audit.list_entries(
        session, entity_type="location", entity_id=ctx["location_id"]
    )
    assert len(again) == 2


def test_location_deletion_audits_the_branch_and_the_lines_it_orphans(
    ctx, session: Session
) -> None:
    # A recursive delete is the one that can quietly rewrite other pages: every
    # invoice line pointing into the branch loses its destination.
    rack = ls.create_location(session, type=LocationType.RACK, name="Rack A")
    shelf = ls.create_location(
        session, type=LocationType.SHELF, name="S1", parent_id=rack.id
    )
    staged_invoice, staged_row = _staged_line(session, number="FV-STAGED")
    imp.update_pending(
        session,
        staged_invoice,
        staged_row,
        location_id=shelf.id,
        user_id=ctx["user_id"],
    )
    invoice = inv.create_invoice(
        session,
        supplier="Mouser",
        invoice_number="INV-DEL",
        invoice_date=date(2026, 7, 8),
        currency="EUR",
    )
    line = inv.add_line(
        session,
        invoice.id,
        component_id=ctx["component_id"],
        quantity=1,
        unit_price=Decimal("1.00"),
        location_id=shelf.id,
    )

    ls.delete_location(session, rack.id, recursive=True, user_id=ctx["user_id"])

    deleted = {
        e.entity_id: e
        for e in audit.list_entries(session, entity_type="location")
        if e.field == "deleted"
    }
    assert set(deleted) == {rack.id, shelf.id}
    assert all(e.new_value == "true" for e in deleted.values())

    cleared = audit.list_entries(session, entity_type="invoice_line", entity_id=line.id)
    assert len(cleared) == 1
    assert cleared[0].field == "location_id"
    assert cleared[0].old_value == str(shelf.id)
    assert cleared[0].new_value is None

    # A staged import row points at a location too, and its clearing is the one
    # that would otherwise make the invoice un-finalizable with no explanation.
    staged_entries = audit.list_entries(
        session, entity_type="invoice", entity_id=staged_invoice
    )
    latest = staged_entries[0]
    assert latest.field == "import-line:1:location_id"
    assert latest.old_value == str(shelf.id)
    assert latest.new_value is None


def test_audit_endpoint_is_admin_only(client: TestClient) -> None:
    ctype = client.post("/api/types", json={"name": "resistor"}).json()
    component = client.post("/api/components", json={"type_id": ctype["id"]}).json()
    location = client.post(
        "/api/locations", json={"type": "drawer", "name": "D1"}
    ).json()
    client.post(
        "/api/stock/add",
        json={
            "component_id": component["id"],
            "location_id": location["id"],
            "quantity": 7,
        },
    )

    resp = client.get("/api/admin/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert any(e["field"].startswith("quantity@location:") for e in body)


def test_field_name_helpers_round_trip() -> None:
    """Parameterized field names build and parse back symmetrically."""
    assert audit.quantity_location_of(audit.quantity_field(42)) == 42
    assert audit.parameter_name_of(audit.parameter_field("resistance")) == "resistance"

    # Non-matching or malformed fields yield None, never a bogus value.
    assert audit.parameter_name_of("location_id") is None
    assert audit.parameter_name_of("parameter:") is None  # empty name is invalid
    assert audit.quantity_location_of("quantity@location:") is None
    assert audit.quantity_location_of("parameter:x") is None


def test_audit_endpoint_requires_auth(anon_client: TestClient) -> None:
    assert anon_client.get("/api/admin/audit").status_code == 401


def test_audit_endpoint_forbidden_for_non_admin(
    client: TestClient, anon_client: TestClient
) -> None:
    """A normal (non-admin) user is rejected with 403 by the router guard."""
    client.post(
        "/api/admin/users",
        json={"username": "worker", "password": "pw", "role": "user"},
    )
    token = anon_client.post(
        "/api/auth/token", json={"username": "worker", "password": "pw"}
    ).json()["access_token"]

    resp = anon_client.get(
        "/api/admin/audit", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


def test_audit_endpoint_rejects_out_of_range_limit(client: TestClient) -> None:
    """``limit`` is bounded so a negative/huge value cannot dump the whole log."""
    assert client.get("/api/admin/audit", params={"limit": -1}).status_code == 422
    assert client.get("/api/admin/audit", params={"limit": 0}).status_code == 422
    assert client.get("/api/admin/audit", params={"limit": 5000}).status_code == 422
    assert client.get("/api/admin/audit", params={"limit": 100}).status_code == 200


def test_granting_admin_is_recorded(ctx, session: Session) -> None:
    """The question an audit log exists for, and until now unanswerable here."""
    from app.models.enums import UserRole
    from app.services import user_service as us

    bob = us.create_user(session, username="bob", password="password123")
    us.set_role(session, bob.id, UserRole.ADMIN, actor_id=ctx["user_id"])

    entries = audit.list_entries(session, entity_type="user", entity_id=bob.id)
    fields = {e.field: e for e in entries}
    assert fields["role"].old_value == "user"
    assert fields["role"].new_value == "admin"
    assert fields["role"].user_id == ctx["user_id"]  # who granted it

    # Setting the role it already has is not a grant and is not recorded.
    before = len(entries)
    us.set_role(session, bob.id, UserRole.ADMIN, actor_id=ctx["user_id"])
    again = audit.list_entries(session, entity_type="user", entity_id=bob.id)
    assert len(again) == before


def test_an_account_is_recorded_with_what_it_may_do(ctx, session: Session) -> None:
    """Creation is not audited for ordinary rows; an account is the exception,
    because it is an access grant however it is worded."""
    from app.models.enums import UserRole
    from app.services import user_service as us

    made = us.create_user(
        session,
        username="carol",
        password="password123",
        role=UserRole.ADMIN,
        actor_id=ctx["user_id"],
    )
    entry = audit.list_entries(session, entity_type="user", entity_id=made.id)[0]
    assert entry.field == "created"
    assert entry.new_value == "carol (admin)"

    # Seeding has nobody to attribute it to and records nothing.
    seeded = us.create_user(session, username="dave", password="password123")
    assert audit.list_entries(session, entity_type="user", entity_id=seeded.id) == []


def test_a_password_change_is_recorded_but_never_the_password(
    ctx, session: Session
) -> None:
    """The entry says a password changed and who changed it. Not the password,
    not the old hash, not a prefix of either."""
    from app.services import user_service as us

    bob = us.create_user(session, username="bob", password="password123")
    hash_before = bob.password_hash

    us.set_password(session, bob.id, "hunter2-the-secret", actor_id=ctx["user_id"])

    entry = audit.list_entries(session, entity_type="user", entity_id=bob.id)[0]
    assert entry.field == "password"
    assert entry.new_value == "set"
    written = f"{entry.old_value}{entry.new_value}"
    assert "hunter2" not in written
    assert (hash_before or "")[:12] not in written
    # An admin reset (actor is somebody else) against a self-service change: the
    # log tells them apart by who is on the entry, not by a separate field.
    assert entry.user_id == ctx["user_id"] != bob.id
    us.change_own_password(session, bob, "hunter2-the-secret", "another-secret-1")
    own = audit.list_entries(session, entity_type="user", entity_id=bob.id)[0]
    assert own.user_id == bob.id


def test_disabling_an_account_is_recorded(ctx, session: Session) -> None:
    from app.services import user_service as us

    bob = us.create_user(session, username="bob", password="password123")
    us.set_active(session, bob.id, False, actor_id=ctx["user_id"])

    entry = audit.list_entries(session, entity_type="user", entity_id=bob.id)[0]
    assert (entry.field, entry.old_value, entry.new_value) == (
        "is_active",
        "true",
        "false",
    )


def test_a_matching_rule_is_recorded_with_what_it_does(ctx, session: Session) -> None:
    """A rule changes how every later import is read and keeps no history of its
    own, so both its arrival and its wording belong in the log."""
    from app.models.enums import MatchDomain
    from app.services import match_rule_service as mrs

    rule = mrs.create_rule(
        session,
        domain=MatchDomain.TYPE,
        alias="rezystor",
        canonical="resistor",
        user_id=ctx["user_id"],
    )
    entries = audit.list_entries(session, entity_type="match_rule", entity_id=rule.id)
    created = entries[0]
    assert created.field == "created"
    assert created.new_value == "type: rezystor → resistor"

    mrs.update_rule(session, rule.id, canonical="capacitor", user_id=ctx["user_id"])
    changed = audit.list_entries(session, entity_type="match_rule", entity_id=rule.id)[
        0
    ]
    assert (changed.field, changed.old_value, changed.new_value) == (
        "canonical",
        "resistor",
        "capacitor",
    )


def test_a_deleted_rule_says_what_it_was(ctx, session: Session) -> None:
    """ "Who deleted the rule that mapped Rezystancja to resistance" is what
    someone will bring to the log — and by then the row's id says nothing."""
    from app.models.enums import MatchDomain
    from app.services import match_rule_service as mrs

    rule = mrs.create_rule(
        session,
        domain=MatchDomain.TYPE,
        alias="kondensator",
        canonical="capacitor",
        user_id=ctx["user_id"],
    )
    mrs.delete_rule(session, rule.id, user_id=ctx["user_id"])

    entry = audit.list_entries(session, entity_type="match_rule", entity_id=rule.id)[0]
    assert entry.field == "deleted"
    assert entry.old_value == "type: kondensator → capacitor"
    assert entry.user_id == ctx["user_id"]


def test_seeded_rules_are_not_attributed_to_anybody(session: Session) -> None:
    """They arrive at startup, before there is anyone to blame for them."""
    from app.services import match_rule_service as mrs

    assert mrs.seed_default_rules(session) > 0
    assert audit.list_entries(session, entity_type="match_rule") == []
