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


def test_tme_description_drops_the_wrapped_article_fragment() -> None:
    invoice = TmeInvoiceParser().parse(_text("tme.txt"))
    # The article "GRM022R60J104KE15L" wraps as "…KE1" + "5L"; the "5L" fragment must
    # not leak into the description, which should start at the spec text.
    line = _by_mpn(invoice, "GRM022R60J104KE15L")
    assert line.description is not None
    assert not line.description.startswith("5L")
    assert line.description.startswith("Kondensator")


def test_tme_sets_the_supplier_symbol_including_a_rewrapped_one() -> None:
    invoice = TmeInvoiceParser().parse(_text("tme.txt"))
    # The article column is TME's own symbol — the supplier index enrichment keys on.
    line = _by_mpn(invoice, "0402WGF1004TCE")
    assert line.supplier_part_number == "0402WGF1004TCE"
    # A wrapped article ("GRM022R60J104KE1" + "5L") is glued back together, so the
    # stored symbol is whole, not the truncated first row.
    wrapped = _by_mpn(invoice, "GRM022R60J104KE15L")
    assert wrapped.supplier_part_number == "GRM022R60J104KE15L"
    # A charge line carries no symbol (its article cell is prose).
    shipping = next(row for row in invoice.lines if row.kind == "shipping")
    assert shipping.supplier_part_number is None


def test_tme_drops_a_possibly_truncated_long_article() -> None:
    # The article column wraps at ~16 chars. A >=16-char article that does NOT match
    # the MPN can't be verified whole — it may be a truncated fragment of a house
    # symbol, and a truncated string offered to the API could collide with an
    # unrelated real symbol. It must be dropped, not persisted.
    from app.services.invoice_import.tme import _ITEM, _line

    row = _ITEM.match(
        "1         HOUSESYMBOL16CHR                 10 SZT           1,00/SZT   "
        "        23                             10,00"
    )
    assert row is not None
    parsed = _line(
        row,
        [
            "Kondensator:foo;bar",
            "Producent: ACME; Symbol producenta: OTHER-MPN;",
        ],
    )
    assert len("HOUSESYMBOL16CHR") == 16
    assert parsed.mpn == "OTHER-MPN"
    assert parsed.supplier_part_number is None  # unverifiable → dropped
    # A short article (can't have wrapped) is kept even when it differs from the MPN.
    short = _ITEM.match(
        "2         SHORTSYM                 10 SZT           1,00/SZT           "
        "23                             10,00"
    )
    assert short is not None
    parsed_short = _line(
        short, ["Producent: ACME; Symbol producenta: OTHER-MPN;"]
    )
    assert parsed_short.supplier_part_number == "SHORTSYM"


def test_tme_does_not_fabricate_a_symbol_from_a_coincidental_suffix() -> None:
    # The description's first token being a suffix of the MPN triggers the notes
    # cleanup, but the symbol is re-glued ONLY when article + fragment equals the
    # MPN exactly. Here it doesn't ("ABC123" + "23" != "XYZ23"), so the symbol must
    # stay the raw article — a fabricated "ABC12323" could collide with some real,
    # unrelated TME symbol and poison the API lookup.
    from app.services.invoice_import.tme import _ITEM, _line

    row = _ITEM.match(
        "1         ABC123                 10 SZT           1,00/SZT           "
        "23                             10,00"
    )
    assert row is not None
    parsed = _line(
        row,
        [
            "23 Kondensator:foo;bar",
            "Producent: ACME; Symbol producenta: XYZ23;",
            "Zgodność RoHS",
        ],
    )
    assert parsed.mpn == "XYZ23"
    assert parsed.supplier_part_number == "ABC123"  # NOT "ABC12323"


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


def test_mouser_raises_on_an_item_shaped_line_it_cannot_read() -> None:
    # Corrupt one unit price so its item row no longer matches; because the row still
    # looks like an item ("<line> <sku> …"), the parser fails loudly and names it
    # rather than folding it silently into the previous line's continuation.
    broken = _text("mouser.txt").replace("25,12", "2X,12", 1)
    with pytest.raises(ValidationError, match="could not read this Mouser line"):
        MouserInvoiceParser().parse(broken)


