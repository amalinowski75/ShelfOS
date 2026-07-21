"""Tests for component_service: types, parameter inheritance, EAV validation."""

from __future__ import annotations

import pytest
from app.models.enums import ParameterDataType
from app.services import component_service as cs
from app.services.errors import NotFoundError, ValidationError
from sqlmodel import Session


def test_create_type_rejects_empty_name(session: Session) -> None:
    with pytest.raises(ValidationError):
        cs.create_type(session, "  ")


def test_create_type_rejects_unknown_parent(session: Session) -> None:
    with pytest.raises(NotFoundError):
        cs.create_type(session, "mosfet", parent_id=999)


def test_create_type_rejects_duplicate_name_under_same_parent(
    session: Session,
) -> None:
    """Type names are unique within a parent, including root level (M3)."""
    cs.create_type(session, "resistor")
    with pytest.raises(ValidationError):
        cs.create_type(session, "resistor")  # duplicate root
    with pytest.raises(ValidationError):
        cs.create_type_with_parameters(session, "resistor")  # same, batch API

    parent = cs.create_type(session, "semiconductor")
    cs.create_type(session, "mosfet", parent_id=parent.id)
    with pytest.raises(ValidationError):
        cs.create_type(session, "mosfet", parent_id=parent.id)


def test_same_type_name_allowed_under_different_parents(session: Session) -> None:
    a = cs.create_type(session, "passive")
    b = cs.create_type(session, "active")
    # "generic" is fine under each distinct parent and at the root.
    cs.create_type(session, "generic", parent_id=a.id)
    cs.create_type(session, "generic", parent_id=b.id)
    cs.create_type(session, "generic")


def test_add_parameter_definition_rejects_duplicate_name(session: Session) -> None:
    """A parameter's technical key is unique within its type (M3)."""
    ctype = cs.create_type(session, "resistor")
    cs.add_parameter_definition(
        session,
        ctype.id,
        name="resistance",
        label="Resistance",
        data_type=ParameterDataType.NUMBER,
        unit="ohm",
    )
    with pytest.raises(ValidationError):
        cs.add_parameter_definition(
            session,
            ctype.id,
            name="resistance",
            label="Resistance (dup)",
            data_type=ParameterDataType.TEXT,
        )


def test_create_type_with_parameters_creates_type_and_definitions(
    session: Session,
) -> None:
    ctype = cs.create_type_with_parameters(
        session,
        "capacitor",
        parameters=[
            cs.ParameterSpec(
                name="capacitance",
                label="Capacitance",
                data_type=ParameterDataType.NUMBER,
                unit="farad",
                sort_order=0,
            ),
            cs.ParameterSpec(
                name="dielectric",
                label="Dielectric",
                data_type=ParameterDataType.ENUM,
                enum_values=["X7R", "C0G"],
                sort_order=1,
            ),
        ],
    )

    definitions = cs.list_own_parameter_definitions(session, ctype.id)
    assert [d.name for d in definitions] == ["capacitance", "dielectric"]

    # The enum values are persisted and the value routes/validates correctly.
    component = cs.create_component(session, ctype.id)
    dielectric = definitions[1]
    param = cs.set_parameter_value(session, component.id, dielectric.id, "X7R")
    assert param.value_text == "X7R"
    with pytest.raises(ValidationError):
        cs.set_parameter_value(session, component.id, dielectric.id, "NP0")


def test_create_type_with_parameters_is_atomic_on_bad_spec(session: Session) -> None:
    with pytest.raises(ValidationError):
        cs.create_type_with_parameters(
            session,
            "capacitor",
            parameters=[
                cs.ParameterSpec(
                    name="dielectric",
                    label="Dielectric",
                    data_type=ParameterDataType.ENUM,  # missing enum_values
                ),
            ],
        )
    # No partially created type is left behind.
    assert cs.list_types(session) == []


def test_create_type_with_parameters_rejects_duplicate_names(session: Session) -> None:
    with pytest.raises(ValidationError):
        cs.create_type_with_parameters(
            session,
            "resistor",
            parameters=[
                cs.ParameterSpec(
                    name="resistance",
                    label="Resistance",
                    data_type=ParameterDataType.NUMBER,
                ),
                cs.ParameterSpec(
                    name="resistance",
                    label="Resistance (dup)",
                    data_type=ParameterDataType.TEXT,
                ),
            ],
        )
    assert cs.list_types(session) == []


def test_create_type_with_parameters_rejects_unknown_parent(session: Session) -> None:
    with pytest.raises(NotFoundError):
        cs.create_type_with_parameters(session, "mosfet", parent_id=999)


