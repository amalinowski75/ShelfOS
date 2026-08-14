"""Tests for the enrichment matching engine (app/services/matching.py).

These cover the logic that used to live in the browser (component_dialog.js) and now
runs server-side: type/mounting/package inference and pulling parameter values out of a
shop's structured attributes and free-text descriptions, all driven by editable rules.
"""

from __future__ import annotations

import pytest
from app.models.enums import MatchDomain, MountingType
from app.models.enums import ParameterDataType as DT
from app.services import component_service as cs
from app.services import match_rule_service as mrs
from app.services.matching import build_proposal
from app.services.shops.base import ProductData
from sqlmodel import Session


def _resistor(session: Session) -> dict[str, int]:
    """A resistor type with Ω/W/% number params, a package text, a dielectric enum."""
    rtype = cs.create_type(session, "resistor")
    ids = {
        "type": rtype.id,
        "resistance": cs.add_parameter_definition(
            session, rtype.id, name="resistance", label="Resistance",
            data_type=DT.NUMBER, unit="Ω", sort_order=0,
        ).id,
        "power": cs.add_parameter_definition(
            session, rtype.id, name="power", label="Power",
            data_type=DT.NUMBER, unit="W", sort_order=2,
        ).id,
        "tolerance": cs.add_parameter_definition(
            session, rtype.id, name="tolerance", label="Tolerance",
            data_type=DT.NUMBER, unit="%", sort_order=3,
        ).id,
        "dielectric": cs.add_parameter_definition(
            session, rtype.id, name="dielectric", label="Dielectric",
            data_type=DT.ENUM, enum_values=["C0G", "X7R"], sort_order=4,
        ).id,
    }
    mrs.seed_default_rules(session)  # type + mounting defaults
    return ids


def _by_id(proposal) -> dict[int, object]:  # type: ignore[no-untyped-def]
    return dict(proposal.parameters)


def test_resolves_type_and_mounting_from_default_rules(session: Session) -> None:
    ids = _resistor(session)
    product = ProductData(
        category="resistor",
        shop_category="Chip Resistor - Surface Mount",
        description="Thick Film Resistors - SMD 0402",
    )
    proposal = build_proposal(session, product)
    assert proposal.type_id == ids["type"]
    assert proposal.mounting_type is MountingType.SMT
    assert proposal.package == "0402"


def test_structured_params_are_matched_and_number_cleaned(session: Session) -> None:
    ids = _resistor(session)
    product = ProductData(
        category="resistor",
        parameters=[("Resistance", "10 kOhms"), ("Tolerance", "1%")],
    )
    values = _by_id(build_proposal(session, product))
    assert values[ids["resistance"]] == "10k"  # engineering-cleaned
    assert values[ids["tolerance"]] == "1"


def test_values_read_from_the_description_by_unit(session: Session) -> None:
    ids = _resistor(session)
    # Mouser-style: attributes are only logistics, so the specs come from the text.
    product = ProductData(
        category="resistor",
        description="Thick Film Resistors - SMD 1.2 kOhms 50 V 100 mW 1 % 0402",
        parameters=[("Packaging", "Reel")],
    )
    values = _by_id(build_proposal(session, product))
    assert values[ids["resistance"]] == "1.2k"
    assert values[ids["power"]] == "100m"
    # The stray "50 V" is ignored — this type has no volt parameter to hold it.
    assert all(v == "50" for v in values.values()) is False


def test_fractional_power_is_not_read_as_its_denominator(session: Session) -> None:
    ids = _resistor(session)
    product = ProductData(
        category="resistor",
        description="Thick Film Resistors 1.2kOhms 1/16W 0402 5%",
    )
    values = _by_id(build_proposal(session, product))
    assert values[ids["power"]] == "0.0625"  # 1/16 W, not 16 W