def test_mouser_reads_comma_thousands_quantities() -> None:
    # A bulk reel is quantitied "2,500" (= 2500) — a thousands separator, not a
    # decimal. The row must match and parse to 2500, not be dropped.
    from app.services.invoice_import.base import to_int
    from app.services.invoice_import.mouser import _ITEM

    row = "   6  810-C1005X7R1V104MBB   2,500   2,500   0   0,063   157,50"
    match = _ITEM.match(row)
    assert match is not None
    assert to_int(match.group(4)) == 2500  # shipped qty


def test_mouser_bulk_invoice_comma_quantities_and_unlabelled_marker() -> None:
    # A real invoice (redacted) that broke the old count-based check: two items are
    # quantitied "2,500", and the last item's part-number cell carries no "Numer
    # katalogowy u producenta:" label — just "20 / UCLAMP2271P.TNT", where the token
    # after the slash IS the MPN. All 11 rows parse; the comma quantities are 2500;
    # the unlabelled cell yields the MPN (and must NOT leak "20" as the description).
    invoice = MouserInvoiceParser().parse(_text("mouser_bulk.txt"))
    assert invoice.invoice_number == "89923858"
    assert len(invoice.lines) == 11
    assert sum(1 for line in invoice.lines if line.quantity == 2500) == 2
    last = invoice.lines[-1]
    assert last.mpn == "UCLAMP2271P.TNT"
    assert last.supplier_part_number == "947-UCLAMP2271P.TNT"
    assert last.description is not None
    assert last.description.startswith("Semtech")  # not the cell's "20" left half


def test_digikey_raises_when_an_item_row_is_unparseable() -> None:
    broken = _text("digikey.txt").replace("6.12900", "6.12X00", 1)
    with pytest.raises(ValidationError, match="wasn't fully understood"):
        DigiKeyInvoiceParser().parse(broken)


def test_mouser_raises_when_a_wrapped_row_would_form_a_chimera() -> None:
    # The reviewer's reproduction: item 2's row wraps so its line number sits alone
    # and the rest no longer looks item-shaped. Both halves fold into item 1's
    # continuation — and item 2's part-number marker would OVERWRITE item 1's MPN,
    # producing one line with item 1's qty/price and item 2's MPN. Must raise.
    row = "2  579-USB7006T/KDX                    5      5      0      25,12   125,60"
    text = _text("mouser.txt")
    assert "       " + row in text
    text = text.replace("       " + row, "       2\n  " + row[3:], 1)
    with pytest.raises(ValidationError, match="two part-number cells"):
        MouserInvoiceParser().parse(text)


def test_mouser_counts_markers_when_a_row_vanishes_entirely() -> None:
    # A first-table row corrupted beyond even the loose item shape: its lines drop
    # with no item active, so neither the strict nor the loose pattern can flag it.
    # The marker count (labels in text vs labels consumed) is the net that catches it.
    text = _text("mouser.txt").replace(
        "       1  771-NX3P1108UKZ", "       X  771-NX3P1108UKZ", 1
    )
    with pytest.raises(ValidationError, match="wasn't fully understood"):
        MouserInvoiceParser().parse(text)


def test_mouser_unlabelled_form_requires_a_numeric_left_token() -> None:
    # A title-less description ("Vishay / Rezystory") has the same two-token shape as
    # the unlabelled part-number cell — it must NOT be read as an MPN (a category word
    # as MPN poisons enrichment; no MPN just routes the row to review).
    text = _text("mouser.txt").replace(
        "Numer katalogowy u producenta: NX3P1108UKZ", "Vishay / Rezystory", 1
    )
    invoice = MouserInvoiceParser().parse(text)
    line = next(
        row for row in invoice.lines
        if row.supplier_part_number == "771-NX3P1108UKZ"
    )
    assert line.mpn is None  # → review, not a fake MPN
    assert line.description == "Vishay"


def test_number_helpers_reject_malformed_grouping() -> None:
    # A damaged extraction must fail loudly, not parse to a plausible wrong number.
    from app.services.invoice_import.base import to_decimal, to_int

    assert to_int("2,500") == 2500
    assert to_int("10 000") == 10000
    for bad in ("2,50", "2,5", ",500"):
        with pytest.raises(ValidationError):
            to_int(bad)
    assert to_decimal("1,234.56", decimal_sep=".") == Decimal("1234.56")
    assert to_decimal("1.234,56", decimal_sep=",") == Decimal("1234.56")
    with pytest.raises(ValidationError):
        to_decimal("6,12.9", decimal_sep=".")  # damaged "6,129" / "612.9"
    with pytest.raises(ValidationError):
        to_decimal("6.12,9", decimal_sep=",")


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
