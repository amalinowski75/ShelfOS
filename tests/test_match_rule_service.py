"""Tests for the matching-rule store (app/services/match_rule_service.py)."""

from __future__ import annotations

import pytest
from app.models.enums import MatchDomain
from app.services import component_service as cs
from app.services import match_rule_service as mrs
from app.services.errors import ValidationError
from sqlmodel import Session


def test_seed_default_rules_is_idempotent(session: Session) -> None:
    first = mrs.seed_default_rules(session)
    assert first > 0
    assert mrs.seed_default_rules(session) == 0  # nothing new the second time
    rules = mrs.load_rules(session)
    assert rules.types  # migrated category keywords
    assert dict(rules.mountings)["smd"] == "SMT"


def test_load_rules_groups_and_normalizes(session: Session) -> None:
    ctype = cs.create_type(session, "resistor")
    definition = cs.add_parameter_definition(
        session, ctype.id, name="resistance", label="Resistance",
        data_type=cs.ParameterDataType.NUMBER, unit="Ω",
    )
    mrs.create_rule(
        session, domain=MatchDomain.PARAM_NAME, alias="Rezystancja",
        canonical="resistance", parameter_definition_id=definition.id,
    )
    rules = mrs.load_rules(session)
    # The alias is stored normalized (lowercase, punctuation stripped).
    assert "rezystancja" in rules.param_aliases[definition.id]


def test_scoped_rule_requires_a_definition(session: Session) -> None:
    with pytest.raises(ValidationError, match="must name a parameter definition"):
        mrs.create_rule(
            session, domain=MatchDomain.ENUM_VALUE, alias="X7R Dielectric",
            canonical="X7R",
        )


def test_global_rule_rejects_a_definition(session: Session) -> None:
    with pytest.raises(ValidationError, match="global"):
        mrs.create_rule(
            session, domain=MatchDomain.TYPE, alias="rezystor",
            canonical="resistor", parameter_definition_id=1,
        )


def test_scoped_rule_rejects_an_unknown_definition(session: Session) -> None:
    from app.services.errors import NotFoundError

    # SQLite doesn't enforce the FK, so the service must reject a dangling scope
    # itself — otherwise a permanently-unusable orphan rule is created.
    with pytest.raises(NotFoundError, match="parameter definition"):
        mrs.create_rule(
            session, domain=MatchDomain.ENUM_VALUE, alias="czerwony",
            canonical="red", parameter_definition_id=9999,
        )


def test_duplicate_alias_is_rejected(session: Session) -> None:
    mrs.create_rule(
        session, domain=MatchDomain.MOUNTING, alias="SMD", canonical="SMT"
    )
    with pytest.raises(ValidationError) as exc:
        # Same domain + same alias (case-insensitively) is a duplicate.
        mrs.create_rule(
            session, domain=MatchDomain.MOUNTING, alias="smd", canonical="SMT"
        )
    # The message names the clashing rule (its alias + target) so the admin sees
    # WHAT already exists, not just that something does.
    assert "already exists" in str(exc.value)
    assert "SMD" in str(exc.value)
    assert "SMT" in str(exc.value)


def test_blank_alias_or_target_is_rejected(session: Session) -> None:
    with pytest.raises(ValidationError):
        mrs.create_rule(
            session, domain=MatchDomain.TYPE, alias="  ", canonical="resistor"
        )


def test_delete_rule(session: Session) -> None:
    rule = mrs.create_rule(
        session, domain=MatchDomain.TYPE, alias="widget", canonical="ic"
    )
    mrs.delete_rule(session, rule.id)
    assert not mrs.list_rules(session, domain=MatchDomain.TYPE)


def test_update_rule_changes_alias_target_and_order(session: Session) -> None:
    rule = mrs.create_rule(
        session, domain=MatchDomain.TYPE, alias="rezystor", canonical="resistor"
    )
    updated = mrs.update_rule(
        session, rule.id, alias="opornik", canonical="resistor", sort_order=5
    )
    assert updated.alias == "opornik"
    assert updated.sort_order == 5
    # The rename is what the engine now loads.
    loaded = mrs.load_rules(session)
    assert ("opornik", "resistor") in loaded.types
    assert ("rezystor", "resistor") not in loaded.types


def test_update_rule_rejects_a_duplicate_alias(session: Session) -> None:
    mrs.create_rule(session, domain=MatchDomain.MOUNTING, alias="SMD", canonical="SMT")
    other = mrs.create_rule(
        session, domain=MatchDomain.MOUNTING, alias="THT", canonical="THT"
    )
    # Renaming onto an alias already used in the same domain is rejected, so the
    # same text never maps two different ways.
    with pytest.raises(ValidationError, match="already exists"):
        mrs.update_rule(session, other.id, alias="smd")
    # The rejected edit left the original alias intact.
    assert session.get(type(other), other.id).alias == "THT"


def test_update_rule_rejects_a_blank_alias(session: Session) -> None:
    rule = mrs.create_rule(
        session, domain=MatchDomain.TYPE, alias="widget", canonical="ic"
    )
    with pytest.raises(ValidationError):
        mrs.update_rule(session, rule.id, alias="   ")


def test_update_rule_can_rename_to_the_same_alias(session: Session) -> None:
    # Editing only the target must not trip the duplicate guard on the rule itself.
    rule = mrs.create_rule(
        session, domain=MatchDomain.TYPE, alias="chip", canonical="ic"
    )
    updated = mrs.update_rule(session, rule.id, alias="chip", canonical="mosfet")
    assert updated.canonical == "mosfet"


def test_create_rule_folds_a_mounting_target_to_its_enum_case(
    session: Session,
) -> None:
    # A mounting target is matched case-sensitively by the engine, so the store
    # normalises "tht" to the exact enum spelling instead of silently keeping a
    # value that would never fire.
    rule = mrs.create_rule(
        session, domain=MatchDomain.MOUNTING, alias="wire-through", canonical="tht"
    )
    assert rule.canonical == "THT"


def test_create_rule_rejects_an_unknown_mounting_target(session: Session) -> None:
    with pytest.raises(ValidationError, match="mounting rule's target"):
        mrs.create_rule(
            session, domain=MatchDomain.MOUNTING, alias="x", canonical="banana"
        )


def test_update_rule_folds_a_mounting_target_to_its_enum_case(
    session: Session,
) -> None:
    rule = mrs.create_rule(
        session, domain=MatchDomain.MOUNTING, alias="reflow", canonical="SMT"
    )
    updated = mrs.update_rule(session, rule.id, canonical="other")
    assert updated.canonical == "Other"


def test_normalize_folds_polish_accents() -> None:
    # An accent must fold to its base letter, not vanish — otherwise "wstążkowy" and
    # the un-accented "wstazkowy" a shop often writes would key differently.
    assert mrs.normalize("wstążkowy") == mrs.normalize("wstazkowy") == "wstazkowy"
    assert mrs.normalize("Pojemność") == "pojemnosc"
    assert mrs.normalize("dławik") == "dlawik"  # ł has no NFKD form; folded by hand
    assert mrs.normalize("ŁÓDŹ") == "lodz"


def test_match_rules_survive_reset_db_keep_types() -> None:
    # The rules are taxonomy; --keep-types must not wipe them.
    from scripts import reset_db

    assert "match_rules" in reset_db._KEEP_TABLES