def test_list_own_parameter_definitions_excludes_inherited(session: Session) -> None:
    transistor = cs.create_type_with_parameters(
        session,
        "transistor",
        parameters=[
            cs.ParameterSpec(
                name="package", label="Package", data_type=ParameterDataType.TEXT
            )
        ],
    )
    mosfet = cs.create_type_with_parameters(
        session,
        "mosfet",
        parent_id=transistor.id,
        parameters=[
            cs.ParameterSpec(
                name="rds_on", label="Rds(on)", data_type=ParameterDataType.NUMBER
            )
        ],
    )

    own = [d.name for d in cs.list_own_parameter_definitions(session, mosfet.id)]
    assert own == ["rds_on"]
    effective = [
        d.name for d in cs.get_effective_parameter_definitions(session, mosfet.id)
    ]
    assert effective == ["package", "rds_on"]


def test_parameter_inheritance_along_hierarchy(session: Session) -> None:
    transistor = cs.create_type(session, "transistor")
    mosfet = cs.create_type(session, "mosfet", parent_id=transistor.id)

    cs.add_parameter_definition(
        session,
        transistor.id,
        name="package",
        label="Package",
        data_type=ParameterDataType.TEXT,
        sort_order=0,
    )
    cs.add_parameter_definition(
        session,
        mosfet.id,
        name="rds_on",
        label="Rds(on)",
        data_type=ParameterDataType.NUMBER,
        unit="ohm",
        sort_order=1,
    )

    names = [d.name for d in cs.get_effective_parameter_definitions(session, mosfet.id)]
    # Ancestor parameters come first, then the type's own.
    assert names == ["package", "rds_on"]
    # The parent type only sees its own parameter.
    parent_names = [
        d.name for d in cs.get_effective_parameter_definitions(session, transistor.id)
    ]
    assert parent_names == ["package"]


def test_enum_parameter_requires_values(session: Session) -> None:
    ctype = cs.create_type(session, "capacitor")
    with pytest.raises(ValidationError):
        cs.add_parameter_definition(
            session,
            ctype.id,
            name="dielectric",
            label="Dielectric",
            data_type=ParameterDataType.ENUM,
        )


def test_enum_parameter_rejects_blank_or_duplicate_values(session: Session) -> None:
    """Enum tokens are client-facing picker choices: no blanks, no duplicates."""
    ctype = cs.create_type(session, "capacitor")
    with pytest.raises(ValidationError):
        cs.add_parameter_definition(
            session,
            ctype.id,
            name="dielectric",
            label="Dielectric",
            data_type=ParameterDataType.ENUM,
            enum_values=["X7R", "  "],
        )
    with pytest.raises(ValidationError):
        cs.add_parameter_definition(
            session,
            ctype.id,
            name="package",
            label="Package",
            data_type=ParameterDataType.ENUM,
            enum_values=["0402", "0402"],
        )
    # The batch (create-type) path validates before writing anything, too.
    with pytest.raises(ValidationError):
        cs.create_type_with_parameters(
            session,
            "resistor",
            parameters=[
                cs.ParameterSpec(
                    name="tolerance",
                    label="Tolerance",
                    data_type=ParameterDataType.ENUM,
                    enum_values=["1%", "1%"],
                )
            ],
        )
    # Nothing partial was created by the rejected batch.
    assert [t.name for t in cs.list_types(session)] == ["capacitor"]


def test_set_number_parameter_routes_to_value_num(session: Session) -> None:
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

    param = cs.set_parameter_value(session, component.id, definition.id, 4700.0)
    assert param.value_num == 4700.0
    assert param.value_text is None
    assert param.value_bool is None


def test_set_number_parameter_rejects_bool(session: Session) -> None:
    ctype = cs.create_type(session, "resistor")
    definition = cs.add_parameter_definition(
        session,
        ctype.id,
        name="resistance",
        label="Resistance",
        data_type=ParameterDataType.NUMBER,
    )
    component = cs.create_component(session, ctype.id)
    with pytest.raises(ValidationError):
        cs.set_parameter_value(session, component.id, definition.id, True)


def test_set_enum_parameter_validates_allowed_values(session: Session) -> None:
    ctype = cs.create_type(session, "capacitor")
    definition = cs.add_parameter_definition(
        session,
        ctype.id,
        name="dielectric",
        label="Dielectric",
        data_type=ParameterDataType.ENUM,
        enum_values=["X7R", "C0G", "Y5V"],
    )
    component = cs.create_component(session, ctype.id)

    param = cs.set_parameter_value(session, component.id, definition.id, "X7R")
    assert param.value_text == "X7R"

    with pytest.raises(ValidationError):
        cs.set_parameter_value(session, component.id, definition.id, "NP0")


