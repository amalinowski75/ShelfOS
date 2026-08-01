"""Per-shop invoice-PDF parser tests.

The parsers consume extracted PDF text, so these run against text fixtures under
``tests/fixtures/invoices/`` — the pdfplumber output of the three real invoices with
the buyer's name/address/VAT redacted (the parsers never read that block). The real
PDFs are kept out of the repo (``samples/`` is gitignored); ``extract_text`` itself is
covered separately with a generated PDF so no personal file is committed.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from app.services.errors import ValidationError
from app.services.invoice_import import parse_invoice
from app.services.invoice_import.digikey import DigiKeyInvoiceParser
from app.services.invoice_import.mouser import MouserInvoiceParser
from app.services.invoice_import.pdf import extract_text
from app.services.invoice_import.tme import TmeInvoiceParser

_DIR = Path(__file__).parent / "fixtures" / "invoices"


def _text(name: str) -> str:
    return (_DIR / name).read_text()


def _by_mpn(invoice, mpn):  # type: ignore[no-untyped-def]
    return next(line for line in invoice.lines if line.mpn == mpn)


# --- TME ---------------------------------------------------------------------


def test_tme_header() -> None:
    invoice = TmeInvoiceParser().parse(_text("tme.txt"))
    assert invoice.supplier == "TME"
    assert invoice.shop_key == "tme"
    assert invoice.invoice_number == "1261247129"
    assert invoice.invoice_date == date(2026, 4, 20)
    assert invoice.currency == "PLN"


def test_tme_reads_mpn_from_symbol_not_the_wrapped_article() -> None:
    invoice = TmeInvoiceParser().parse(_text("tme.txt"))
    # The article column wraps this MPN across two rows ("GRM022R60J104KE1" + "5L");
    # the Symbol producenta field carries it whole. Parsing the article would drop
    # the "5L".
    line = _by_mpn(invoice, "GRM022R60J104KE15L")
    assert line.manufacturer == "MURATA"
    # And a part whose Symbol producenta value itself sat on a page break (its "__"
    # glyphs must be stripped) still reads clean.
    assert any(line.mpn == "GRM31CR60J227ME11L" for line in invoice.lines)


def test_tme_per_pack_unit_price() -> None:
    invoice = TmeInvoiceParser().parse(_text("tme.txt"))
    # "2,01/1000 SZT" is a price per 1000 → 0.002010 per unit, quantity 10 000.
    line = _by_mpn(invoice, "0402WGF1004TCE")
    assert line.quantity == 10000
    assert line.unit_price == Decimal("0.002010")
    assert line.manufacturer == "ROYALOHM"


def test_tme_classifies_shipping_line() -> None:
    invoice = TmeInvoiceParser().parse(_text("tme.txt"))
    shipping = [line for line in invoice.lines if line.kind == "shipping"]
    assert len(shipping) == 1
    assert shipping[0].mpn is None
    # 10 real components + 1 shipping charge.
    assert sum(1 for line in invoice.lines if line.kind == "component") == 10


# --- Mouser ------------------------------------------------------------------


def test_mouser_header() -> None:
    invoice = MouserInvoiceParser().parse(_text("mouser.txt"))
    assert invoice.supplier == "Mouser"
    assert invoice.invoice_number == "91185641"
    assert invoice.invoice_date == date(2026, 7, 2)
    assert invoice.currency == "PLN"


def test_mouser_line_reads_mpn_and_shop_sku() -> None:
    invoice = MouserInvoiceParser().parse(_text("mouser.txt"))
    line = _by_mpn(invoice, "NX3P1108UKZ")
    assert line.supplier_part_number == "771-NX3P1108UKZ"
    assert line.quantity == 100
    assert line.unit_price == Decimal("0.853000")
    # Manufacturer is deliberately not split from the description — it is enriched
    # from the shop API — but the description's leading text is kept as notes.
    assert line.manufacturer is None
    assert line.description and line.description.startswith("NXP Semiconductors")


def test_mouser_reads_all_lines_across_pages() -> None:
    invoice = MouserInvoiceParser().parse(_text("mouser.txt"))
    assert len(invoice.lines) == 11
    # A second-page line is present.
    assert any(line.mpn == "632712000112" for line in invoice.lines)


# --- Digi-Key ----------------------------------------------------------------


def test_digikey_header() -> None:
    invoice = DigiKeyInvoiceParser().parse(_text("digikey.txt"))
    assert invoice.supplier == "Digi-Key"
    assert invoice.invoice_number == "123458567"
    # The 2nd date on the Order/Invoice/Ship/Document row, not the order date.
    assert invoice.invoice_date == date(2026, 4, 6)
    assert invoice.currency == "PLN"


def test_digikey_splits_mfg_and_strips_va() -> None:
    invoice = DigiKeyInvoiceParser().parse(_text("digikey.txt"))
    line = _by_mpn(invoice, "AP22615AWU-7")
    # "MFG : Diodes Incorporated (VA) / AP22615AWU-7" — the (VA) marker is dropped.
    assert line.manufacturer == "Diodes Incorporated"
    assert line.supplier_part_number == "AP22615AWU-7DICT-ND"
    assert line.quantity == 25
    assert line.unit_price == Decimal("1.737200")


def test_digikey_mpn_may_contain_a_slash() -> None:
    invoice = DigiKeyInvoiceParser().parse(_text("digikey.txt"))
    # "MFG : Microchip Technology / MCP2200-I/MQ" (no (VA)) — the MPN keeps its slash.
    line = _by_mpn(invoice, "MCP2200-I/MQ")
    assert line.manufacturer == "Microchip Technology"


# --- integrity: a dropped line fails loudly, not silently --------------------


def test_tme_raises_when_an_item_row_is_unparseable() -> None:
    # Corrupt line 1's price so its header row no longer matches; the "Producent:"
    # marker count (10) then exceeds the parsed components (9).
    broken = _text("tme.txt").replace("2,01/1000", "2,0X/1000", 1)
    with pytest.raises(ValidationError, match="wasn't fully understood"):
        TmeInvoiceParser().parse(broken)


def test_mouser_raises_when_an_item_row_is_unparseable() -> None:
    # Corrupt one unit price so its item row fails to match; the "Numer katalogowy"
    # markers (11) then exceed the parsed lines (10) — a silent drop is caught.
    broken = _text("mouser.txt").replace("25,12", "2X,12", 1)
    with pytest.raises(ValidationError, match="wasn't fully understood"):
        MouserInvoiceParser().parse(broken)


def test_digikey_raises_when_an_item_row_is_unparseable() -> None:
    broken = _text("digikey.txt").replace("6.12900", "6.12X00", 1)
    with pytest.raises(ValidationError, match="wasn't fully understood"):
        DigiKeyInvoiceParser().parse(broken)


def test_mouser_requires_a_currency() -> None:
    broken = _text("mouser.txt").replace("Price(PLN)", "Price()")
    with pytest.raises(ValidationError, match="currency"):
        MouserInvoiceParser().parse(broken)


def test_digikey_requires_a_currency() -> None:
    broken = _text("digikey.txt").replace("zł", "")
    with pytest.raises(ValidationError, match="currency"):
        DigiKeyInvoiceParser().parse(broken)


# --- Registry / extract_text -------------------------------------------------


def test_parse_invoice_rejects_unrecognised_text(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import app.services.invoice_import as pkg

    monkeypatch.setattr(pkg, "extract_text", lambda data: "a plain shopping receipt")
    with pytest.raises(ValidationError, match="unrecognised invoice"):
        parse_invoice(b"%PDF-1.4", "receipt.pdf")


def _minimal_pdf(text: str) -> bytes:
    """A one-page text PDF with a correct xref, built by hand (no writer library)."""
    stream = b"BT /F1 12 Tf 20 100 Td (" + text.encode("latin-1") + b") Tj ET\n"
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 144]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length %d>>\nstream\n%s endstream" % (len(stream), stream),
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (
        len(objs) + 1,
        xref_pos,
    )
    return bytes(out)


def test_extract_text_reads_a_real_pdf() -> None:
    assert "Hello Invoice 123" in extract_text(_minimal_pdf("Hello Invoice 123"))


def test_extract_text_rejects_a_pdf_with_no_text() -> None:
    with pytest.raises(ValidationError, match="no extractable text"):
        # A valid PDF page carrying no text object at all.
        extract_text(_minimal_pdf(" "))


def test_extract_text_rejects_a_non_pdf() -> None:
    with pytest.raises(ValidationError, match="could not read this PDF"):
        extract_text(b"this is not a pdf")
