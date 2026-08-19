"""Tests for the enrichment matching engine (app/services/matching.py).

These cover the logic that used to live in the browser (component_dialog.js) and now
runs server-side: type/mounting/package inference and pulling parameter values out of a
shop's structured attributes and free-text descriptions, all driven by editable rules.
"""

from __future__ import annotations

import pytest
from app.models.enums import MatchDomain, MountingType
from app.models.enums import ParameterDataType as DT
from app.models.match_rule import MatchRule
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


def _cable_with_type_enum(session: Session) -> tuple[int, int]:
    """A cable type with a "Type" enum (Flat/Round) — for free-text enum matching."""
    ctype = cs.create_type(session, "cable")
    type_def = cs.add_parameter_definition(
        session, ctype.id, name="ctype", label="Type",
        data_type=DT.ENUM, enum_values=["Flat", "Round"], sort_order=0,
    )
    mrs.seed_default_rules(session)
    return ctype.id, type_def.id


def test_enum_value_matched_from_free_text_by_a_polish_alias(session: Session) -> None:
    _, def_id = _cable_with_type_enum(session)
    mrs.create_rule(
        session, domain=MatchDomain.ENUM_VALUE, alias="wstążkowy",
        canonical="Flat", parameter_definition_id=def_id,
    )
    # A bare adjective in the description (no "label: value" structure) — the
    # invoice case that used to leave the enum unset.
    product = ProductData(category="cable", description="Przewód wstążkowy 40 żył")
    assert _by_id(build_proposal(session, product))[def_id] == "Flat"


def test_enum_free_text_match_ignores_polish_accents(session: Session) -> None:
    _, def_id = _cable_with_type_enum(session)
    mrs.create_rule(
        session, domain=MatchDomain.ENUM_VALUE, alias="wstążkowy",
        canonical="Flat", parameter_definition_id=def_id,
    )
    # The shop wrote it without diacritics — must still hit the accented alias.
    product = ProductData(category="cable", description="kabel wstazkowy plaski")
    assert _by_id(build_proposal(session, product))[def_id] == "Flat"


def test_enum_value_matched_from_free_text_by_a_multi_word_alias(
    session: Session,
) -> None:
    _, def_id = _cable_with_type_enum(session)
    mrs.create_rule(
        session, domain=MatchDomain.ENUM_VALUE, alias="taśma płaska",
        canonical="Flat", parameter_definition_id=def_id,
    )
    # The alias spans two words in the description (no "label: value" structure); it
    # must still match, not just fire on structured shop attributes.
    product = ProductData(category="cable", description="Przewód taśma płaska 40 żył")
    assert _by_id(build_proposal(session, product))[def_id] == "Flat"


def test_enum_value_matched_from_free_text_by_a_hyphenated_alias(
    session: Session,
) -> None:
    _, def_id = _cable_with_type_enum(session)
    mrs.create_rule(
        session, domain=MatchDomain.ENUM_VALUE, alias="flat-flex",
        canonical="Flat", parameter_definition_id=def_id,
    )
    # "flat-flex" folds to "flatflex"; the description tokenises to "flat" + "flex",
    # which the adjacent-word window rejoins.
    product = ProductData(category="cable", description="Cable flat flex 20 way")
    assert _by_id(build_proposal(session, product))[def_id] == "Flat"


def test_enum_free_text_does_not_match_an_unrelated_description(
    session: Session,
) -> None:
    _, def_id = _cable_with_type_enum(session)
    mrs.create_rule(
        session, domain=MatchDomain.ENUM_VALUE, alias="wstążkowy",
        canonical="Flat", parameter_definition_id=def_id,
    )
    product = ProductData(category="cable", description="Przewód okrągły ekranowany")
    assert def_id not in _by_id(build_proposal(session, product))


def test_enum_free_text_alias_to_a_non_member_is_dropped(session: Session) -> None:
    _, def_id = _cable_with_type_enum(session)
    # A stale rule whose target is no longer one of the parameter's values. The store
    # blocks creating one now, but an enum value could be removed after the fact — so
    # the engine keeps its own membership gate and must refuse to emit it (the create
    # path would reject a non-member). Inserted directly to bypass the store's guard.
    session.add(
        MatchRule(
            domain=MatchDomain.ENUM_VALUE, alias="wstążkowy",
            canonical="Ribbon", parameter_definition_id=def_id,
        )
    )
    session.commit()
    product = ProductData(category="cable", description="Przewód wstążkowy")
    assert def_id not in _by_id(build_proposal(session, product))


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


def test_ic_alias_does_not_match_inside_a_word_ending_in_ic_colon(
    session: Session,
) -> None:
    # "ic:" is a substring of "electronic:" — the left word-boundary anchor must keep
    # the IC-type rule from firing on it.
    cs.create_type(session, "ic")
    mrs.seed_default_rules(session)
    product = ProductData(shop_category="Electronic: modules", description="a module")
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