def test_enum_values_of_returns_tokens_in_sort_order(session: Session) -> None:
    ctype = cs.create_type(session, "capacitor")
    definition = cs.add_parameter_definition(
        session,
        ctype.id,
        name="dielectric",
        label="Dielectric",
        data_type=ParameterDataType.ENUM,
        enum_values=["X7R", "C0G", "Y5V"],
    )
    # Insertion order is the display order (decision D6).
    assert cs.enum_values_of(session, definition.id) == ["X7R", "C0G", "Y5V"]

    # A non-enum parameter simply has no allowed values.
    resistance = cs.add_parameter_definition(
        session,
        ctype.id,
        name="resistance",
        label="Resistance",
        data_type=ParameterDataType.NUMBER,
    )
    assert cs.enum_values_of(session, resistance.id) == []


def test_create_component_with_values_batches_enum_validation(
    session: Session,
) -> None:
    from sqlalchemy import event

    ctype = cs.create_type(session, "capacitor")
    dielectric = cs.add_parameter_definition(
        session,
        ctype.id,
        name="dielectric",
        label="Dielectric",
        data_type=ParameterDataType.ENUM,
        enum_values=["X7R", "C0G"],
    )
    package = cs.add_parameter_definition(
        session,
        ctype.id,
        name="package",
        label="Package",
        data_type=ParameterDataType.ENUM,
        enum_values=["0402", "0603"],
    )

    enum_selects = 0

    def count(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        nonlocal enum_selects
        normalized = statement.lstrip().lower()
        if normalized.startswith("select") and "parameter_enum_values" in normalized:
            enum_selects += 1

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", count)
    try:
        component = cs.create_component_with_values(
            session,
            ctype.id,
            values=[(dielectric.id, "X7R"), (package.id, "0603")],
        )
    finally:
        event.remove(bind, "before_cursor_execute", count)

    # One batched lookup for both enum parameters, not one query per value.
    assert enum_selects == 1
    values = {
        v.parameter_definition_id: v.value_text
        for v in cs.list_parameter_values(session, component.id)
    }
    assert values == {dielectric.id: "X7R", package.id: "0603"}


def test_create_component_with_values_skips_enum_query_when_no_enums(
    session: Session,
) -> None:
    from sqlalchemy import event

    ctype = cs.create_type(session, "resistor")
    resistance = cs.add_parameter_definition(
        session,
        ctype.id,
        name="resistance",
        label="Resistance",
        data_type=ParameterDataType.NUMBER,
    )

    enum_selects = 0

    def count(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        nonlocal enum_selects
        normalized = statement.lstrip().lower()
        if normalized.startswith("select") and "parameter_enum_values" in normalized:
            enum_selects += 1

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", count)
    try:
        cs.create_component_with_values(
            session, ctype.id, values=[(resistance.id, "4k7")]
        )
    finally:
        event.remove(bind, "before_cursor_execute", count)

    # A create with no enum parameters must not touch parameter_enum_values.
    assert enum_selects == 0


def test_create_component_with_values_rejects_bad_enum_token(session: Session) -> None:
    ctype = cs.create_type(session, "capacitor")
    dielectric = cs.add_parameter_definition(
        session,
        ctype.id,
        name="dielectric",
        label="Dielectric",
        data_type=ParameterDataType.ENUM,
        enum_values=["X7R", "C0G"],
    )
    # The batched allowed-set path still rejects an out-of-set token, atomically.
    with pytest.raises(ValidationError):
        cs.create_component_with_values(
            session, ctype.id, values=[(dielectric.id, "NP0")]
        )
    assert cs.list_components(session, type_id=ctype.id) == []


def test_enum_values_by_definition_batches_multiple(session: Session) -> None:
    ctype = cs.create_type(session, "capacitor")
    dielectric = cs.add_parameter_definition(
        session,
        ctype.id,
        name="dielectric",
        label="Dielectric",
        data_type=ParameterDataType.ENUM,
        enum_values=["X7R", "C0G"],
    )
    package = cs.add_parameter_definition(
        session,
        ctype.id,
        name="package",
        label="Package",
        data_type=ParameterDataType.ENUM,
        enum_values=["0402", "0603"],
    )
    plain = cs.add_parameter_definition(
        session,
        ctype.id,
        name="capacitance",
        label="Capacitance",
        data_type=ParameterDataType.NUMBER,
    )

    grouped = cs.enum_values_by_definition(
        session, [dielectric.id, package.id, plain.id]
    )
    assert grouped == {
        dielectric.id: ["X7R", "C0G"],
        package.id: ["0402", "0603"],
    }
    # Non-enum definitions never appear as keys; an empty request is a no-op.
    assert plain.id not in grouped
    assert cs.enum_values_by_definition(session, []) == {}


def test_set_text_and_bool_parameters(session: Session) -> None:
    ctype = cs.create_type(session, "led")
    color = cs.add_parameter_definition(
        session,
        ctype.id,
        name="color",
        label="Color",
        data_type=ParameterDataType.TEXT,
    )
    rohs = cs.add_parameter_definition(
        session,
        ctype.id,
        name="rohs",
        label="RoHS",
        data_type=ParameterDataType.BOOL,
    )
    component = cs.create_component(session, ctype.id)

    text_param = cs.set_parameter_value(session, component.id, color.id, "red")
    assert text_param.value_text == "red"
    assert text_param.value_num is None

    bool_param = cs.set_parameter_value(session, component.id, rohs.id, True)
    assert bool_param.value_bool is True
    assert bool_param.value_text is None


def test_text_and_bool_parameters_reject_wrong_types(session: Session) -> None:
    ctype = cs.create_type(session, "led")
    color = cs.add_parameter_definition(
        session,
        ctype.id,
        name="color",
        label="Color",
        data_type=ParameterDataType.TEXT,
    )
    rohs = cs.add_parameter_definition(
        session,
        ctype.id,
        name="rohs",
        label="RoHS",
        data_type=ParameterDataType.BOOL,
    )
    component = cs.create_component(session, ctype.id)

    with pytest.raises(ValidationError):
        cs.set_parameter_value(session, component.id, color.id, 123)
    with pytest.raises(ValidationError):
        cs.set_parameter_value(session, component.id, rohs.id, "yes")


def test_non_enum_definition_rejects_enum_values(session: Session) -> None:
    ctype = cs.create_type(session, "resistor")
    with pytest.raises(ValidationError):
        cs.add_parameter_definition(
            session,
            ctype.id,
            name="resistance",
            label="Resistance",
            data_type=ParameterDataType.NUMBER,
            enum_values=["1k", "10k"],
        )


def test_set_parameter_updates_existing_value(session: Session) -> None:
    ctype = cs.create_type(session, "resistor")
    definition = cs.add_parameter_definition(
        session,
        ctype.id,
        name="resistance",
        label="Resistance",
        data_type=ParameterDataType.NUMBER,
    )
    component = cs.create_component(session, ctype.id)

    cs.set_parameter_value(session, component.id, definition.id, 1000.0)
    param = cs.set_parameter_value(session, component.id, definition.id, 2200.0)
    assert param.value_num == 2200.0


def test_set_parameter_rejects_definition_from_other_type(session: Session) -> None:
    resistor = cs.create_type(session, "resistor")
    capacitor = cs.create_type(session, "capacitor")
    foreign = cs.add_parameter_definition(
        session,
        capacitor.id,
        name="capacitance",
        label="Capacitance",
        data_type=ParameterDataType.NUMBER,
    )
    component = cs.create_component(session, resistor.id)
    with pytest.raises(ValidationError):
        cs.set_parameter_value(session, component.id, foreign.id, 1e-6)


def test_add_parameter_definition_rejects_blank_name(session: Session) -> None:
    ctype = cs.create_type(session, "resistor")
    with pytest.raises(ValidationError):
        cs.add_parameter_definition(
            session,
            ctype.id,
            name="  ",
            label="X",
            data_type=ParameterDataType.TEXT,
        )


def test_set_enum_parameter_rejects_non_string(session: Session) -> None:
    ctype = cs.create_type(session, "capacitor")
    definition = cs.add_parameter_definition(
        session,
        ctype.id,
        name="dielectric",
        label="Dielectric",
        data_type=ParameterDataType.ENUM,
        enum_values=["X7R", "C0G"],
    )
    component = cs.create_component(session, ctype.id)
    with pytest.raises(ValidationError):
        cs.set_parameter_value(session, component.id, definition.id, 123)


def test_hard_delete_component_removes_parameters_and_stock(
    session: Session,
) -> None:
    """Deleting a component with EAV/stock rows cascades to those rows (§20)."""
    from app.models.component import Component

    ctype = cs.create_type(session, "resistor")
    definition = cs.add_parameter_definition(
        session,
        ctype.id,
        name="tolerance",
        label="Tolerance",
        data_type=ParameterDataType.TEXT,
    )
    component = cs.create_component(session, ctype.id)
    cs.set_parameter_value(session, component.id, definition.id, "1%")

    cs.hard_delete_component(session, component.id)
    # The component and its EAV rows are gone; re-fetching the component raises.
    assert session.get(Component, component.id) is None
    with pytest.raises(NotFoundError):
        cs.list_parameter_values(session, component.id)


def test_hard_delete_component_removes_its_links(session: Session) -> None:
    """Links have no FK cascade, so the hard delete must clear them explicitly."""
    from app.models.enums import LinkKind
    from app.models.link import Link
    from app.services import link_service as ls
    from sqlmodel import select

    ctype = cs.create_type(session, "resistor")
    component = cs.create_component(session, ctype.id)
    ls.create_link(
        session,
        entity_type="component",
        entity_id=component.id,
        kind=LinkKind.SHOP,
        url="https://www.tme.eu/pl/details/x/y/",
    )

    cs.hard_delete_component(session, component.id)
    remaining = session.exec(
        select(Link).where(Link.entity_id == component.id)
    ).all()
    assert remaining == []


def _number_component(session: Session, unit: str):  # type: ignore[no-untyped-def]
    """A component whose type has one NUMBER parameter with the given unit."""
    ctype = cs.create_type(session, "part")
    definition = cs.add_parameter_definition(
        session,
        ctype.id,
        name="value",
        label="Value",
        data_type=ParameterDataType.NUMBER,
        unit=unit,
    )
    component = cs.create_component(session, ctype.id)
    return component, definition


def test_set_number_parameter_accepts_engineering_notation(session: Session) -> None:
    component, definition = _number_component(session, unit="Ω")
    assert (
        cs.set_parameter_value(session, component.id, definition.id, "4k7").value_num
        == 4700.0
    )
    # A raw number is still stored as-is.
    assert (
        cs.set_parameter_value(session, component.id, definition.id, 330).value_num
        == 330.0
    )


def test_set_number_parameter_ignores_trailing_unit(session: Session) -> None:
    component, definition = _number_component(session, unit="F")
    assert (
        cs.set_parameter_value(
            session, component.id, definition.id, "100 nF"
        ).value_num
        == 1e-7
    )


def test_set_number_parameter_rejects_unreadable_value(session: Session) -> None:
    component, definition = _number_component(session, unit="F")
    with pytest.raises(ValidationError):
        cs.set_parameter_value(session, component.id, definition.id, "not a number")
    with pytest.raises(ValidationError):
        cs.set_parameter_value(session, component.id, definition.id, True)
    # A non-finite raw number (Pydantic accepts inf/nan by default) is rejected.
    with pytest.raises(ValidationError):
        cs.set_parameter_value(session, component.id, definition.id, float("inf"))


def test_create_component_with_values_applies_own_and_inherited(
    session: Session,
) -> None:
    parent = cs.create_type(session, "passive")
    cs.add_parameter_definition(
        session, parent.id, name="tolerance", label="Tol",
        data_type=ParameterDataType.TEXT,
    )
    ctype = cs.create_type(session, "resistor", parent_id=parent.id)
    resistance = cs.add_parameter_definition(
        session, ctype.id, name="resistance", label="Resistance",
        data_type=ParameterDataType.NUMBER, unit="Ω",
    )
    tolerance = next(
        d
        for d in cs.get_effective_parameter_definitions(session, ctype.id)
        if d.name == "tolerance"
    )

    component = cs.create_component_with_values(
        session, ctype.id, mpn="R-100",
        # Engineering-notation number plus an inherited text parameter.
        values=[(resistance.id, "4k7"), (tolerance.id, "1%")],
    )
    values = {
        v.parameter_definition_id: v
        for v in cs.list_parameter_values(session, component.id)
    }
    assert values[resistance.id].value_num == 4700.0
    assert values[tolerance.id].value_text == "1%"


def test_create_component_with_values_is_atomic_on_bad_value(
    session: Session,
) -> None:
    ctype = cs.create_type(session, "resistor")
    other = cs.create_type(session, "capacitor")
    foreign = cs.add_parameter_definition(
        session, other.id, name="capacitance", label="C",
        data_type=ParameterDataType.NUMBER, unit="F",
    )
    # A definition from another type must abort the whole create.
    with pytest.raises(ValidationError):
        cs.create_component_with_values(
            session, ctype.id, values=[(foreign.id, "1n")]
        )
    assert cs.list_components(session, type_id=ctype.id) == []


def test_create_component_with_values_rejects_duplicate_definition(
    session: Session,
) -> None:
    ctype = cs.create_type(session, "resistor")
    definition = cs.add_parameter_definition(
        session, ctype.id, name="resistance", label="R",
        data_type=ParameterDataType.NUMBER, unit="Ω",
    )
    with pytest.raises(ValidationError):
        cs.create_component_with_values(
            session, ctype.id, values=[(definition.id, "1k"), (definition.id, "2k")]
        )
    assert cs.list_components(session, type_id=ctype.id) == []


def test_create_component_with_no_values(session: Session) -> None:
    ctype = cs.create_type(session, "resistor")
    component = cs.create_component_with_values(session, ctype.id, mpn="R-1")
    assert component.id is not None
    assert cs.list_parameter_values(session, component.id) == []


def test_create_component_with_values_audits_initial_values(session: Session) -> None:
    from app.seed import ensure_system_user
    from app.services import audit_service as audit

    user = ensure_system_user(session)
    ctype = cs.create_type(session, "resistor")
    definition = cs.add_parameter_definition(
        session, ctype.id, name="resistance", label="R",
        data_type=ParameterDataType.NUMBER, unit="Ω",
    )
    component = cs.create_component_with_values(
        session, ctype.id, values=[(definition.id, "4k7")], user_id=user.id
    )
    entries = audit.list_entries(
        session, entity_type="component", entity_id=component.id
    )
    assert len(entries) == 1
    assert entries[0].old_value is None


def test_create_component_with_values_skips_audit_without_user(
    session: Session,
) -> None:
    from app.services import audit_service as audit

    ctype = cs.create_type(session, "resistor")
    definition = cs.add_parameter_definition(
        session, ctype.id, name="resistance", label="R",
        data_type=ParameterDataType.NUMBER, unit="Ω",
    )
    component = cs.create_component_with_values(
        session, ctype.id, values=[(definition.id, "4k7")]
    )
    assert (
        audit.list_entries(session, entity_type="component", entity_id=component.id)
        == []
    )


def test_find_duplicate_component_matches_case_insensitively(session: Session) -> None:
    ctype = cs.create_type(session, "resistor")
    cs.create_component(session, ctype.id, mpn="R-100", manufacturer="YAGEO")
    # Same pair in any case is a duplicate.
    assert (
        cs.find_duplicate_component(session, mpn="r-100", manufacturer="yageo")
        is not None
    )
    # A different manufacturer with the same MPN is NOT a duplicate.
    assert (
        cs.find_duplicate_component(session, mpn="R-100", manufacturer="TDK") is None
    )


def test_find_duplicate_component_exempts_a_blank_mpn(session: Session) -> None:
    ctype = cs.create_type(session, "resistor")
    cs.create_component(session, ctype.id, manufacturer="YAGEO")  # no MPN
    # A part with no MPN can't be de-duplicated — two must be allowed to coexist.
    for mpn in (None, "", "   "):
        assert (
            cs.find_duplicate_component(session, mpn=mpn, manufacturer="YAGEO") is None
        )


def test_find_duplicate_component_is_manufacturer_null_aware(session: Session) -> None:
    ctype = cs.create_type(session, "resistor")
    cs.create_component(session, ctype.id, mpn="C-5")  # MPN, no manufacturer
    # A blank manufacturer matches only another blank one.
    assert (
        cs.find_duplicate_component(session, mpn="C-5", manufacturer=None) is not None
    )
    assert cs.find_duplicate_component(session, mpn="C-5", manufacturer="TDK") is None
    # …and a set manufacturer does not match the MPN-only row.
    cs.create_component(session, ctype.id, mpn="C-6", manufacturer="TDK")
    assert cs.find_duplicate_component(session, mpn="C-6", manufacturer=None) is None


def test_find_duplicate_component_folds_non_ascii_case(session: Session) -> None:
    # SQLite's lower() is ASCII-only, so folding must happen in Python or an
    # uppercase-accented manufacturer escapes the check.
    ctype = cs.create_type(session, "resistor")
    cs.create_component(session, ctype.id, mpn="R-1", manufacturer="ÉCLAIR")
    assert (
        cs.find_duplicate_component(session, mpn="R-1", manufacturer="éclair")
        is not None
    )


def test_find_duplicate_component_catches_legacy_non_space_whitespace(
    session: Session,
) -> None:
    from sqlalchemy import text

    # A row created before normalisation (or by a direct client) with tab/NBSP
    # padding. SQLite's trim() strips only 0x20, so the stored side must be folded
    # in Python for this to be caught.
    ctype = cs.create_type(session, "resistor")
    session.connection().execute(
        text(
            "INSERT INTO components (type_id, mpn, manufacturer, mounting_type, "
            "status) VALUES (:t, :m, NULL, 'Other', 'active')"
        ),
        {"t": ctype.id, "m": "\tR-9 "},  # tab + non-breaking space
    )
    session.commit()
    assert (
        cs.find_duplicate_component(session, mpn="R-9", manufacturer=None) is not None
    )


def test_create_normalises_blank_and_whitespace_mpn_manufacturer(
    session: Session,
) -> None:
    ctype = cs.create_type(session, "resistor")
    component = cs.create_component(
        session, ctype.id, mpn="  R-100  ", manufacturer="   "
    )
    # Trimmed, and a whitespace-only manufacturer becomes None — so it can't slip
    # past the de-dup, which compares trimmed values.
    assert component.mpn == "R-100"
    assert component.manufacturer is None
    # A blank ("" / whitespace) manufacturer matches the normalised row.
    assert (
        cs.find_duplicate_component(session, mpn=" r-100 ", manufacturer="")
        is not None
    )


def test_find_duplicate_component_ignores_soft_deleted(session: Session) -> None:
    from datetime import UTC, datetime

    ctype = cs.create_type(session, "resistor")
    component = cs.create_component(session, ctype.id, mpn="R-9", manufacturer="YAGEO")
    component.deleted_at = datetime.now(UTC)
    session.add(component)
    session.commit()
    # A deleted part doesn't block re-adding it.
    assert (
        cs.find_duplicate_component(session, mpn="R-9", manufacturer="YAGEO") is None
    )


def _resistor_with_params(session: Session):  # type: ignore[no-untyped-def]
    ctype = cs.create_type(session, "resistor")
    resistance = cs.add_parameter_definition(
        session, ctype.id, name="resistance", label="R",
        data_type=ParameterDataType.NUMBER, unit="Ω",
    )
    tolerance = cs.add_parameter_definition(
        session, ctype.id, name="tolerance", label="Tol",
        data_type=ParameterDataType.TEXT,
    )
    return ctype, resistance, tolerance


def test_update_component_edits_fields_and_values(session: Session) -> None:
    from app.models.enums import MountingType

    ctype, resistance, tolerance = _resistor_with_params(session)
    component = cs.create_component_with_values(
        session, ctype.id, mpn="R-1", manufacturer="YAGEO",
        values=[(resistance.id, "1k"), (tolerance.id, "5%")],
    )
    cs.update_component(
        session, component.id, manufacturer="TDK", package="0402",
        mounting_type=MountingType.SMT, notes="edited",
        values=[(resistance.id, "2k2"), (tolerance.id, "1%")],
    )
    session.refresh(component)
    assert (component.manufacturer, component.package) == ("TDK", "0402")
    assert component.mounting_type is MountingType.SMT
    assert component.notes == "edited"
    values = {
        v.parameter_definition_id: cs._current_value(v)
        for v in cs.list_parameter_values(session, component.id)
    }
    assert values[resistance.id] == 2200.0
    assert values[tolerance.id] == "1%"


def test_update_component_leaves_type_and_mpn_untouched(session: Session) -> None:
    ctype, resistance, _ = _resistor_with_params(session)
    component = cs.create_component_with_values(session, ctype.id, mpn="R-1")
    cs.update_component(session, component.id, manufacturer="TDK")
    session.refresh(component)
    # update_component has no type/mpn parameter, so they can never change here.
    assert component.mpn == "R-1"
    assert component.type_id == ctype.id


def test_update_component_clears_a_blank_parameter(session: Session) -> None:
    ctype, resistance, tolerance = _resistor_with_params(session)
    component = cs.create_component_with_values(
        session, ctype.id, values=[(tolerance.id, "5%")],
    )
    cs.update_component(session, component.id, values=[(tolerance.id, None)])
    stored = {
        v.parameter_definition_id
        for v in cs.list_parameter_values(session, component.id)
    }
    assert tolerance.id not in stored  # the value row is deleted


def test_update_component_is_atomic_on_a_bad_value(session: Session) -> None:
    from app.models.component import Component

    ctype, resistance, _ = _resistor_with_params(session)
    component = cs.create_component_with_values(
        session, ctype.id, manufacturer="YAGEO", values=[(resistance.id, "1k")],
    )
    # A non-numeric resistance aborts the whole edit — the manufacturer change with it.
    with pytest.raises(ValidationError):
        cs.update_component(
            session, component.id, manufacturer="TDK",
            values=[(resistance.id, "not-a-number")],
        )
    fresh = session.get(Component, component.id)
    assert fresh is not None and fresh.manufacturer == "YAGEO"


def test_update_component_rejects_a_foreign_parameter(session: Session) -> None:
    ctype, _, _ = _resistor_with_params(session)
    other = cs.create_type(session, "capacitor")
    foreign = cs.add_parameter_definition(
        session, other.id, name="capacitance", label="C",
        data_type=ParameterDataType.NUMBER, unit="F",
    )
    component = cs.create_component(session, ctype.id)
    with pytest.raises(ValidationError):
        cs.update_component(session, component.id, values=[(foreign.id, "1n")])


def test_update_component_audits_each_change(session: Session) -> None:
    from app.services import audit_service as audit

    ctype, resistance, _ = _resistor_with_params(session)
    component = cs.create_component_with_values(
        session, ctype.id, manufacturer="YAGEO", values=[(resistance.id, "1k")],
    )
    cs.update_component(
        session, component.id, manufacturer="TDK",
        values=[(resistance.id, "2k")], user_id=7,
    )
    fields = {
        e.field
        for e in audit.list_entries(
            session, entity_type="component", entity_id=component.id
        )
    }
    assert "manufacturer" in fields
    assert audit.parameter_field("resistance") in fields


def test_update_component_skips_audit_for_an_unchanged_field(session: Session) -> None:
    from app.services import audit_service as audit

    ctype, _, _ = _resistor_with_params(session)
    component = cs.create_component(session, ctype.id, manufacturer="YAGEO")
    # Re-set the same manufacturer — a no-op must not write a phantom audit row.
    cs.update_component(session, component.id, manufacturer="YAGEO", user_id=7)
    fields = [
        e.field
        for e in audit.list_entries(
            session, entity_type="component", entity_id=component.id
        )
    ]
    assert "manufacturer" not in fields


def test_update_component_validates_an_enum_value(session: Session) -> None:
    ctype = cs.create_type(session, "capacitor")
    dielectric = cs.add_parameter_definition(
        session, ctype.id, name="dielectric", label="Dielectric",
        data_type=ParameterDataType.ENUM, enum_values=["X7R", "C0G"],
    )
    component = cs.create_component_with_values(
        session, ctype.id, values=[(dielectric.id, "X7R")],
    )
    # A valid token is accepted…
    cs.update_component(session, component.id, values=[(dielectric.id, "C0G")])
    values = {
        v.parameter_definition_id: cs._current_value(v)
        for v in cs.list_parameter_values(session, component.id)
    }
    assert values[dielectric.id] == "C0G"
    # …a token outside the allowed set is rejected.
    with pytest.raises(ValidationError):
        cs.update_component(session, component.id, values=[(dielectric.id, "NP0")])


def test_update_component_allows_a_pure_case_change_of_own_manufacturer(
    session: Session,
) -> None:
    ctype, _, _ = _resistor_with_params(session)
    component = cs.create_component(session, ctype.id, mpn="R-1", manufacturer="YAGEO")
    # Recasing the same row's manufacturer matches self (excluded) — not a duplicate.
    cs.update_component(session, component.id, manufacturer="yageo")
    session.refresh(component)
    assert component.manufacturer == "yageo"


def test_update_component_blocks_a_case_folded_manufacturer_collision(
    session: Session,
) -> None:
    from app.services.errors import DuplicateComponentError

    ctype, _, _ = _resistor_with_params(session)
    cs.create_component(session, ctype.id, mpn="R-9", manufacturer="ÉCLAIR")
    other = cs.create_component(session, ctype.id, mpn="R-9", manufacturer="TDK")
    # Editing TDK's part to "éclair" collides with "ÉCLAIR" under casefold — blocked
    # (the hardened matcher from #51 is reused through the edit path).
    with pytest.raises(DuplicateComponentError):
        cs.update_component(session, other.id, manufacturer="éclair")


def test_update_component_trims_a_text_parameter(session: Session) -> None:
    ctype, _, tolerance = _resistor_with_params(session)
    component = cs.create_component(session, ctype.id)
    cs.update_component(session, component.id, values=[(tolerance.id, "  5%  ")])
    values = {
        v.parameter_definition_id: cs._current_value(v)
        for v in cs.list_parameter_values(session, component.id)
    }
    assert values[tolerance.id] == "5%"  # stored trimmed, not "  5%  "
