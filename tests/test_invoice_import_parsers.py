"""Per-shop invoice-PDF parser tests.

The parsers consume extracted PDF text, so these run against text fixtures under
``tests/fixtures/invoices/`` — the pdfplumber output of the real invoices with the
buyer's name/address/VAT redacted (the parsers never read that block). The real
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
from app.services.invoice_import.farnell import FarnellInvoiceParser
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


def test_tme_rejoins_an_article_broken_on_its_hyphen() -> None:
    # From the field: TME broke "DS1052-082B2NA2060" after the hyphen, so the
    # article cell read "DS1052-" and its tail stood alone on the row below.
    # The symbol differs from the MPN here, so nothing could verify it — the
    # invoice kept "DS1052-" and the tail leaked into the description, and a
    # bag whose QR prints PN:DS1052-082B2NA2060 matched no line.
    from app.services.invoice_import.tme import _ITEM, _line

    row = _ITEM.match(
        "4         DS1052-                 100 SZT           25,80/10 SZT       "
        "23                             258,00"
    )
    assert row is not None
    parsed = _line(
        row,
        [
            "082B2NA2060",
            "Kabel wstążkowy ze złączami",
            "IDC;R.taśmy:1,27mm;0,6m;8x28AWG",
            "Producent: CONNFLY; Symbol producenta:",
            "DS1052-082B2NA206001; Zgodność RoHS",
        ],
    )
    assert parsed.mpn == "DS1052-082B2NA206001"  # the manufacturer's
    assert parsed.supplier_part_number == "DS1052-082B2NA2060"  # TME's own
    assert parsed.description is not None
    assert parsed.description.startswith("Kabel wstążkowy")
    assert "082B2NA2060" not in parsed.description


def test_tme_rejoins_an_article_that_wrapped_twice() -> None:
    # A symbol that wrapped twice must be glued from BOTH tail rows: stopping at
    # one would rebuild a string that is still truncated — a fabrication, and
    # worse than the drop rule it would bypass.
    from app.services.invoice_import.tme import _ITEM, _line

    row = _ITEM.match(
        "1         HOUSE-                 10 SZT           1,00/SZT   "
        "        23                             10,00"
    )
    assert row is not None
    parsed = _line(
        row,
        [
            "CONTINUED",
            "TAIL",
            "Kondensator:foo;bar",
            "Producent: ACME; Symbol producenta: OTHER-MPN;",
        ],
    )
    assert parsed.supplier_part_number == "HOUSE-CONTINUEDTAIL"
    assert parsed.description == "Kondensator:foo;bar"


def test_tme_keeps_a_one_word_description_off_an_intact_article() -> None:
    # An article that shows no break wrapped nowhere, so a one-word description
    # row underneath is description text.
    from app.services.invoice_import.tme import _ITEM, _line

    row = _ITEM.match(
        "1         ZL262-40DG                 10 SZT           1,00/SZT         "
        "  23                             10,00"
    )
    assert row is not None
    parsed = _line(
        # A bare ASCII word: indistinguishable from a tail but for the article.
        row,
        ["Transformator", "Producent: ACME; Symbol producenta: OTHER-MPN;"],
    )
    assert parsed.supplier_part_number == "ZL262-40DG"
    assert parsed.description == "Transformator"


def test_tme_never_rebuilds_an_article_that_merely_fills_the_column() -> None:
    # The column is 16 chars wide, so a 16-char article is exactly as likely to
    # be a complete symbol filling it as a truncated one — "RC1210FR-07100RL" is
    # a real line on the sample invoice. Gluing the row below onto it would
    # invent a part number that was never printed AND swallow the notes; length
    # is a reason to DROP a symbol, never to rebuild one.
    from app.services.invoice_import.tme import _ITEM, _WRAP_WIDTH, _line

    assert len("RC1210FR-07100RL") == _WRAP_WIDTH
    row = _ITEM.match(
        "5         RC1210FR-07100RL                 10 SZT           1,00/SZT   "
        "        23                             10,00"
    )
    assert row is not None
    # The description wraps, so a bare first row DOES have description rows after
    # it — the positional guard cannot help here, and only the missing break
    # character stands between this line and an invented symbol.
    parsed = _line(
        row,
        [
            "Transformator",
            "do zasilaczy impulsowych",
            "Producent: ACME; Symbol producenta: OTHER-MPN;",
        ],
    )
    assert parsed.supplier_part_number is None  # unverifiable → dropped, not glued
    assert parsed.description == "Transformator do zasilaczy impulsowych"


def test_tme_keeps_a_one_word_description_off_a_broken_article() -> None:
    # The break character opens the door, but a tail stands ABOVE the
    # description — so a bare token that is the LAST row before "Producent:" is
    # a one-word description, not the column's tail.
    from app.services.invoice_import.tme import _ITEM, _line

    row = _ITEM.match(
        "1         DS1052-                 10 SZT           1,00/SZT         "
        "  23                             10,00"
    )
    assert row is not None
    parsed = _line(
        row,
        ["Kondensator", "Producent: ACME; Symbol producenta: OTHER-MPN;"],
    )
    assert parsed.supplier_part_number == "DS1052-"  # left as printed
    assert parsed.description == "Kondensator"


def test_tme_reads_a_wrapped_manufacturer_block_as_no_description() -> None:
    # The "a tail has description rows after it" test has to stop at the
    # manufacturer block, because that block wraps too — USBLC6-2SC6 on the
    # sample invoice prints as "Producent: STMicroelectronics; Symbol
    # producenta:" + "USBLC6-2SC6; Zgodność RoHS". Its second row is non-empty
    # and carries no "Producent:", so a row-by-row scan that reads past the
    # block sees description text that is not there — and eats the one-word
    # description the candidate tail was sitting on.
    from app.services.invoice_import.tme import _ITEM, _line

    row = _ITEM.match(
        "1         DS1052-                 10 SZT           1,00/SZT         "
        "  23                             10,00"
    )
    assert row is not None
    parsed = _line(
        row,
        [
            "Kondensator",
            "Producent: ACME; Symbol producenta:",
            "OTHER-MPN; Zgodność RoHS",
        ],
    )
    assert parsed.supplier_part_number == "DS1052-"  # not "DS1052-Kondensator"
    assert parsed.description == "Kondensator"
    assert parsed.mpn == "OTHER-MPN"  # the block still reads normally


def test_tme_rejoins_an_article_broken_on_any_separator() -> None:
    # TME symbols carry "/" and "." as well as "-", and the column can break after
    # any of them. The separators are spelled out here rather than read from
    # _BREAK_CHARS: a test that loops over the constant it is pinning passes for
    # any value of it, including one with the extras dropped.
    from app.services.invoice_import.tme import _BREAK_CHARS, _ITEM, _line

    assert set(_BREAK_CHARS) == {"-", "/", "."}  # a new one needs a case below
    for char in ("-", "/", "."):
        row = _ITEM.match(
            f"1         HOUSE{char}                 10 SZT           1,00/SZT   "
            "        23                             10,00"
        )
        assert row is not None, char
        parsed = _line(
            row,
            [
                "TAIL",
                "Kondensator:foo;bar",
                "Producent: ACME; Symbol producenta: OTHER-MPN;",
            ],
        )
        assert parsed.supplier_part_number == f"HOUSE{char}TAIL", char


def test_tme_stops_gluing_tails_at_the_description() -> None:
    # After a genuine tail the loop must still stop at the description: an
    # unbounded run would eat a one-word description row too and lose the notes.
    from app.services.invoice_import.tme import _ITEM, _line

    row = _ITEM.match(
        "1         HOUSE-                 10 SZT           1,00/SZT         "
        "  23                             10,00"
    )
    assert row is not None
    parsed = _line(
        row,
        [
            "CONTINUED",
            "Transformator",
            "Producent: ACME; Symbol producenta: OTHER-MPN;",
        ],
    )
    assert parsed.supplier_part_number == "HOUSE-CONTINUED"
    assert parsed.description == "Transformator"


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
    parsed_short = _line(short, ["Producent: ACME; Symbol producenta: OTHER-MPN;"])
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


# --- Farnell -----------------------------------------------------------------


def test_farnell_header() -> None:
    invoice = FarnellInvoiceParser().parse(_text("farnell.txt"))
    assert invoice.supplier == "Farnell"
    assert invoice.shop_key == "farnell"
    assert invoice.invoice_number == "8208977"
    # "24 AUG 2026" — the same DD-Mon-YYYY as the others, spaced not hyphenated.
    assert invoice.invoice_date == date(2026, 8, 24)


def test_farnell_takes_the_invoice_currency_not_the_vat_information_one() -> None:
    # The invoice states its money in PLN and ALSO restates the goods total in GBP
    # under "For VAT Information Only". Reading the wrong one would record every
    # price on the invoice in a currency nobody paid.
    text = _text("farnell.txt")
    assert "GBP" in text
    assert FarnellInvoiceParser().parse(text).currency == "PLN"


def test_farnell_reads_a_line_across_its_three_rows() -> None:
    invoice = FarnellInvoiceParser().parse(_text("farnell.txt"))
    line = _by_mpn(invoice, "NCP730BMT330TBG")
    # The order code is element14's own catalogue number, so enrichment can look the
    # part up by `id:` and cannot land on another maker's part.
    assert line.supplier_part_number == "3367839"
    assert line.quantity == 5
    assert line.unit_price == Decimal("3.770000")
    # MPN and description share a row with only a space between them.
    assert line.description == "LDO, FIXED, 3.3V, 0.15A, -40 TO 125DEG C"
    # Farnell prints no manufacturer column at all.
    assert line.manufacturer is None


def test_farnell_reads_every_line_including_the_page_break() -> None:
    invoice = FarnellInvoiceParser().parse(_text("farnell.txt"))
    components = [line for line in invoice.lines if line.kind == "component"]
    assert len(components) == 7  # 5 on page 1, 2 on page 2
    assert [line.supplier_part_number for line in components] == [
        "3772760",
        "2490597",
        "2300551",
        "3367839",
        "3596298",
        "2164811",  # the page break falls here
        "2497595",
    ]


def test_farnell_line_total_reconciles_with_the_invoice_total() -> None:
    # An independent check on the columns: the parser could plausibly take the LIST
    # price, or the vat rate, or misread a quantity, and every assertion above would
    # still pass on this invoice (its discounts are all zero). Summing has to hit the
    # stated total.
    invoice = FarnellInvoiceParser().parse(_text("farnell.txt"))
    total = sum(line.quantity * line.unit_price for line in invoice.lines)
    assert total == Decimal("577.91")  # "Invoice Total PLN 577.91"


def test_farnell_takes_the_net_price_not_the_list_one() -> None:
    # Every discount on the sample invoice is 0.00, so list and net are identical
    # there and no assertion over it can tell the two columns apart — including the
    # total-reconciliation one. This is the row that can: a 10% discount, where
    # taking the list price would overstate what was paid by 11%.
    from app.services.invoice_import.farnell import _ITEM, _line

    row = _ITEM.match(
        "     3  2300551                         EA    5 12.5800 10.00 11.3220"
        " 0.00 56.61   "
    )
    assert row is not None
    parsed = _line(row, ["        1270.1001 LIGHT GUIDE, DUAL, HORIZONTAL,3MM,2W"])
    assert parsed.unit_price == Decimal("11.322000")
    assert parsed.quantity == 5


def test_farnell_mpn_may_contain_a_comma_or_a_plus() -> None:
    invoice = FarnellInvoiceParser().parse(_text("farnell.txt"))
    # The MPN is everything up to the first space, so punctuation inside it survives.
    assert _by_mpn(invoice, "NTS0102GT,115").supplier_part_number == "2164811"
    assert _by_mpn(invoice, "MAX14689AETB+T").supplier_part_number == "3596298"


def test_farnell_delivery_charge_is_a_charge_not_a_component() -> None:
    # It sits among the items with no line number and no order code. Imported as a
    # component it would become a phantom part; dropped entirely, the invoice would
    # no longer add up.
    invoice = FarnellInvoiceParser().parse(_text("farnell.txt"))
    charges = [line for line in invoice.lines if line.kind != "component"]
    assert len(charges) == 1
    assert charges[0].description == "EXPRESS"
    assert charges[0].unit_price == Decimal("39.990000")
    assert charges[0].supplier_part_number is None


def test_farnell_a_charge_shaped_description_stays_with_its_item() -> None:
    # The charge shape is offered every row of an open block, including the one
    # carrying the MPN and description. Taking that row would BOTH invent a charge
    # and strip the item bare — and neither integrity check can see it, since the
    # line is still parsed and still one per customs code. Requiring the item to
    # already have a row of its own closes it, because the real charge always
    # follows a completed block.
    poisoned = _text("farnell.txt").replace(
        "NCP730BMT330TBG LDO, FIXED, 3.3V, 0.15A, -40 TO 125DEG C",
        "ABCDEF LDO FIXED                                   3.30 0.15",
    )
    invoice = FarnellInvoiceParser().parse(poisoned)
    line = next(
        row for row in invoice.lines if row.supplier_part_number == "3367839"
    )
    assert line.mpn == "ABCDEF"  # not None: the row stayed with its item
    assert [row.description for row in invoice.lines if row.kind != "component"] == [
        "EXPRESS"
    ]


def test_farnell_reads_an_invoice_carrying_no_customs_codes() -> None:
    # "Tariff Code:" is a fact about a cross-border shipment, not a catalogue
    # column — a domestic invoice can carry none. Counting it unconditionally
    # refused a document this parser reads perfectly well, with a message ("7 of 0")
    # that read as a parser bug rather than as what happened.
    domestic = "\n".join(
        row for row in _text("farnell.txt").splitlines() if "Tariff Code:" not in row
    )
    invoice = FarnellInvoiceParser().parse(domestic)
    assert len([row for row in invoice.lines if row.kind == "component"]) == 7


def test_farnell_notices_a_row_that_broke_outside_the_money_columns() -> None:
    # _LOOSE_ITEM shares the order-code and unit shapes with _ITEM, so it only flags
    # a row that broke in the AMOUNTS. One that breaks in the unit of measure — a
    # unit this parser has not seen — matches neither and folds into the block above
    # it. Farnell numbers its own lines, and the gap is what shows.
    broken = _text("farnell.txt").replace(
        "     3  2300551                         EA ",
        "     3  2300551                         E  ",
    )
    with pytest.raises(ValidationError, match="line 3 was not recognised"):
        FarnellInvoiceParser().parse(broken)


def test_farnell_still_catches_a_row_dropped_from_the_end() -> None:
    # The gap the numbering cannot see: dropping the LAST row leaves 1..N-1, which
    # is contiguous. This is what the customs-code count is still there for.
    text = _text("farnell.txt")
    broken = text.replace("     7  2497595", "     X  2497595", 1)
    with pytest.raises(ValidationError, match="wasn't fully understood"):
        FarnellInvoiceParser().parse(broken)


def test_farnell_currency_requires_an_amount_beside_the_code() -> None:
    # "Invoice Total" is printed on every page and only the last one states the
    # money. Nothing on this invoice sits where a bare "Invoice Total <WORD>" would
    # be misread, so the fixture cannot demonstrate the guard — pin it on the
    # pattern itself rather than claim a coverage that isn't there.
    from app.services.invoice_import.farnell import _CURRENCY

    match = _CURRENCY.search(
        "Invoice Total\n     PAY NOW\nInvoice Total  PLN 577.91\n"
    )
    assert match is not None
    assert match.group(1) == "PLN"


def test_farnell_reads_an_item_that_has_no_description_row() -> None:
    # The customs row is skipped when looking for the "<MPN> <description>" row, so
    # an item printed without one yields no MPN rather than a customs code as its
    # part number. Not exercised by the sample invoice, where every item has both.
    from app.services.invoice_import.farnell import _ITEM, _line

    row = _ITEM.match(
        "     1  3772760                         TC  100 1.3300 0.00 1.3300"
        " 0.00  133.00"
    )
    assert row is not None
    parsed = _line(row, ["        Tariff Code: IL 85412900"])
    assert parsed.mpn is None
    assert parsed.description is None
    assert parsed.supplier_part_number == "3772760"


def test_farnell_end_of_table_marker_survives_the_extractor() -> None:
    # Why the parser stops on "P&P Charge" and not on the "Very Important" beside
    # it: pdfplumber renders that heading as "Very Im portant" on one page of this
    # invoice. Recorded as a test because the alternative reads like the obvious
    # choice, and nothing else in the fixture explains why it was not taken.
    text = _text("farnell.txt")
    assert "Very Im portant" in text
    assert text.count("P&P Charge") == 3  # every page, intact


def test_farnell_does_not_read_the_prose_around_the_items_as_charges() -> None:
    # The page-2 footer has all-caps prose sitting inside the last item's block
    # ("ORDER PLACED BY MR …", "*** PAID BY CREDIT/DEBIT CARD ***"). The charge
    # shape has to be narrow enough to leave it alone.
    text = _text("farnell.txt")
    assert "ORDER PLACED BY" in text
    invoice = FarnellInvoiceParser().parse(text)
    assert [line.description for line in invoice.lines if line.kind != "component"] == [
        "EXPRESS"
    ]


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
        row for row in invoice.lines if row.supplier_part_number == "771-NX3P1108UKZ"
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


def test_farnell_raises_on_an_item_shaped_line_it_cannot_read() -> None:
    # Corrupt one net price so the strict row no longer matches. The row still looks
    # item-shaped, so the parser names it rather than folding it into the previous
    # item's continuation — where its MPN row would overwrite that item's.
    broken = _text("farnell.txt").replace(
        "37.3900 0.00 37.3900", "37.3900 0.00 37.X900", 1
    )
    with pytest.raises(ValidationError, match="could not read this Farnell line"):
        FarnellInvoiceParser().parse(broken)


def test_farnell_counts_customs_codes_when_a_row_vanishes_entirely() -> None:
    # A row damaged past even the loose shape drops with no item active, so neither
    # pattern can flag it. Every component carries one "Tariff Code:", so the count
    # is the net that catches it.
    broken = _text("farnell.txt").replace("     1  3772760", "     X  3772760", 1)
    with pytest.raises(ValidationError, match="wasn't fully understood"):
        FarnellInvoiceParser().parse(broken)


def test_farnell_requires_a_currency() -> None:
    broken = _text("farnell.txt").replace("Invoice Total  PLN 577.91", "Invoice Total")
    with pytest.raises(ValidationError, match="currency"):
        FarnellInvoiceParser().parse(broken)


# --- Registry / extract_text -------------------------------------------------


def test_parse_invoice_rejects_unrecognised_text(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import app.services.invoice_import as pkg

    monkeypatch.setattr(pkg, "extract_text", lambda data: "a plain shopping receipt")
    with pytest.raises(ValidationError, match="unrecognised invoice"):
        parse_invoice(b"%PDF-1.4", "receipt.pdf")


@pytest.mark.parametrize(
    ("fixture", "shop_key"),
    [
        ("tme.txt", "tme"),
        ("mouser.txt", "mouser"),
        ("digikey.txt", "digikey"),
        ("farnell.txt", "farnell"),
    ],
)
def test_parse_invoice_picks_the_right_parser(
    fixture: str, shop_key: str, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    """Registering a parser is the whole integration surface — check it took.

    Every fixture against the whole registry, not just its own parser: a signature
    that also matches another shop's invoice would silently import it under the
    wrong shop, and only trying them all can show that.
    """
    import app.services.invoice_import as pkg

    monkeypatch.setattr(pkg, "extract_text", lambda data: _text(fixture))
    assert parse_invoice(b"%PDF-1.4", fixture).shop_key == shop_key


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
