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
import unicodedata
from dataclasses import dataclass, field
from typing import cast

from sqlmodel import Session, select

from app.models.component import ParameterDefinition
from app.models.enums import MatchDomain, MountingType
from app.models.match_rule import MatchRule
from app.services import audit_service
from app.services._common import require_entity
from app.services.component_service import enum_values_of
from app.services.errors import ValidationError

# Fold a shop's parameter label / value to a comparable key: lowercase, strip accents
# to their base letter, then drop every non-alphanumeric character. So "Rezystancja",
# "Resistance (Ω)" and "resistance" fold the same — and, crucially for Polish, so do
# "wstążkowy" and "wstazkowy" (an accent must not simply vanish and change the word).
_NON_ALNUM = re.compile(r"[^a-z0-9]")
# Letters NFKD does not decompose (they have no combining form), folded by hand.
_STANDALONE_FOLD = str.maketrans({"ł": "l", "đ": "d", "ø": "o", "ß": "ss", "þ": "th"})


def normalize(name: str | None) -> str:
    text = str(name or "").lower().translate(_STANDALONE_FOLD)
    # NFKD splits e.g. "ż" into "z" + a combining mark; dropping the marks leaves "z".
    text = "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )
    return _NON_ALNUM.sub("", text)


# What the audit log calls a matching rule (spec §19).
_AUDIT_ENTITY = "match_rule"


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


def _canonical_target(
    session: Session,
    domain: MatchDomain,
    canonical: str,
    parameter_definition_id: int | None,
) -> str:
    """Validate/normalise a rule's target for the domains that have a fixed vocabulary.

    A MOUNTING target must name a :class:`MountingType`, and an ENUM_VALUE target must
    name one of its parameter's allowed enum tokens. Both are folded to the exact
    stored spelling ("tht" -> "THT"), because the engine matches the target
    case-sensitively (``canonical in allowed``) and a wrong case would make the rule
    silently do nothing — the very "looks alive in the table but can never fire"
    failure this guards against. A TYPE target is resolved case-insensitively by the
    engine, and a PARAM_NAME target is a free-text definition name, so both are kept
    verbatim.
    """
    if domain is MatchDomain.MOUNTING:
        for member in MountingType:
            if member.value.casefold() == canonical.casefold():
                return member.value
        allowed = ", ".join(m.value for m in MountingType)
        raise ValidationError(f"a mounting rule's target must be one of: {allowed}")
    if domain is MatchDomain.ENUM_VALUE and parameter_definition_id is not None:
        values = enum_values_of(session, parameter_definition_id)
        for value in values:
            if value.casefold() == canonical.casefold():
                return value
        allowed = ", ".join(values) or "(the parameter has no enum values)"
        raise ValidationError(
            f"an enum_value rule's target must be one of the parameter's "
            f"values: {allowed}"
        )
    return canonical


def _describe(rule: MatchRule) -> str:
    """A rule in one line, for a log entry that has to stand on its own.

    An audit row names an entity by id, which is no use once the rule is gone —
    and "who deleted the rule that mapped Rezystancja to resistance" is exactly
    the question someone will bring to the log.
    """
    return f"{rule.domain.value}: {rule.alias} → {rule.canonical}"


