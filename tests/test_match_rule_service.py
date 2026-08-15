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
    with pytest.raises(ValidationError, match="already exists"):
        # Same domain + same alias (case-insensitively) is a duplicate.
        mrs.create_rule(
            session, domain=MatchDomain.MOUNTING, alias="smd", canonical="SMT"
        )


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


def test_match_rules_survive_reset_db_keep_types() -> None:
    # The rules are taxonomy; --keep-types must not wipe them.
    from scripts import reset_db

    assert "match_rules" in reset_db._KEEP_TABLES
