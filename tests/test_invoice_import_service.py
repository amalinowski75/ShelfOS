"""Orchestrator tests: ParsedInvoice → draft invoice, matching and staging.

Parsing is covered separately (``test_invoice_import_parsers``); here ``parse_invoice``
is monkeypatched to return a crafted :class:`ParsedInvoice`, so the tests exercise the
match/enrich/stage decisions without depending on a PDF. Shop enrichment is faked via
``shops._BY_MPN``.
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
        def fetch_by_mpn(self, mpn, *, transport=None):  # type: ignore[no-untyped-def]
            return product

    monkeypatch.setitem(shops._BY_MPN, shop_key, _Fake())


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


def test_missing_component_with_known_type_is_created(
    session: Session, monkeypatch
) -> None:
    cs.create_type(session, "mosfet")
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

    assert result.added == 1 and result.pending == 0
    components = cs.list_components(session)
    assert len(components) == 1
    created = components[0]
    assert created.manufacturer == "NXP Semiconductors"
    assert created.mpn == "NX3P1108UKZ"
    assert created.package == "XSON8"


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
    cs.create_type(session, "resistor")
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

    assert result.added == 1 and result.pending == 0
    created = cs.list_components(session)
    assert len(created) == 1
    assert created[0].mpn == "0402WGF1004TCE"


# --- finalize is blocked while lines are still parked -------------------------


def test_finalize_blocked_while_import_lines_pending(
    session: Session, monkeypatch
) -> None:
    # One line resolves (existing component), one is parked.
    diode = cs.create_type(session, "diode")
    component = cs.create_component(session, diode.id, manufacturer="Onsemi", mpn="OK")
    _patch_parse(
        monkeypatch,
        _invoice(
            _line(mpn="OK", manufacturer="Onsemi"),
            _line(mpn="XYZ", manufacturer="Acme"),
            shop_key="tme",
        ),
    )
    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    assert result.pending == 1

    # Give the added line a location so only the parked row blocks finalization.
    _, lines = invoice_service.get_invoice_detail(session, result.invoice_id)
    location = _make_location(session)
    invoice_service.set_line_location(
        session, result.invoice_id, lines[0][0].id, location, user_id=1
    )

    with pytest.raises(ValidationError, match="need review"):
        invoice_service.finalize_invoice(session, result.invoice_id, user_id=1)

    # After dismissing the parked line, finalization goes through.
    staged = iis.list_pending(session, result.invoice_id)[0]
    iis.dismiss_pending(session, result.invoice_id, staged.id)
    invoice_service.finalize_invoice(session, result.invoice_id, user_id=1)
    invoice, _ = invoice_service.get_invoice_detail(session, result.invoice_id)
    assert invoice.is_finalized is True
    _ = component  # (referenced so linters keep the seed above)


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
        def fetch_by_mpn(self, mpn, *, transport=None):  # type: ignore[no-untyped-def]
            raise ValidationError("Mouser integration is not configured")

    monkeypatch.setitem(shops._BY_MPN, "mouser", _Broken())
    _patch_parse(
        monkeypatch,
        _invoice(_line(mpn="NX3P1108UKZ", description="a switch"), shop_key="mouser"),
    )

    # No type resolves (no API category, description doesn't infer one) → staged,
    # but the import as a whole survives the failed lookup.
    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    assert result.pending == 1
    assert iis.list_pending(session, result.invoice_id)[0].mpn == "NX3P1108UKZ"


# --- duplicate invoice -------------------------------------------------------


def test_duplicate_invoice_is_rejected(session: Session, monkeypatch) -> None:
    _patch_parse(monkeypatch, _invoice(number="DUP"))
    iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    with pytest.raises(ValidationError, match="already exists"):
        iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)


# --- resolving a parked line clears it (via add_line reconcile) ---------------


def test_resolving_a_pending_line_clears_the_staging_row(
    session: Session, monkeypatch
) -> None:
    ctype = cs.create_type(session, "diode")
    _patch_parse(
        monkeypatch,
        _invoice(_line(mpn="XYZ123", manufacturer="Acme"), shop_key="tme"),
    )
    result = iis.import_invoice(session, data=b"x", filename="f.pdf", user_id=1)
    staged = iis.list_pending(session, result.invoice_id)[0]

    component = cs.create_component(
        session, ctype.id, manufacturer="Acme", mpn="XYZ123"
    )
    invoice_service.add_line(
        session,
        result.invoice_id,
        component_id=component.id,
        quantity=staged.quantity,
        unit_price=staged.unit_price,
        import_line_id=staged.id,
    )

    assert iis.list_pending(session, result.invoice_id) == []
    _, lines = invoice_service.get_invoice_detail(session, result.invoice_id)
    assert len(lines) == 1


def test_add_line_ignores_a_foreign_import_line_id(
    session: Session, monkeypatch
) -> None:
    """A staging row from another invoice must not be deleted by this add."""
    ctype = cs.create_type(session, "diode")
    _patch_parse(
        monkeypatch, _invoice(_line(mpn="A1", manufacturer="Acme"), number="ONE")
    )
    one = iis.import_invoice(session, data=b"x", filename="a.pdf", user_id=1)
    foreign = iis.list_pending(session, one.invoice_id)[0]

    two = invoice_service.create_invoice(
        session,
        supplier="Other",
        invoice_number="TWO",
        invoice_date=date(2026, 1, 1),
        currency="PLN",
    )
    comp = cs.create_component(session, ctype.id, mpn="B1")
    invoice_service.add_line(
        session,
        two.id,
        component_id=comp.id,
        quantity=1,
        unit_price=Decimal("1"),
        import_line_id=foreign.id,
    )
    # The other invoice's pending line is untouched.
    assert len(iis.list_pending(session, one.invoice_id)) == 1


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
