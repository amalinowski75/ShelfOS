"""Orchestrator tests: ParsedInvoice → draft invoice, matching and staging.

Parsing is covered separately (``test_invoice_import_parsers``); here ``parse_invoice``
is monkeypatched to return a crafted :class:`ParsedInvoice`, so the tests exercise the
match/enrich/stage decisions without depending on a PDF. Shop enrichment is faked via
``shops._BY_INDEX``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from app import config
from app.services import component_service as cs
from app.services import invoice_import_service as iis
from app.services import invoice_service, shops
from app.services.errors import NotFoundError, ValidationError
from app.services.invoice_import import ParsedInvoice, ParsedLine
from app.services.shops.base import ProductData
from sqlmodel import Session


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """Point the attachment store (the saved PDF) at a throwaway directory."""
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path)
    return tmp_path


def _invoice(*lines: ParsedLine, shop_key: str = "digikey", number: str = "INV-1"):
    return ParsedInvoice(
        supplier="Digi-Key",
        invoice_number=number,
        invoice_date=date(2026, 1, 1),
        currency="PLN",
        shop_key=shop_key,
        lines=list(lines),
    )


def _patch_parse(monkeypatch, invoice: ParsedInvoice) -> None:
    monkeypatch.setattr(iis, "parse_invoice", lambda data, filename: invoice)


def _fake_provider(monkeypatch, shop_key: str, product: ProductData) -> None:
    class _Fake:
        def fetch_by_index(self, candidates):  # type: ignore[no-untyped-def]
            return product

    monkeypatch.setitem(shops._BY_INDEX, shop_key, _Fake())


def _line(**kw) -> ParsedLine:  # type: ignore[no-untyped-def]
    kw.setdefault("quantity", 10)
    kw.setdefault("unit_price", Decimal("1.500000"))
    return ParsedLine(**kw)


# --- case 1: component already exists ----------------------------------------


def test_existing_component_is_matched_and_added(session: Session, monkeypatch) -> None:
    ctype = cs.create_type(session, "diode")
    cs.create_component(session, ctype.id, manufacturer="Onsemi", mpn="1N4148")
    _patch_parse(
        monkeypatch,
        _invoice(_line(mpn="1N4148", manufacturer="Onsemi", quantity=50)),
    )

    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)

    assert result.added == 1 and result.pending == 0
    _, lines = invoice_service.get_invoice_detail(session, result.invoice_id)
    assert len(lines) == 1
    assert lines[0][0].quantity == 50
    # No new component was created — the existing one was reused.
    assert len(cs.list_components(session)) == 1


# --- case 2: type exists, component created + enriched ------------------------


def test_missing_component_with_known_type_is_staged_ready_not_created(
    session: Session, monkeypatch
) -> None:
    ctype = cs.create_type(session, "mosfet")
    _fake_provider(
        monkeypatch,
        "mouser",
        ProductData(
            mpn="NX3P1108UKZ",
            manufacturer="NXP Semiconductors",
            description="Load switch",
            package="XSON8",
            category="mosfet",
        ),
    )
    _patch_parse(
        monkeypatch,
        _invoice(_line(mpn="NX3P1108UKZ"), shop_key="mouser"),
    )

    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)

    # Deferred: NO component created at import — the line is staged "ready" with the
    # inferred type, carrying the enriched fields for materialisation at finalize.
    assert result.added == 0 and result.pending == 1
    assert cs.list_components(session) == []
    row = iis.list_pending(session, result.invoice_id)[0]
    assert row.type_id == ctype.id
    assert row.manufacturer == "NXP Semiconductors"
    assert row.mpn == "NX3P1108UKZ"
    assert row.package == "XSON8"
    assert row.reason == ""  # ready — no review reason


def test_enrichment_fills_parameters_and_mounting_on_the_staged_row(
    session: Session, monkeypatch
) -> None:
    from app.models.enums import MountingType
    from app.services import match_rule_service as mrs

    rtype = cs.create_type(session, "resistor")
    resistance = cs.add_parameter_definition(
        session, rtype.id, name="resistance", label="Resistance",
        data_type=cs.ParameterDataType.NUMBER, unit="Ω",
    )
    mrs.seed_default_rules(session)  # gives SMD -> SMT
    _fake_provider(
        monkeypatch,
        "mouser",
        ProductData(
            mpn="RC0402",
            manufacturer="YAGEO",
            category="resistor",
            shop_category="Chip Resistor - Surface Mount",
            description="Thick Film Resistors - SMD 10 kOhms 0402",
            parameters=[("Resistance", "10 kOhms")],
        ),
    )
    _patch_parse(monkeypatch, _invoice(_line(mpn="RC0402"), shop_key="mouser"))

    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)

    row = iis.list_pending(session, result.invoice_id)[0]
    assert row.type_id == rtype.id
    assert row.mounting_type is MountingType.SMT
    assert row.package == "0402"
    # The engine placed the resistance value onto the staged row — previously dropped.
    assert row.parameters == [
        {"parameter_definition_id": resistance.id, "value": "10k"}
    ]

    # Finalize materialises a component carrying those values + mounting.
    iis.update_pending(
        session, result.invoice_id, row.id, location_id=_make_location(session)
    )
    invoice_service.finalize_invoice(session, result.invoice_id, user_id=1)
    component = cs.list_components(session)[0]
    assert component.mounting_type is MountingType.SMT
    values = cs.list_parameter_values(session, component.id)
    assert values[0].parameter_definition_id == resistance.id
    assert values[0].value_num == 10000.0


def test_changing_the_type_clears_parameters_but_keeps_mounting(
    session: Session, monkeypatch
) -> None:
    from app.models.enums import MountingType
    from app.services import match_rule_service as mrs

    rtype = cs.create_type(session, "resistor")
    resistance = cs.add_parameter_definition(
        session, rtype.id, name="resistance", label="Resistance",
        data_type=cs.ParameterDataType.NUMBER, unit="Ω",
    )
    other = cs.create_type(session, "capacitor")
    mrs.seed_default_rules(session)
    _fake_provider(
        monkeypatch,
        "mouser",
        ProductData(
            mpn="RC0402", manufacturer="YAGEO", category="resistor",
            description="Thick Film Resistors - SMD 10 kOhms 0402",
            parameters=[("Resistance", "10 kOhms")],
        ),
    )
    _patch_parse(monkeypatch, _invoice(_line(mpn="RC0402"), shop_key="mouser"))
    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    row = iis.list_pending(session, result.invoice_id)[0]
    assert row.mounting_type is MountingType.SMT
    assert row.parameters == [
        {"parameter_definition_id": resistance.id, "value": "10k"}
    ]

    # Switching type drops the old type's parameters, but mounting is type-independent
    # and must survive — it was never re-derived on a type change.
    updated = iis.update_pending(session, result.invoice_id, row.id, type_id=other.id)
    assert updated.type_id == other.id
    assert updated.parameters == []
    assert updated.mounting_type is MountingType.SMT


def test_update_pending_ignores_a_null_mounting_type(
    session: Session, monkeypatch
) -> None:
    from app.models.enums import MountingType
    from app.services import match_rule_service as mrs

    cs.create_type(session, "resistor")
    mrs.seed_default_rules(session)
    _fake_provider(
        monkeypatch,
        "mouser",
        ProductData(mpn="RC0402", manufacturer="YAGEO", category="resistor",
                    description="SMD 0402"),
    )
    _patch_parse(monkeypatch, _invoice(_line(mpn="RC0402"), shop_key="mouser"))
    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    row = iis.list_pending(session, result.invoice_id)[0]
    assert row.mounting_type is MountingType.SMT

    # An explicit null (the model has no cleared state) must leave the value intact,
    # not write None into the NOT-NULL column and break template rendering.
    updated = iis.update_pending(
        session, result.invoice_id, row.id, mounting_type=None
    )
    assert updated.mounting_type is MountingType.SMT


# --- enrichment is keyed by the shop's own index -----------------------------


def test_enrich_offers_the_supplier_index_before_the_mpn(
    session: Session, monkeypatch
) -> None:
    seen: list[list[str]] = []

    class _Recorder:
        def fetch_by_index(self, candidates):  # type: ignore[no-untyped-def]
            seen.append(list(candidates))
            return ProductData(mpn="NX3P1108UKZ", manufacturer="NXP")

    monkeypatch.setitem(shops._BY_INDEX, "mouser", _Recorder())
    _patch_parse(
        monkeypatch,
        _invoice(
            _line(mpn="NX3P1108UKZ", supplier_part_number="771-NX3P1108UKZ"),
            shop_key="mouser",
        ),
    )

    iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)

    # The shop's own catalogue number leads; the parsed MPN is the fallback.
    assert seen == [["771-NX3P1108UKZ", "NX3P1108UKZ"]]


def test_enrich_deduplicates_identical_index_and_mpn(
    session: Session, monkeypatch
) -> None:
    # TME's symbol often IS the MPN — it must be offered once, not twice.
    seen: list[list[str]] = []

    class _Recorder:
        def fetch_by_index(self, candidates):  # type: ignore[no-untyped-def]
            seen.append(list(candidates))
            return ProductData(mpn="AO3400A")

    monkeypatch.setitem(shops._BY_INDEX, "tme", _Recorder())
    _patch_parse(
        monkeypatch,
        _invoice(
            _line(mpn="AO3400A", supplier_part_number="AO3400A", manufacturer="AOS"),
            shop_key="tme",
        ),
    )

    iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    assert seen == [["AO3400A"]]


def test_a_hard_enrich_failure_disables_the_api_for_the_rest_of_the_import(
    session: Session, monkeypatch
) -> None:
    # An unconfigured/unreachable shop fails identically for every line — after the
    # first hard failure the import must stop calling the API (each call can burn a
    # full timeout inside one synchronous request).
    calls: list[list[str]] = []

    class _Hard:
        def fetch_by_index(self, candidates):  # type: ignore[no-untyped-def]
            calls.append(list(candidates))
            raise ValidationError("could not reach Mouser")

    monkeypatch.setitem(shops._BY_INDEX, "mouser", _Hard())
    _patch_parse(
        monkeypatch,
        _invoice(
            _line(mpn="A1"), _line(mpn="B2"), _line(mpn="C3"), shop_key="mouser"
        ),
    )

    iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    assert len(calls) == 1  # lines 2 and 3 skipped the API entirely


def test_a_plain_miss_keeps_trying_the_api_for_later_lines(
    session: Session, monkeypatch
) -> None:
    from app.services.shops.base import ShopLookupMiss

    calls: list[list[str]] = []

    class _Miss:
        def fetch_by_index(self, candidates):  # type: ignore[no-untyped-def]
            calls.append(list(candidates))
            raise ShopLookupMiss("no product found")

    monkeypatch.setitem(shops._BY_INDEX, "mouser", _Miss())
    _patch_parse(
        monkeypatch,
        _invoice(_line(mpn="A1"), _line(mpn="B2"), shop_key="mouser"),
    )

    iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    assert len(calls) == 2  # a per-part miss doesn't condemn the next part


# --- case 3: no matching type → parked ---------------------------------------


def test_missing_component_without_a_type_is_staged(
    session: Session, monkeypatch
) -> None:
    # TME has no shop API, and its inferred category won't match any existing type.
    _patch_parse(
        monkeypatch,
        _invoice(_line(mpn="XYZ123", manufacturer="Acme"), shop_key="tme"),
    )

    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)

    assert result.added == 0 and result.pending == 1
    _, lines = invoice_service.get_invoice_detail(session, result.invoice_id)
    assert lines == []
    pending = iis.list_pending(session, result.invoice_id)
    assert len(pending) == 1
    assert pending[0].mpn == "XYZ123"
    assert pending[0].manufacturer == "Acme"
    assert "type" in pending[0].reason
    # A staged line must not have created a component.
    assert cs.list_components(session) == []


# --- TME (no shop API) resolves a type from its Polish description ------------


def test_tme_polish_description_resolves_an_existing_type(
    session: Session, monkeypatch
) -> None:
    ctype = cs.create_type(session, "resistor")
    # TME has no fetch_by_mpn; the category is inferred from the Polish description.
    _patch_parse(
        monkeypatch,
        _invoice(
            _line(
                mpn="0402WGF1004TCE",
                manufacturer="ROYALOHM",
                description="Rezystor:thick film;SMD;0402;1MΩ",
            ),
            shop_key="tme",
        ),
    )

    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)

    # Staged ready against the resistor type; still not created until finalize.
    assert result.pending == 1
    assert cs.list_components(session) == []
    assert iis.list_pending(session, result.invoice_id)[0].type_id == ctype.id


# --- finalize is blocked while lines are still parked -------------------------


def _import_one_ready(session, monkeypatch, *, mpn="R1", manufacturer="Acme"):
    """Import a single new part whose type exists → one ready staged row."""
    cs.create_type(session, "resistor")
    _patch_parse(
        monkeypatch,
        _invoice(
            _line(mpn=mpn, manufacturer=manufacturer, description="Rezystor:x"),
            shop_key="tme",
        ),
    )
    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    return result.invoice_id, iis.list_pending(session, result.invoice_id)[0]


def test_finalize_materializes_a_ready_row(session: Session, monkeypatch) -> None:
    invoice_id, row = _import_one_ready(session, monkeypatch)
    location = _make_location(session)
    iis.update_pending(session, invoice_id, row.id, location_id=location)
    assert cs.list_components(session) == []  # still nothing created

    invoice_service.finalize_invoice(session, invoice_id, user_id=1)

    invoice, lines = invoice_service.get_invoice_detail(session, invoice_id)
    assert invoice.is_finalized is True
    # The component is created at finalize, the line references it, staging is gone.
    created = cs.list_components(session)
    assert len(created) == 1 and created[0].mpn == "R1"
    assert len(lines) == 1
    assert iis.list_pending(session, invoice_id) == []


def test_finalize_blocked_by_a_needs_review_row(
    session: Session, monkeypatch
) -> None:
    # No type exists → the row needs review (no type_id).
    _patch_parse(
        monkeypatch,
        _invoice(_line(mpn="X9", manufacturer="Beta"), shop_key="tme"),
    )
    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    row = iis.list_pending(session, result.invoice_id)[0]
    assert row.type_id is None

    with pytest.raises(ValidationError, match="need a type"):
        invoice_service.finalize_invoice(session, result.invoice_id, user_id=1)

    # Dismissing it (and it was the only line) then leaves nothing to finalize.
    iis.dismiss_pending(session, result.invoice_id, row.id)
    with pytest.raises(ValidationError, match="no lines"):
        invoice_service.finalize_invoice(session, result.invoice_id, user_id=1)


def test_finalize_does_not_partially_materialize(
    session: Session, monkeypatch
) -> None:
    # One ready row (type + location) and one needs-review row. Finalize must reject
    # the whole thing WITHOUT creating the ready row's component (no half-done state).
    cs.create_type(session, "resistor")
    _patch_parse(
        monkeypatch,
        _invoice(
            _line(mpn="R1", manufacturer="Acme", description="Rezystor:x"),  # ready
            _line(mpn="X9", manufacturer="Beta"),  # needs review (no type)
            shop_key="tme",
        ),
    )
    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    pending = iis.list_pending(session, result.invoice_id)
    ready = next(p for p in pending if p.type_id is not None)
    iis.update_pending(
        session, result.invoice_id, ready.id, location_id=_make_location(session)
    )

    with pytest.raises(ValidationError, match="need a type"):
        invoice_service.finalize_invoice(session, result.invoice_id, user_id=1)

    # The ready row was NOT materialised — nothing created, both rows still staged.
    assert cs.list_components(session) == []
    assert len(iis.list_pending(session, result.invoice_id)) == 2


def test_finalize_blocked_by_a_case1_line_without_location_creates_nothing(
    session: Session, monkeypatch
) -> None:
    # A line matched to an existing component at import ("case 1") has no location yet,
    # alongside a ready staged row. Finalize must reject BEFORE materialising, so the
    # staged row's component is never created.
    rt = cs.create_type(session, "resistor")
    cs.create_component(session, rt.id, manufacturer="On", mpn="EXIST")
    _patch_parse(
        monkeypatch,
        _invoice(
            _line(mpn="EXIST", manufacturer="On"),  # case 1 → real line, no location
            _line(mpn="NEW", manufacturer="Acme", description="Rezystor:x"),  # ready
            shop_key="tme",
        ),
    )
    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    ready = next(p for p in iis.list_pending(session, result.invoice_id) if p.type_id)
    iis.update_pending(
        session, result.invoice_id, ready.id, location_id=_make_location(session)
    )

    with pytest.raises(ValidationError, match="location"):
        invoice_service.finalize_invoice(session, result.invoice_id, user_id=1)

    # Only the seeded component exists — the ready row was NOT materialised.
    assert [c.mpn for c in cs.list_components(session)] == ["EXIST"]
    assert len(iis.list_pending(session, result.invoice_id)) == 1


def test_finalize_rolls_back_the_whole_unit_on_a_mid_materialize_failure(
    session: Session, monkeypatch
) -> None:
    # Two new ready rows; component creation blows up on the 2nd. The atomic finalize
    # must roll back the 1st component too — nothing created, both rows still staged.
    cs.create_type(session, "resistor")
    location = _make_location(session)
    _patch_parse(
        monkeypatch,
        _invoice(
            _line(mpn="A1", manufacturer="Acme", description="Rezystor:x"),
            _line(mpn="B2", manufacturer="Beta", description="Rezystor:y"),
            shop_key="tme",
        ),
    )
    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    for row in iis.list_pending(session, result.invoice_id):
        iis.update_pending(session, result.invoice_id, row.id, location_id=location)

    real_create = cs.create_component_with_values
    calls = {"n": 0}

    def exploding_create(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom on the 2nd component")
        return real_create(*args, **kwargs)

    monkeypatch.setattr(cs, "create_component_with_values", exploding_create)

    with pytest.raises(RuntimeError, match="boom"):
        invoice_service.finalize_invoice(session, result.invoice_id, user_id=1)

    # Rolled back: no component created, both staged rows intact, still a draft.
    assert cs.list_components(session) == []
    assert len(iis.list_pending(session, result.invoice_id)) == 2
    invoice, _ = invoice_service.get_invoice_detail(session, result.invoice_id)
    assert invoice.is_finalized is False


def test_finalize_blocked_when_a_ready_row_has_no_location(
    session: Session, monkeypatch
) -> None:
    invoice_id, row = _import_one_ready(session, monkeypatch)
    # type set (ready) but no location assigned.
    with pytest.raises(ValidationError, match="location"):
        invoice_service.finalize_invoice(session, invoice_id, user_id=1)
    assert cs.list_components(session) == []  # nothing materialised


def test_finalize_applies_parameters_entered_during_review(
    session: Session, monkeypatch
) -> None:
    from app.models.enums import ParameterDataType

    rt = cs.create_type(session, "resistor")
    resistance = cs.add_parameter_definition(
        session, rt.id, name="resistance", label="R",
        data_type=ParameterDataType.NUMBER, unit="Ω",
    )
    _patch_parse(
        monkeypatch,
        _invoice(
            _line(mpn="R1", manufacturer="Acme", description="Rezystor:x"),
            shop_key="tme",
        ),
    )
    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    row = iis.list_pending(session, result.invoice_id)[0]
    iis.update_pending(
        session,
        result.invoice_id,
        row.id,
        location_id=_make_location(session),
        parameters=[{"parameter_definition_id": resistance.id, "value": "4k7"}],
    )

    invoice_service.finalize_invoice(session, result.invoice_id, user_id=1)

    component = cs.list_components(session)[0]
    values = {
        v.parameter_definition_id: v.value_num
        for v in cs.list_parameter_values(session, component.id)
    }
    assert values[resistance.id] == 4700.0  # engineering-parsed like a normal create


def test_changing_the_type_clears_reviewed_parameters(
    session: Session, monkeypatch
) -> None:
    from app.models.enums import ParameterDataType

    rt = cs.create_type(session, "resistor")
    resistance = cs.add_parameter_definition(
        session, rt.id, name="resistance", label="R",
        data_type=ParameterDataType.NUMBER, unit="Ω",
    )
    other = cs.create_type(session, "capacitor")
    _patch_parse(
        monkeypatch,
        _invoice(_line(mpn="R1", manufacturer="Acme", description="Rezystor:x")),
    )
    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    row = iis.list_pending(session, result.invoice_id)[0]
    iis.update_pending(
        session, result.invoice_id, row.id,
        parameters=[{"parameter_definition_id": resistance.id, "value": "1k"}],
    )
    assert iis.get_pending(session, result.invoice_id, row.id).parameters

    iis.update_pending(session, result.invoice_id, row.id, type_id=other.id)
    assert iis.get_pending(session, result.invoice_id, row.id).parameters == []


def test_finalize_reuses_an_existing_matching_component(
    session: Session, monkeypatch
) -> None:
    # A component with this exact Manufacturer+MPN already exists → materialise reuses
    # it instead of creating a duplicate.
    invoice_id, row = _import_one_ready(
        session, monkeypatch, mpn="R1", manufacturer="Acme"
    )
    ctype = cs.list_types(session)[0]
    existing = cs.create_component(session, ctype.id, manufacturer="Acme", mpn="R1")
    location = _make_location(session)
    iis.update_pending(session, invoice_id, row.id, location_id=location)

    invoice_service.finalize_invoice(session, invoice_id, user_id=1)

    assert len(cs.list_components(session)) == 1  # no duplicate created
    _, lines = invoice_service.get_invoice_detail(session, invoice_id)
    assert lines[0][0].component_id == existing.id


def _make_location(session: Session) -> int:
    from app.models.enums import LocationType
    from app.services import location_service as ls

    loc = ls.create_location(session, type=LocationType.BOX, name="Bin")
    return loc.id


# --- shipping line is skipped ------------------------------------------------


def test_shipping_line_is_not_a_component(session: Session, monkeypatch) -> None:
    _patch_parse(
        monkeypatch,
        _invoice(
            _line(kind="shipping", description="Koszty wysyłki", mpn=None),
            _line(mpn="XYZ123", manufacturer="Acme"),
            shop_key="tme",
        ),
    )

    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)

    # Only the component line is considered; the charge is noted, not staged/added.
    assert result.added == 0 and result.pending == 1
    invoice, _ = invoice_service.get_invoice_detail(session, result.invoice_id)
    assert invoice.notes and "Koszty wysyłki" in invoice.notes


# --- ambiguous MPN → parked --------------------------------------------------


def test_ambiguous_mpn_without_manufacturer_is_staged(
    session: Session, monkeypatch
) -> None:
    diode = cs.create_type(session, "diode")
    # Two components share the MPN under different manufacturers.
    cs.create_component(session, diode.id, manufacturer="MakerA", mpn="D1")
    cs.create_component(session, diode.id, manufacturer="MakerB", mpn="D1")
    # A Mouser line carries no manufacturer, so it can't be disambiguated.
    _patch_parse(monkeypatch, _invoice(_line(mpn="D1"), shop_key="mouser"))

    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)

    assert result.added == 0 and result.pending == 1
    assert "share this MPN" in iis.list_pending(session, result.invoice_id)[0].reason


# --- enrichment failure degrades ---------------------------------------------


def test_enrichment_failure_degrades_to_parsed_data(
    session: Session, monkeypatch
) -> None:
    class _Broken:
        def fetch_by_index(self, candidates):  # type: ignore[no-untyped-def]
            raise ValidationError("Mouser integration is not configured")

    monkeypatch.setitem(shops._BY_INDEX, "mouser", _Broken())
    _patch_parse(
        monkeypatch,
        _invoice(_line(mpn="NX3P1108UKZ", description="a switch"), shop_key="mouser"),
    )

    # No type resolves (no API category, description doesn't infer one) → staged,
    # but the import as a whole survives the failed lookup.
    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    assert result.pending == 1
    assert iis.list_pending(session, result.invoice_id)[0].mpn == "NX3P1108UKZ"


# --- a mid-import failure leaves nothing behind ------------------------------


def test_failure_mid_import_discards_the_whole_invoice(
    session: Session, monkeypatch, tmp_path
) -> None:
    diode = cs.create_type(session, "diode")
    cs.create_component(session, diode.id, mpn="OK", manufacturer="A")
    cs.create_component(session, diode.id, mpn="OK2", manufacturer="A")
    _patch_parse(
        monkeypatch,
        _invoice(
            _line(mpn="OK", manufacturer="A"),  # line 1 adds fine
            _line(mpn="OK2", manufacturer="A"),  # line 2 blows up in add_line
        ),
    )
    calls = {"n": 0}
    real_add = invoice_service.add_line

    def exploding_add(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom on line 2")
        return real_add(*args, **kwargs)

    monkeypatch.setattr(invoice_service, "add_line", exploding_add)

    with pytest.raises(RuntimeError, match="boom"):
        iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)

    # No invoice, no lines, no staged rows, no stored PDF survived the failure.
    assert invoice_service.list_invoices(session) == []
    from app.models.attachment import Attachment
    from sqlmodel import select

    assert session.exec(select(Attachment)).all() == []

    # And the same PDF can now be re-imported (the unique number isn't taken).
    monkeypatch.setattr(invoice_service, "add_line", real_add)
    _patch_parse(monkeypatch, _invoice(_line(mpn="OK", manufacturer="A")))
    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    assert result.added == 1


# --- duplicate invoice -------------------------------------------------------


def test_duplicate_invoice_is_rejected(session: Session, monkeypatch) -> None:
    _patch_parse(monkeypatch, _invoice(number="DUP"))
    iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    with pytest.raises(ValidationError, match="already exists"):
        iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)


# --- resolving a parked line clears it (via add_line reconcile) ---------------


@pytest.mark.parametrize("cleared", ["type", "location"])
def test_materialize_rejects_a_row_that_lost_its_type_or_location(
    session: Session, cleared: str
) -> None:
    # Simulates the concurrent-PATCH race: finalize's upfront snapshot said the row
    # was ready, but by the time materialize runs its type OR location has been
    # cleared. Either way it must raise (never silently skip a typeless row →
    # orphaned; never build a location-less line → 500) — and create nothing.
    from app.models.invoice import InvoiceImportLine

    rt = cs.create_type(session, "resistor")
    location = _make_location(session)
    invoice = invoice_service.create_invoice(
        session, supplier="TME", invoice_number="M", invoice_date=date(2026, 1, 1),
        currency="PLN",
    )
    session.add(
        InvoiceImportLine(
            invoice_id=invoice.id, line_no=1, mpn="R1", manufacturer="Acme",
            quantity=1, unit_price=Decimal("1"), shop_key="tme",
            type_id=None if cleared == "type" else rt.id,
            location_id=None if cleared == "location" else location,
            reason="",
        )
    )
    session.commit()

    with pytest.raises(ValidationError, match="changed during finalize"):
        iis.materialize_ready_lines(session, invoice.id, user_id=1)
    assert cs.list_components(session) == []


def test_update_and_dismiss_pending_refuse_a_finalized_invoice(
    session: Session,
) -> None:
    from app.services.errors import InvoiceFinalizedError

    diode = cs.create_type(session, "diode")
    comp = cs.create_component(session, diode.id, manufacturer="On", mpn="OK")
    location = _make_location(session)
    invoice = invoice_service.create_invoice(
        session, supplier="X", invoice_number="F", invoice_date=date(2026, 1, 1),
        currency="PLN",
    )
    line = invoice_service.add_line(
        session, invoice.id, component_id=comp.id, quantity=1, unit_price=Decimal("1")
    )
    invoice_service.set_line_location(session, invoice.id, line.id, location, user_id=1)
    invoice_service.finalize_invoice(session, invoice.id, user_id=1)

    # A finalized invoice is read-only — a stray review PATCH/dismiss is refused.
    with pytest.raises(InvoiceFinalizedError):
        iis.dismiss_pending(session, invoice.id, 999)
    with pytest.raises(InvoiceFinalizedError):
        iis.update_pending(session, invoice.id, 999, location_id=location)


# --- delete a draft clears its import artefacts -------------------------------


def test_delete_draft_invoice_removes_lines_staging_and_pdf(
    session: Session, monkeypatch
) -> None:
    diode = cs.create_type(session, "diode")
    cs.create_component(session, diode.id, mpn="OK", manufacturer="A")
    _patch_parse(
        monkeypatch,
        _invoice(
            _line(mpn="OK", manufacturer="A"),  # added
            _line(mpn="XYZ", manufacturer="A"),  # parked
        ),
    )
    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    assert result.added == 1 and result.pending == 1

    invoice_service.delete_invoice(session, result.invoice_id)

    assert invoice_service.list_invoices(session) == []
    assert iis.list_pending(session, result.invoice_id) == []
    from app.models.attachment import Attachment
    from sqlmodel import select

    assert session.exec(select(Attachment)).all() == []


# --- dismiss -----------------------------------------------------------------


def test_dismiss_drops_a_pending_line(session: Session, monkeypatch) -> None:
    _patch_parse(
        monkeypatch,
        _invoice(_line(mpn="XYZ", manufacturer="Acme"), shop_key="tme"),
    )
    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    staged = iis.list_pending(session, result.invoice_id)[0]

    iis.dismiss_pending(session, result.invoice_id, staged.id)
    assert iis.list_pending(session, result.invoice_id) == []


def test_dismiss_rejects_a_foreign_invoice_id(session: Session, monkeypatch) -> None:
    _patch_parse(
        monkeypatch,
        _invoice(_line(mpn="XYZ", manufacturer="Acme"), shop_key="tme"),
    )
    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    staged = iis.list_pending(session, result.invoice_id)[0]
    # The staging row exists, but not under this (wrong) invoice id.
    with pytest.raises(NotFoundError):
        iis.dismiss_pending(session, result.invoice_id + 999, staged.id)
