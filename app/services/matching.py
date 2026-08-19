"""The enrichment matching engine — one place, both import paths.

Given a :class:`~app.services.shops.base.ProductData` (from a shop API lookup, or the
fallback built from an invoice line's own text), this works out the ShelfOS fields we
can fill in for the user to review: the component **type**, its **mounting**, the
**package**, and as many **parameter values** as it can pull from the shop's structured
attributes and from free-text descriptions.

What it recognises is not hardcoded — it consults the editable rules in
``match_rule_service`` (type/mounting/package/parameter-name/enum-value synonyms), so
the vocabulary grows without touching this file. The logic here used to live in the
browser (``component_dialog.js``); it now runs on the server so the URL-dialog and
invoice imports share exactly one implementation.

The engine only *proposes* values and gates them (a value it emits is guaranteed to be
accepted by ``create_component_with_values``); the user still reviews before anything is
created.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlmodel import Session

from app.models.component import ParameterDefinition
from app.models.enums import MountingType, ParameterDataType
from app.services import component_service as cs
from app.services.component_service import ParameterValue
from app.services.match_rule_service import RuleSet, load_rules, normalize
from app.services.shops.base import ProductData
from app.units import UnitParseError, parse_engineering


@dataclass
class MatchProposal:
    """What the engine could work out — all of it for the user to review, not commit."""

    type_id: int | None = None
    mounting_type: MountingType | None = None
    package: str | None = None
    # (parameter_definition_id, value) pairs, each already gated so the create/finalize
    # path accepts them.
    parameters: list[tuple[int, ParameterValue]] = field(default_factory=list)
    # (source label, value) pairs we saw but could not place — surfaced for diagnostics.
    unmatched: list[tuple[str, str]] = field(default_factory=list)


# --- ported engineering-value helpers (were in component_dialog.js) ----------

# SI prefixes we keep on a cleaned number. µ folds to u and K to k; m stays milli
# and M stays mega.
_MULTIPLIERS = {"p", "n", "u", "k", "M", "G", "m"}


def _fold_prefix(char: str) -> str:
    return {"µ": "u", "K": "k"}.get(char, char)


def clean_number_value(raw: object) -> str:
    """"10 kOhms" -> "10k": keep the number and a valid SI prefix, drop the unit."""
    text = str(raw if raw is not None else "").strip()
    match = re.match(r"^[±\s]*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-zµΩ]*)", text)
    if not match:
        return text
    tail = match.group(2)
    first = _fold_prefix(tail[0]) if tail else ""
    return match.group(1) + (first if first in _MULTIPLIERS else "")


# A shop often leaves the real specs in the free-text description ("Thick Film
# Resistors - SMD 1.2 kOhms 50 V 100 mW 1 % 0402"). Rather than a parser per category,
# scan the description with the TYPE'S OWN parameter units: a resistor's Ω/W/% params
# pick up their values and the stray "50 V" is ignored (no volt parameter to hold it).
_UNIT_PATTERNS = {
    "ohm": r"(?:[Oo]hms?|Ω)",
    "ω": r"(?:[Oo]hms?|Ω)",
    "%": r"%",
    "w": r"W",
    "v": r"V",
    "f": r"F",
    "a": r"A",
    "h": r"H",
    "hz": r"Hz",
}
# A number, possibly a fraction ("1/16W" is 1/16 W, not 16 W).
_NUMBER = r"\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?"


def find_value_for_unit(text: str, unit: str | None) -> str | None:
    """The value carrying ``unit`` in ``text`` ("… 1.2 kOhms …", unit Ω -> "1.2k")."""
    pattern = _UNIT_PATTERNS.get(str(unit or "").strip().lower())
    if not pattern:
        return None
    # The multiplier stays case-sensitive (m milli vs M mega); the unit is tolerant.
    match = re.search(rf"({_NUMBER})\s*([pnµukKMGm])?\s*{pattern}(?![A-Za-z])", text)
    if not match:
        return None
    number = match.group(1)
    if "/" in number:
        top, _, bottom = number.partition("/")
        try:
            denominator = float(bottom)
        except ValueError:
            return None
        if not denominator:
            return None
        number = str(float(top) / denominator)  # 1/16 W -> 0.0625
    mult = _fold_prefix(match.group(2)) if match.group(2) else ""
    return number + (mult if mult in _MULTIPLIERS else "")


# A bare engineering token ("… TNPW-0402 1.2K 0.1% …" -> "1.2k"): a multiplier is
# required so "0402" and "100 PPM" can't be mistaken for a value.
_BARE_ENGINEERING = re.compile(
    r"(?:^|[\s\-])(\d+(?:\.\d+)?)([pnµukKMG])(?![A-Za-z0-9])"
)

_EIA_PACKAGE = re.compile(r"\b(0201|0402|0603|0805|1206|1210|1812|2010|2512)\b")

_BOOL_TRUE = {"true", "yes", "1", "tak", "y", "t"}
_BOOL_FALSE = {"false", "no", "0", "nie", "n", "f"}

# The most tokens a free-text enum alias may span ("taśma płaska" is 2, "surface
# mount" 2). An alias is stored normalized to a single spaceless blob, so to find it
# in a description we compare it against joins of up to this many adjacent words.
_MAX_ALIAS_WORDS = 4


# --- the engine --------------------------------------------------------------


def build_proposal(
    session: Session,
    product: ProductData,
    *,
    extra_text: str = "",
    type_id: int | None = None,
    rules: RuleSet | None = None,
) -> MatchProposal:
    """Work out type, mounting, package and parameter values from ``product``.

    ``type_id`` forces a type (the dialog re-runs this when the user picks one);
    otherwise it is inferred from the text via the rules. ``extra_text`` is anything
    beyond the description worth scanning (an invoice line's own notes). Pass a
    pre-loaded ``rules`` when calling in a loop.
    """
    if rules is None:
        rules = load_rules(session)

    description = product.description or ""
    hints = product.shop_category or ""
    # The type/mounting/package scans look at everything; the unit/value scans look
    # only at the real description + extra text (a category's stray digits must not
    # land in a number field as a bogus measurement).
    scan_text = " ".join(t for t in (description, extra_text) if t)
    blob = " ".join(t for t in (product.category, hints, scan_text) if t)

    proposal = MatchProposal()

    # 1. Type.
    if type_id is not None:
        proposal.type_id = type_id
    else:
        proposal.type_id = _resolve_type(session, product.category, blob, rules)

    # 2 + 3. Parameters (only once a type gives us its definitions).
    if proposal.type_id is not None:
        definitions = cs.get_effective_parameter_definitions(session, proposal.type_id)
        _fill_parameters(session, product, scan_text, definitions, rules, proposal)

    # 4. Mounting. A shop often states it only as a structured attribute (TME's
    # "Mounting" parameter) that never reaches the free-text blob, so scan the
    # attribute values too — but as a FALLBACK, not an equal contributor. The
    # description is the higher-confidence source, and _resolve_mounting returns the
    # first matching RULE (not the first match in the text), so merging the two into
    # one blob would let an incidental "SMD" in some attribute outrank an explicit
    # "through-hole" in the description. Resolve the text first, params only if it
    # names no mounting.
    param_text = " ".join(str(value) for _, value in product.parameters if value)
    proposal.mounting_type = _resolve_mounting(blob, rules) or _resolve_mounting(
        param_text, rules
    )

    # 5. Package.
    proposal.package = _resolve_package(product.package, blob, rules)

    return proposal


def _resolve_type(
    session: Session, category: str | None, blob: str, rules: RuleSet
) -> int | None:
    """Resolve a component type: an exact category name first, then TYPE aliases."""
    names = {ctype.name.casefold(): ctype.id for ctype in cs.list_types(session)}
    # A category that already IS a type name resolves directly — so seeded rules only
    # need to add synonyms, not an identity rule for every type.
    if category:
        exact = names.get(category.strip().casefold())
        if exact is not None:
            return exact
    lowered = blob.lower()
    for alias, canonical in rules.types:
        if not alias:
            continue
        # Left-anchored substring: the alias must start at a word boundary, but the
        # right side stays open so Polish inflection still works ("rezystor" catches
        # "Rezystory", "złącz" catches "złączami"). The left anchor stops mid-word
        # false hits — "ic:" no longer fires inside "electronic:", nor "led" inside
        # "coupled".
        if re.search(rf"(?<![a-z0-9]){re.escape(alias)}", lowered):
            type_id = names.get(canonical.casefold())
            if type_id is not None:
                return type_id
    return None


def _resolve_mounting(blob: str, rules: RuleSet) -> MountingType | None:
    """First MOUNTING alias present as a whole word -> its MountingType."""
    lowered = blob.lower()
    for alias, canonical in rules.mountings:
        if not alias:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", lowered):
            try:
                return MountingType(canonical)
            except ValueError:
                continue  # a rule pointing at a non-existent mounting value; skip
    return None


def _resolve_package(given: str | None, blob: str, rules: RuleSet) -> str | None:
    """The package to store: the shop's own field, a PACKAGE alias, or an EIA size.

    A shop that names the package outright wins over anything read out of the text —
    but its wording still goes through the rules first, which is half of what a
    PACKAGE rule is for: "SOT-23-3" and "SOT23" both land as the one "SOT-23" the
    shelf already uses. With no rule for it, the shop's text is kept as it came. That
    match is against the WHOLE field, not a word inside it: a shop's package field is
    the value itself, so a partial hit ("SOT23" inside "SOT23 (3-pin)") is a reason to
    leave a stated value alone rather than to rewrite it.

    Otherwise the description is scanned for an alias, whole-word the same way
    mounting is — and unlike the type scan, the RIGHT boundary stays. A type alias
    drops it to follow Polish inflection ("rezystor" catching "Rezystory"); a package
    name is a token, not a word that inflects, and what sits past its end is usually a
    different case ("TO-220" against "TO-220AB", "SOT-23" against "SOT-235"). So an
    alias fires only on the exact name, and a family is folded by SAYING so — a
    "TO-220AB -> TO-220" rule of its own — rather than by a regex deciding for the
    admin. An unfilled package the user completes beats a confidently wrong one
    prefilled into the dialog.

    Ordering still decides where aliases genuinely overlap, which a separator makes
    common enough: in "SOT-23-3" both "SOT-23" and "SOT-23-3" match, and the lower
    sort_order wins.

    Only if nothing matches does the built-in EIA size pattern have its say — those
    two-to-four digit chip codes are a numeric pattern rather than vocabulary, so they
    stay here instead of needing a rule per size.
    """
    if given and given.strip():
        text = given.strip()
        lowered = text.lower()
        for alias, canonical in rules.packages:
            if alias and alias == lowered:
                return canonical
        return text
    lowered = blob.lower()
    for alias, canonical in rules.packages:
        if not alias:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", lowered):
            return canonical
    eia = _EIA_PACKAGE.search(blob)
    return eia.group(1) if eia else None


def _fill_parameters(
    session: Session,
    product: ProductData,
    scan_text: str,
    definitions: list[ParameterDefinition],
    rules: RuleSet,
    proposal: MatchProposal,
) -> None:
    """Fill each definition from structured shop params first, then loose text."""
    allowed_enums = cs.enum_values_by_definition(
        session, [d.id for d in definitions if d.id is not None]
    )
    # A shop label / alias -> the definition it names.
    by_name: dict[str, ParameterDefinition] = {}
    for definition in definitions:
        by_name[normalize(definition.name)] = definition
        by_name[normalize(definition.label)] = definition
        for alias in rules.param_aliases.get(definition.id or -1, set()):
            by_name[alias] = definition

    filled: dict[int, ParameterValue] = {}

    # Structured shop attributes, then key:value fragments dug out of the description
    # ("… ; PIN: 40 ; THT ; 3A" -> ("PIN", "40")). Structured wins on a tie.
    named = list(product.parameters) + _key_value_fragments(scan_text)
    for label, value in named:
        target = by_name.get(normalize(label))
        if target is None or target.id in filled:
            continue
        gated = _gate_value(target, value, allowed_enums, rules.enum_aliases)
        if gated is None:
            proposal.unmatched.append((label, str(value)))
        else:
            filled[target.id] = gated  # type: ignore[index]

    # Loose text: each NUMBER def with a unit picks up its value; the primary value
    # parameter also takes a bare engineering token as a last resort; and an ENUM def
    # takes a value word that an ENUM_VALUE rule recognises ("wstążkowy" -> Flat).
    if scan_text:
        for definition in definitions:
            if (
                definition.data_type is not ParameterDataType.NUMBER
                or not definition.unit
                or definition.id in filled
            ):
                continue
            found = find_value_for_unit(scan_text, definition.unit)
            if found is not None:
                filled[definition.id] = found  # type: ignore[index]
        _fill_value_parameter(scan_text, definitions, filled)
        _fill_enums_from_text(
            scan_text, definitions, filled, rules.enum_aliases, allowed_enums
        )

    proposal.parameters.extend(filled.items())


def _fill_enums_from_text(
    scan_text: str,
    definitions: list[ParameterDefinition],
    filled: dict[int, ParameterValue],
    enum_aliases: dict[int, dict[str, str]],
    allowed_enums: dict[int, list[str]],
) -> None:
    """Fill an unset ENUM def when a word in the text matches one of its aliases.

    Structured shop attributes and "label: value" fragments are handled earlier; this
    is the free-text case an invoice usually is — a bare "wstążkowy" in the middle of
    a description. Only an admin-added ENUM_VALUE **alias** counts here (not the raw
    allowed tokens), so an incidental word can't be mistaken for a value; the first
    alias present wins (rules are ordered by sort_order). Matching is accent- and
    case-insensitive via ``normalize`` (so Polish spelling variants unify).

    A stored alias is a single spaceless blob (``normalize`` drops spaces), so a
    multi-word alias like "taśma płaska" (or hyphenated "flat-flex") is matched against
    joins of up to ``_MAX_ALIAS_WORDS`` adjacent description words — otherwise it would
    fire on structured data but be invisible here.
    """
    tokens = [normalize(w) for w in re.findall(r"\w+", scan_text.lower())]
    tokens = [t for t in tokens if t]
    # Every join of 1..N adjacent tokens; a normalized alias equals one of these iff
    # its words appear consecutively in the text.
    windows: set[str] = set()
    for size in range(1, _MAX_ALIAS_WORDS + 1):
        for start in range(len(tokens) - size + 1):
            windows.add("".join(tokens[start : start + size]))
    for definition in definitions:
        did = definition.id
        if (
            definition.data_type is not ParameterDataType.ENUM
            or did is None
            or did in filled
        ):
            continue
        allowed = allowed_enums.get(did, [])
        for alias, canonical in enum_aliases.get(did, {}).items():
            if alias in windows and canonical in allowed:
                filled[did] = canonical
                break


def _fill_value_parameter(
    scan_text: str,
    definitions: list[ParameterDefinition],
    filled: dict[int, ParameterValue],
) -> None:
    """Last resort for a unitless primary value ("… 1.2K …" on a resistor)."""
    number_defs = [
        d
        for d in definitions
        if d.data_type is ParameterDataType.NUMBER and d.id is not None
    ]
    if not number_defs:
        return
    value_def = min(number_defs, key=lambda d: (d.sort_order, d.id or 0))
    if value_def.id in filled:
        return
    match = _BARE_ENGINEERING.search(scan_text)
    if match:
        filled[value_def.id] = match.group(1) + _fold_prefix(match.group(2))  # type: ignore[index]


def _gate_value(
    definition: ParameterDefinition,
    value: object,
    allowed_enums: dict[int, list[str]],
    enum_aliases: dict[int, dict[str, str]],
) -> ParameterValue | None:
    """Turn a raw shop value into one the create path accepts, or None if it can't."""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    match definition.data_type:
        case ParameterDataType.NUMBER:
            cleaned = clean_number_value(text)
            try:
                parse_engineering(cleaned)
            except UnitParseError:
                return None
            return cleaned
        case ParameterDataType.TEXT:
            return text
        case ParameterDataType.BOOL:
            folded = normalize(text)
            if folded in _BOOL_TRUE:
                return True
            if folded in _BOOL_FALSE:
                return False
            return None
        case ParameterDataType.ENUM:
            return _match_enum(
                text,
                allowed_enums.get(definition.id or -1, []),
                enum_aliases.get(definition.id or -1, {}),
            )
    return None


def _match_enum(
    text: str, allowed: list[str], aliases: dict[str, str]
) -> str | None:
    """Resolve a shop value to one of the definition's allowed enum tokens, or None.

    An ENUM_VALUE rule may map a synonym ("1206 Metric") to the canonical token
    ("1206"); otherwise the value must already equal an allowed token (ignoring case
    and punctuation). A candidate that doesn't land on an allowed token is dropped —
    never emitted — because the create path rejects a non-member and would abort the
    whole component.
    """
    key = normalize(text)
    canonical = aliases.get(key)
    if canonical is not None:
        return canonical if canonical in allowed else None
    for option in allowed:
        if normalize(option) == key:
            return option
    return None


def _key_value_fragments(text: str) -> list[tuple[str, str]]:
    """Split "Connector: pin strips; PIN: 40; THT; 3A" into its "label: value" bits."""
    fragments: list[tuple[str, str]] = []
    for part in text.split(";"):
        label, sep, value = part.partition(":")
        if sep and label.strip() and value.strip():
            fragments.append((label.strip(), value.strip()))
    return fragments
