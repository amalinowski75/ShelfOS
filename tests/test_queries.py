"""Tests for read/query helpers used by the UI (listings, history)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models.enums import LocationType
from app.seed import ensure_system_user
from app.services import component_service as cs
from app.services import invoice_service as inv
from app.services import location_service as ls
from app.services import stock_service as ss
from sqlmodel import Session


def test_list_components_filters_by_type(session: Session) -> None:
    resistor = cs.create_type(session, "resistor")
    capacitor = cs.create_type(session, "capacitor")
    r1 = cs.create_component(session, resistor.id)
    cs.create_component(session, capacitor.id)

    all_components = cs.list_components(session)
    assert len(all_components) == 2

    only_resistors = cs.list_components(session, type_id=resistor.id)
    assert [c.id for c in only_resistors] == [r1.id]


def test_total_quantities_by_component(session: Session) -> None:
    user = ensure_system_user(session)
    ctype = cs.create_type(session, "resistor")
    c1 = cs.create_component(session, ctype.id)
    c2 = cs.create_component(session, ctype.id)
    loc_a = ls.create_location(session, type=LocationType.DRAWER, name="A")
    loc_b = ls.create_location(session, type=LocationType.DRAWER, name="B")

    ss.add_stock(
        session, component_id=c1.id, location_id=loc_a.id, quantity=30, user_id=user.id
    )
    ss.add_stock(
        session, component_id=c1.id, location_id=loc_b.id, quantity=20, user_id=user.id
    )
    ss.add_stock(
        session, component_id=c2.id, location_id=loc_a.id, quantity=5, user_id=user.id
    )

    totals = ss.total_quantities_by_component(session)
    assert totals == {c1.id: 50, c2.id: 5}


def test_list_component_locations_only_positive(session: Session) -> None:
    user = ensure_system_user(session)
    ctype = cs.create_type(session, "resistor")
    component = cs.create_component(session, ctype.id)
    loc = ls.create_location(session, type=LocationType.DRAWER, name="A")

    ss.add_stock(
        session,
        component_id=component.id,
        location_id=loc.id,
        quantity=10,
        user_id=user.id,
    )
    ss.remove_stock(
        session,
        component_id=component.id,
        location_id=loc.id,
        quantity=10,
        user_id=user.id,
    )
    # Depleted location is excluded.
    assert ss.list_component_locations(session, component.id) == []


def test_list_movements_most_recent_first(session: Session) -> None:
    user = ensure_system_user(session)
    ctype = cs.create_type(session, "resistor")
    component = cs.create_component(session, ctype.id)
    loc = ls.create_location(session, type=LocationType.DRAWER, name="A")

    ss.add_stock(
        session,
        component_id=component.id,
        location_id=loc.id,
        quantity=10,
        user_id=user.id,
    )
    ss.remove_stock(
        session,
        component_id=component.id,
        location_id=loc.id,
        quantity=3,
        user_id=user.id,
    )
    movements = ss.list_movements(session, component.id)
    assert [m.delta_quantity for m in movements] == [-3, 10]


def test_list_purchase_history(session: Session) -> None:
    user = ensure_system_user(session)
    ctype = cs.create_type(session, "resistor")
    component = cs.create_component(session, ctype.id)
    loc = ls.create_location(session, type=LocationType.DRAWER, name="A")

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
        component_id=component.id,
        quantity=100,
        unit_price=Decimal("0.05"),
        location_id=loc.id,
    )
    # Draft invoice: no purchase history yet.
    assert ss.list_movements(session, component.id) == []
    assert inv.list_purchase_history(session, component.id) == []

    inv.finalize_invoice(session, invoice.id, user_id=user.id)

    history = inv.list_purchase_history(session, component.id)
    assert len(history) == 1
    line, loaded_invoice = history[0]
    assert line.quantity == 100
    assert loaded_invoice.supplier == "Mouser"


def test_build_location_stock_groups_sorts_and_drops_depleted(
    session: Session,
) -> None:
    """What the locations page renders inside each location."""
    from app.web.presenter import build_location_stock

    user = ensure_system_user(session)
    ctype = cs.create_type(session, "resistor")
    zed = cs.create_component(session, ctype.id, mpn="ZZ-1", manufacturer="Yageo")
    alpha = cs.create_component(session, ctype.id, mpn="AA-1")
    nameless = cs.create_component(session, ctype.id)  # no MPN
    drawer = ls.create_location(session, type=LocationType.DRAWER, name="D5")
    other = ls.create_location(session, type=LocationType.DRAWER, name="D9")

    for component, location in ((zed, drawer), (alpha, drawer), (nameless, other)):
        ss.add_stock(
            session,
            component_id=component.id,
            location_id=location.id,
            quantity=5,
            user_id=user.id,
        )
    # A slot emptied back to zero is not "contents".
    ss.remove_stock(
        session,
        component_id=nameless.id,
        location_id=other.id,
        quantity=5,
        user_id=user.id,
    )

    stock = build_location_stock(session)
    assert set(stock) == {drawer.id}  # the depleted location is absent entirely
    rows = stock[drawer.id]
    # Sorted by MPN, so a drawer's contents read in a stable order.
    assert [row["mpn"] for row in rows] == ["AA-1", "ZZ-1"]
    assert rows[1] == {
        "component_id": zed.id,
        "mpn": "ZZ-1",
        "manufacturer": "Yageo",
        "quantity": 5,
        "container": "loose",
    }


def test_build_location_stock_labels_a_component_with_no_mpn(
    session: Session,
) -> None:
    """The label is a link's text, so it can never be blank."""
    from app.web.presenter import build_location_stock

    user = ensure_system_user(session)
    ctype = cs.create_type(session, "resistor")
    component = cs.create_component(session, ctype.id)
    drawer = ls.create_location(session, type=LocationType.DRAWER, name="D5")
    ss.add_stock(
        session,
        component_id=component.id,
        location_id=drawer.id,
        quantity=1,
        user_id=user.id,
    )
    assert build_location_stock(session)[drawer.id][0]["mpn"] == (
        f"Component #{component.id}"
    )


def test_build_location_stock_is_empty_without_stock(session: Session) -> None:
    ls.create_location(session, type=LocationType.DRAWER, name="D5")
    from app.web.presenter import build_location_stock

    assert build_location_stock(session) == {}
