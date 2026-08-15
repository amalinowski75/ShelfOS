"""Store and load the enrichment engine's matching rules.

A rule is a small row (see :class:`app.models.match_rule.MatchRule`) saying "when you
see this alias, treat it as this canonical value". This module is the only place that
reads and writes them: the seed and the (future) admin UI create/edit them here, and
the engine (``app/services/matching.py``) loads them all at once via :func:`load_rules`.

Rules are loaded fresh on each :func:`load_rules` call (the table is small); callers
that run in a loop — an invoice import, one dialog request — call it once and reuse the
resulting :class:`RuleSet`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.models.component import ParameterDefinition
from app.models.enums import MatchDomain
from app.models.match_rule import MatchRule
from app.services._common import require_entity
from app.services.errors import ValidationError

# Fold a shop's parameter label / value to a comparable key: lowercase, drop every
# non-alphanumeric character. So "Rezystancja", "Resistance (Ω)" and "resistance"
# only match a rule/definition whose own name folds the same way. (Ported verbatim
# from the old client-side normalizeName in component_dialog.js.)
_NON_ALNUM = re.compile(r"[^a-z0-9]")


def normalize(name: str | None) -> str:
    return _NON_ALNUM.sub("", str(name or "").lower())


@dataclass
class RuleSet:
    """All matching rules, grouped and normalized for the engine to consult.

    ``types`` and ``mountings`` are ordered lowest-``sort_order``-first so the more
    specific alias wins (led before diode). ``param_aliases`` and ``enum_aliases`` are
    keyed by the parameter definition the rule is scoped to.
    """

    # (alias_lower, canonical) pairs, most-specific first:
    types: list[tuple[str, str]] = field(default_factory=list)
    mountings: list[tuple[str, str]] = field(default_factory=list)
    # definition id -> set of normalized aliases that mean this definition
    param_aliases: dict[int, set[str]] = field(default_factory=dict)
    # definition id -> {normalized alias -> canonical allowed enum value}
    enum_aliases: dict[int, dict[str, str]] = field(default_factory=dict)


def load_rules(session: Session) -> RuleSet:
    """Read every rule and group it into a :class:`RuleSet`."""
    rules = session.exec(
        select(MatchRule).order_by(
            MatchRule.sort_order,  # type: ignore[arg-type]
            MatchRule.id,  # type: ignore[arg-type]
        )
    ).all()
    result = RuleSet()
    for rule in rules:
        if rule.domain is MatchDomain.TYPE:
            result.types.append((rule.alias.lower(), rule.canonical))
        elif rule.domain is MatchDomain.MOUNTING:
            result.mountings.append((rule.alias.lower(), rule.canonical))
        elif rule.parameter_definition_id is None:
            continue  # a scoped rule with no definition is unusable; skip defensively
        elif rule.domain is MatchDomain.PARAM_NAME:
            result.param_aliases.setdefault(rule.parameter_definition_id, set()).add(
                normalize(rule.alias)
            )
        elif rule.domain is MatchDomain.ENUM_VALUE:
            result.enum_aliases.setdefault(rule.parameter_definition_id, {})[
                normalize(rule.alias)
            ] = rule.canonical
    return result


# --- CRUD (used by the seed now, and the admin UI later) ---------------------


def list_rules(
    session: Session, *, domain: MatchDomain | None = None
) -> list[MatchRule]:
    statement = select(MatchRule)
    if domain is not None:
        statement = statement.where(MatchRule.domain == domain)
    return list(
        session.exec(
            statement.order_by(
                MatchRule.domain,
                MatchRule.sort_order,  # type: ignore[arg-type]
                MatchRule.id,  # type: ignore[arg-type]
            )
        ).all()
    )


def create_rule(
    session: Session,
    *,
    domain: MatchDomain,
    alias: str,
    canonical: str,
    parameter_definition_id: int | None = None,
    sort_order: int = 0,
) -> MatchRule:
    """Add a rule, rejecting blanks and exact duplicates.

    The scoped domains (param_name, enum_value) require a definition; the global ones
    (type, mounting) must not carry one.
    """
    alias = alias.strip()
    canonical = canonical.strip()
    if not alias or not canonical:
        raise ValidationError("a matching rule needs both an alias and a target")
    scoped = domain in (MatchDomain.PARAM_NAME, MatchDomain.ENUM_VALUE)
    if scoped and parameter_definition_id is None:
        raise ValidationError(f"{domain.value} rules must name a parameter definition")
    if not scoped and parameter_definition_id is not None:
        raise ValidationError(f"{domain.value} rules are global, not per-parameter")
    if parameter_definition_id is not None:
        # SQLite FKs aren't enforced, so guard here — an orphaned scoped rule would
        # be silently unusable (the engine only loads aliases for live definitions).
        require_entity(
            session,
            ParameterDefinition,
            parameter_definition_id,
            "parameter definition",
        )
    if _find_duplicate(session, domain, alias, parameter_definition_id) is not None:
        raise ValidationError("an identical rule already exists")
    rule = MatchRule(
        domain=domain,
        alias=alias,
        canonical=canonical,
        parameter_definition_id=parameter_definition_id,
        sort_order=sort_order,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def delete_rule(session: Session, rule_id: int) -> None:
    rule = session.get(MatchRule, rule_id)
    if rule is None:
        raise ValidationError("matching rule not found")
    session.delete(rule)
    session.commit()


def _find_duplicate(
    session: Session,
    domain: MatchDomain,
    alias: str,
    parameter_definition_id: int | None,
) -> MatchRule | None:
    # Same domain + same scope + same alias (case-insensitively) is a duplicate.
    for rule in session.exec(
        select(MatchRule).where(
            MatchRule.domain == domain,
            MatchRule.parameter_definition_id == parameter_definition_id,
        )
    ).all():
        if rule.alias.lower() == alias.lower():
            return rule
    return None


def rule_exists(
    session: Session,
    *,
    domain: MatchDomain,
    alias: str,
    parameter_definition_id: int | None = None,
) -> bool:
    """Whether an identical rule is already stored (lets the seed stay idempotent)."""
    return (
        _find_duplicate(session, domain, alias.strip(), parameter_definition_id)
        is not None
    )


# --- default rules shipped with every install -------------------------------

# Mounting words -> MountingType value, English + Polish. Whole-word matched, so
# "SMD" won't fire inside a longer token.
_DEFAULT_MOUNTING: list[tuple[str, str]] = [
    ("SMD", "SMT"),
    ("SMT", "SMT"),
    ("surface mount", "SMT"),
    ("surface-mount", "SMT"),
    ("powierzchniowy", "SMT"),
    ("THT", "THT"),
    ("through hole", "THT"),
    ("through-hole", "THT"),
    ("przewlekany", "THT"),
    ("panel", "Panel"),
    ("przewodowy", "Wire"),
]


def seed_default_rules(session: Session) -> int:
    """Install the universal TYPE and MOUNTING rules if missing (idempotent).

    TYPE rules migrate the old hardcoded category keywords so the same vocabulary is
    now editable; MOUNTING rules give SMT/THT inference (incl. Polish). The
    per-parameter domains are not seeded here — they reference specific definitions,
    so they are added per install (demo data, or the admin UI).

    Returns how many new rules were created.
    """
    # Imported here to avoid a module-level dependency from the service on the shop
    # providers; the keyword list is the single source these rules are migrated from.
    from app.services.shops.base import _CATEGORY_KEYWORDS

    created = 0
    for order, (alias, canonical) in enumerate(_CATEGORY_KEYWORDS):
        if not rule_exists(session, domain=MatchDomain.TYPE, alias=alias):
            create_rule(
                session,
                domain=MatchDomain.TYPE,
                alias=alias,
                canonical=canonical,
                sort_order=order,
            )
            created += 1
    for alias, canonical in _DEFAULT_MOUNTING:
        if not rule_exists(session, domain=MatchDomain.MOUNTING, alias=alias):
            create_rule(
                session,
                domain=MatchDomain.MOUNTING,
                alias=alias,
                canonical=canonical,
            )
            created += 1
    return created
