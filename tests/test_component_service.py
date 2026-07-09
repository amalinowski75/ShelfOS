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