def test_mounting_from_a_structured_attribute_only(session: Session) -> None:
    # Mouser states mounting only as a structured attribute ("Mounting Style:
    # SMD/SMT"), never in the free text. The mounting scan must reach the attribute
    # value too, or nothing sets the field. (The seeded "SMD"->SMT rule fires on the
    # "SMD" inside "SMD/SMT"; the point is that SMT is resolved at all.)
    _resistor(session)
    product = ProductData(
        category="resistor",
        description="TMUXHS4412 High-Bandwidth Multiplexer",
        parameters=[("Mounting Style", "SMD/SMT")],
    )
    assert build_proposal(session, product).mounting_type is MountingType.SMT


def test_description_mounting_beats_an_incidental_attribute_word(
    session: Session,
) -> None:
    # The description is the higher-confidence source. A stray "SMD" in some attribute
    # value must not outrank an explicit "through-hole" in the text — attributes are a
    # fallback, not a rival. (_resolve_mounting returns the first matching RULE, and
    # SMD sorts before THT, so a single merged blob would wrongly resolve SMT here.)
    _resistor(session)
    product = ProductData(
        category="resistor",
        description="Through-hole axial resistor, 1/4W",
        parameters=[("Alternative package", "also available as SMD")],
    )
    assert build_proposal(session, product).mounting_type is MountingType.THT
def test_package_rule_reads_a_case_out_of_the_description(session: Session) -> None:
    # The EIA pattern only knows chip sizes; a named case ("obudowa SOT-23") needs a
    # rule, which is what the package domain is for.
    _resistor(session)
    mrs.create_rule(
        session, domain=MatchDomain.PACKAGE, alias="SOT-23", canonical="SOT-23"
    )
    product = ProductData(
        category="resistor", description="Tranzystor w obudowie SOT-23, 100 mA"
    )
    assert build_proposal(session, product).package == "SOT-23"


def test_package_rule_normalizes_the_shop_s_own_package_field(
    session: Session,
) -> None:
    # A shop's own package field still wins over the description — but it is spelled
    # the shop's way, so the rule folds it to the one name the shelf uses.
    _resistor(session)
    mrs.create_rule(
        session, domain=MatchDomain.PACKAGE, alias="SOT23-3", canonical="SOT-23"
    )
    product = ProductData(category="resistor", description="0402", package="sot23-3")
    assert build_proposal(session, product).package == "SOT-23"
    # No rule for it: the shop's wording is kept as it came, not dropped.
    unknown = ProductData(category="resistor", description="0402", package="DPAK")
    assert build_proposal(session, unknown).package == "DPAK"


@pytest.mark.parametrize(
    "broad_order,specific_order,expected",
    [(0, 1, "SOT-23"), (1, 0, "SOT-23-3")],
)
def test_two_overlapping_package_aliases_are_settled_by_order(
    session: Session, broad_order: int, specific_order: int, expected: str
) -> None:
    # Aliases that BOTH match the same text, which a separator makes ordinary:
    # "SOT-23-3" contains "SOT-23", and a hyphen is a word boundary, so each one
    # fires on its own. Swapping the orders swaps the answer — otherwise this would
    # be measuring the regex rather than the ordering it claims to test.
    _resistor(session)
    mrs.create_rule(
        session, domain=MatchDomain.PACKAGE, alias="SOT-23", canonical="SOT-23",
        sort_order=broad_order,
    )
    mrs.create_rule(
        session, domain=MatchDomain.PACKAGE, alias="SOT-23-3",
        canonical="SOT-23-3", sort_order=specific_order,
    )
    product = ProductData(category="resistor", description="Tranzystor SOT-23-3")
    assert build_proposal(session, product).package == expected


def test_a_package_alias_does_not_fire_on_a_longer_case_name(
    session: Session,
) -> None:
    # "TO-220AB" is a different case from "TO-220", so a TO-220 rule leaves it
    # alone: the dialog shows an empty package the user fills in, rather than a
    # confidently wrong one. Folding a family is said outright, as its own rule.
    _resistor(session)
    mrs.create_rule(
        session, domain=MatchDomain.PACKAGE, alias="TO-220", canonical="TO-220"
    )
    product = ProductData(category="resistor", description="Radiator TO-220AB")
    assert build_proposal(session, product).package is None
    mrs.create_rule(
        session, domain=MatchDomain.PACKAGE, alias="TO-220AB", canonical="TO-220"
    )
    assert build_proposal(session, product).package == "TO-220"


def test_the_eia_pattern_still_backs_the_package_rules(session: Session) -> None:
    _resistor(session)
    mrs.create_rule(
        session, domain=MatchDomain.PACKAGE, alias="TO-220", canonical="TO-220"
    )
    # No alias matched: the built-in chip-size pattern still fills the package in.
    eia = ProductData(category="resistor", description="Thick Film Resistor 0805")
    assert build_proposal(session, eia).package == "0805"
    # Nothing at all to go on.
    assert build_proposal(session, ProductData(category="resistor")).package is None
