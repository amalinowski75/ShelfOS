"""Tests for the data model: schema creation, enum storage, seeding."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models import (
    Component,
    ComponentType,
    Invoice,
    Location,
    ParameterDefinition,
)
from app.models.enums import (
    LocationType,
    MountingType,
    ParameterDataType,
    UserRole,
)
from app.seed import SYSTEM_USER_NAME, ensure_system_user
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import Session, select


def test_all_expected_tables_created(engine: Engine) -> None:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).all()
    tables = {row[0] for row in rows}
    expected = {
        "component_types",
        "parameter_definitions",
        "parameter_enum_values",
        "components",
        "component_parameters",
        "locations",
        "component_locations",
        "stock_movements",
        "invoices",
        "invoice_lines",
        "attachments",
        "users",
        "audit_log",
    }
    assert expected <= tables


def test_enum_persisted_by_value_not_name(session: Session) -> None:
    location = Location(type=LocationType.COMPARTMENT, name="C1")
    session.add(location)
    session.commit()

    # Raw storage must be the token value, not the Python member name.
    stored = session.exec(text("SELECT type FROM locations")).one()  # type: ignore[call-overload]
    assert stored[0] == "compartment"


def test_read_only_role_stored_with_hyphen(session: Session) -> None:
    from app.models.user import User

    session.add(User(name="ro", role=UserRole.READ_ONLY))
    session.commit()
    stored = session.exec(text("SELECT role FROM users WHERE name='ro'")).one()  # type: ignore[call-overload]
    assert stored[0] == "read-only"


def test_component_defaults(session: Session) -> None:
    ctype = ComponentType(name="resistor")
    session.add(ctype)
    session.commit()
    session.refresh(ctype)

    component = Component(type_id=ctype.id)  # type: ignore[arg-type]
    session.add(component)
    session.commit()
    session.refresh(component)

    assert component.mounting_type is MountingType.OTHER
    assert component.status.value == "active"
    assert component.deleted_at is None


def test_parameter_definition_roundtrip(session: Session) -> None:
    ctype = ComponentType(name="resistor")
    session.add(ctype)
    session.commit()
    session.refresh(ctype)

    definition = ParameterDefinition(
        type_id=ctype.id,  # type: ignore[arg-type]
        name="resistance",
        label="Resistance",
        data_type=ParameterDataType.NUMBER,
        unit="ohm",
        is_filterable=True,
        is_table_column=True,
    )
    session.add(definition)
    session.commit()

    loaded = session.exec(select(ParameterDefinition)).one()
    assert loaded.data_type is ParameterDataType.NUMBER
    assert loaded.unit == "ohm"


def test_invoice_decimal_precision(session: Session) -> None:
    invoice = Invoice(
        supplier="Mouser",
        invoice_number="INV-1",
        invoice_date=date(2026, 7, 8),
        currency="EUR",
        total_net=Decimal("12.345678"),
        total_gross=Decimal("15.185184"),
    )
    session.add(invoice)
    session.commit()

    loaded = session.exec(select(Invoice)).one()
    assert isinstance(loaded.total_net, Decimal)
    assert loaded.total_net == Decimal("12.345678")


def test_ensure_system_user_is_idempotent(session: Session) -> None:
    first = ensure_system_user(session)
    second = ensure_system_user(session)
    assert first.id == second.id
    assert first.name == SYSTEM_USER_NAME
    assert first.role is UserRole.ADMIN