def create_rule(
    session: Session,
    *,
    domain: MatchDomain,
    alias: str,
    canonical: str,
    parameter_definition_id: int | None = None,
    sort_order: int = 0,
    user_id: int | None = None,
) -> MatchRule:
    """Add a rule, rejecting blanks and exact duplicates.

    The scoped domains (param_name, enum_value) require a definition; the global ones
    (type, mounting) must not carry one.

    Audited when a ``user_id`` is given (§19): a rule changes how every later
    import is read, and the rule row keeps no history of its own. ``None`` is
    for :func:`seed_default_rules`, which runs at startup with nobody to blame.
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
    # Validate the target after the FK check — an enum_value target is checked against
    # the parameter's own allowed values, which needs the definition to exist.
    canonical = _canonical_target(session, domain, canonical, parameter_definition_id)
    existing = _find_duplicate(session, domain, alias, parameter_definition_id)
    if existing is not None:
        raise ValidationError(
            f"the alias '{existing.alias}' already exists in this domain "
            f"(it maps to '{existing.canonical}')"
        )
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
    if user_id is not None:
        audit_service.record_change(
            session,
            entity_type=_AUDIT_ENTITY,
            entity_id=cast(int, rule.id),
            field=audit_service.FIELD_CREATED,
            old_value=None,
            new_value=_describe(rule),
            user_id=user_id,
        )
        session.commit()
    return rule


def update_rule(
    session: Session,
    rule_id: int,
    *,
    alias: str | None = None,
    canonical: str | None = None,
    sort_order: int | None = None,
    user_id: int,
) -> MatchRule:
    """Edit a rule's alias, target or order in place (only the fields given change).

    The rule's domain and scope are fixed — changing them would make it a different
    rule (delete and re-add instead). The same duplicate guard as :func:`create_rule`
    applies to a rename: an alias already used in this domain + scope is rejected, so
    the same text never maps two different ways.
    """
    rule = session.get(MatchRule, rule_id)
    if rule is None:
        raise ValidationError("matching rule not found")
    before = (rule.alias, rule.canonical, rule.sort_order)
    if alias is not None:
        alias = alias.strip()
        if not alias:
            raise ValidationError("a matching rule needs an alias")
        clash = _find_duplicate(
            session, rule.domain, alias, rule.parameter_definition_id
        )
        if clash is not None and clash.id != rule.id:
            raise ValidationError(
                f"the alias '{clash.alias}' already exists in this domain "
                f"(it maps to '{clash.canonical}')"
            )
        rule.alias = alias
    if canonical is not None:
        canonical = canonical.strip()
        if not canonical:
            raise ValidationError("a matching rule needs a target")
        rule.canonical = _canonical_target(
            session, rule.domain, canonical, rule.parameter_definition_id
        )
    if sort_order is not None:
        rule.sort_order = sort_order
    for name, old, new in zip(
        (
            audit_service.FIELD_ALIAS,
            audit_service.FIELD_CANONICAL,
            audit_service.FIELD_SORT_ORDER,
        ),
        before,
        (rule.alias, rule.canonical, rule.sort_order),
        strict=True,
    ):
        if old != new:
            audit_service.record_change(
                session,
                entity_type=_AUDIT_ENTITY,
                entity_id=rule_id,
                field=name,
                old_value=old,
                new_value=new,
                user_id=user_id,
            )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def delete_rule(session: Session, rule_id: int, *, user_id: int) -> None:
    """Remove a rule, recording what it was (§19).

    The entry carries the rule's text rather than only "deleted", because by the
    time anyone reads it the row is gone and its id says nothing.
    """
    rule = session.get(MatchRule, rule_id)
    if rule is None:
        raise ValidationError("matching rule not found")
    audit_service.record_change(
        session,
        entity_type=_AUDIT_ENTITY,
        entity_id=rule_id,
        field=audit_service.FIELD_DELETED,
        old_value=_describe(rule),
        new_value=None,
        user_id=user_id,
    )
    session.delete(rule)
    session.commit()


def _find_duplicate(
    session: Session,
    domain: MatchDomain,
    alias: str,
    parameter_definition_id: int | None,
) -> MatchRule | None:
    # Two aliases collide when the ENGINE would key them the same way — otherwise the
    # loser sits in the admin table looking healthy but never fires. type/mounting are
    # keyed by .lower(); the scoped domains by normalize() (which folds accents and
    # punctuation), so there "wstążkowy" and "wstazkowy" are one alias, not two.
    scoped = domain in (MatchDomain.PARAM_NAME, MatchDomain.ENUM_VALUE)
    fold = normalize if scoped else str.lower
    target = fold(alias)
    for rule in session.exec(
        select(MatchRule).where(
            MatchRule.domain == domain,
            MatchRule.parameter_definition_id == parameter_definition_id,
        )
    ).all():
        if fold(rule.alias) == target:
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