def test_unitless_value_fills_the_primary_parameter(session: Session) -> None:
    ids = _resistor(session)
    product = ProductData(
        category="resistor",
        description="Thin Film Resistors - SMD TNPW-0402 1.2K 0.1% T-9",
    )
    values = _by_id(build_proposal(session, product))
    assert values[ids["resistance"]] == "1.2k"  # the lowest-order NUMBER def
    # "0402" is a package code, not a value.
    assert build_proposal(session, product).package == "0402"


def test_enum_alias_resolves_and_a_non_member_is_dropped(session: Session) -> None:
    ids = _resistor(session)
    mrs.create_rule(
        session, domain=MatchDomain.ENUM_VALUE, alias="X7R Dielectric",
        canonical="X7R", parameter_definition_id=ids["dielectric"],
    )
    good = ProductData(
        category="resistor", parameters=[("Dielectric", "X7R Dielectric")]
    )
    proposal = build_proposal(session, good)
    assert _by_id(proposal)[ids["dielectric"]] == "X7R"

    # A value that is neither an allowed token nor a known alias must NOT be emitted
    # (the create path rejects a non-member and would abort the whole component).
    bad = ProductData(category="resistor", parameters=[("Dielectric", "Z9U")])
    proposal = build_proposal(session, bad)
    assert ids["dielectric"] not in _by_id(proposal)
    assert ("Dielectric", "Z9U") in proposal.unmatched


def test_param_name_synonym_matches_cross_language(session: Session) -> None:
    ids = _resistor(session)
    mrs.create_rule(
        session, domain=MatchDomain.PARAM_NAME, alias="Rezystancja",
        canonical="resistance", parameter_definition_id=ids["resistance"],
    )
    product = ProductData(category="resistor", parameters=[("Rezystancja", "4.7k")])
    assert _by_id(build_proposal(session, product))[ids["resistance"]] == "4.7k"


def test_key_value_fragment_in_a_description(session: Session) -> None:
    ctype = cs.create_type(session, "connector")
    pins = cs.add_parameter_definition(
        session, ctype.id, name="pins", label="PIN", data_type=DT.NUMBER
    )
    mrs.seed_default_rules(session)  # "connector" is among the default TYPE rules
    product = ProductData(
        description="Connector: pin strips; socket; female; PIN: 40; THT; straight; 3A"
    )
    proposal = build_proposal(session, product)
    assert proposal.type_id == ctype.id
    assert proposal.mounting_type is MountingType.THT
    assert _by_id(proposal)[pins.id] == "40"


def test_ic_alias_does_not_match_inside_logic(session: Session) -> None:
    # The default "ic:" rule (colon-guarded) must not fire on "logic" or the like.
    cs.create_type(session, "ic")
    mrs.seed_default_rules(session)
    product = ProductData(description="Logic gate 74HC00 buffer")
    assert build_proposal(session, product).type_id is None


def test_no_type_means_no_parameters(session: Session) -> None:
    mrs.seed_default_rules(session)
    product = ProductData(description="Some Widget with 10 kOhms in it")
    proposal = build_proposal(session, product)
    assert proposal.type_id is None
    assert proposal.parameters == []


def test_a_forced_type_skips_inference(session: Session) -> None:
    ids = _resistor(session)
    other = cs.create_type(session, "capacitor")
    # The text says resistor, but the caller forces capacitor (a dialog type change).
    product = ProductData(category="resistor", description="SMD resistor")
    proposal = build_proposal(session, product, type_id=other.id)
    assert proposal.type_id == other.id
    # No resistor params leak onto the forced type.
    assert ids["resistance"] not in _by_id(proposal)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("SMD 0402", MountingType.SMT),
        ("montaż przewlekany THT", MountingType.THT),
        ("montaż powierzchniowy", MountingType.SMT),
    ],
)
def test_mounting_words_including_polish(
    session: Session, text: str, expected: MountingType
) -> None:
    _resistor(session)
    product = ProductData(category="resistor", description=text)
    assert build_proposal(session, product).mounting_type is expected
