"""Service-level tests for file attachments (spec §10)."""

from __future__ import annotations

from datetime import date

import pytest
from app import config
from app.models.attachment import Attachment
from app.models.enums import AttachmentKind
from app.services import attachment_service as ats
from app.services import component_service as cs
from app.services import invoice_service as inv
from app.services.errors import NotFoundError, ValidationError
from sqlmodel import Session


@pytest.fixture
def store(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """Point the attachment store at a throwaway directory."""
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path)
    return tmp_path


def _component(session: Session) -> object:
    ctype = cs.create_type(session, "resistor")
    return cs.create_component(session, ctype.id)


def _invoice(session: Session) -> object:
    return inv.create_invoice(
        session,
        supplier="Mouser",
        invoice_number="INV-1",
        invoice_date=date(2026, 7, 15),
        currency="EUR",
    )


def _attach(session: Session, entity_type: str, entity_id: int, **kw) -> Attachment:  # type: ignore[no-untyped-def]
    return ats.create_attachment(
        session,
        entity_type=entity_type,
        entity_id=entity_id,
        kind=kw.get("kind", AttachmentKind.DATASHEET),
        filename=kw.get("filename", "ds.pdf"),
        data=kw.get("data", b"%PDF-1.4 bytes"),
        notes=kw.get("notes"),
    )


def _png_bytes(size: tuple[int, int] = (400, 300)) -> bytes:
    """A real PNG so Pillow can actually decode/resize it."""
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", size, (10, 80, 160)).save(buffer, "PNG")
    return buffer.getvalue()


def _image(session: Session, entity_id: int) -> Attachment:
    return _attach(
        session, "component", entity_id, filename="front.png", data=_png_bytes()
    )


def test_create_writes_row_and_file(session: Session, store) -> None:  # type: ignore[no-untyped-def]
    component = _component(session)
    att = _attach(session, "component", component.id, data=b"%PDF-1.4 bytes")

    assert att.id is not None
    assert att.filename == "ds.pdf"
    # The stored name is UUID-based — different from, and safer than, the original.
    assert att.file_path != att.filename
    assert "/" not in att.file_path and ".." not in att.file_path
    assert (store / att.file_path).read_bytes() == b"%PDF-1.4 bytes"


def test_create_sanitizes_stored_name_but_keeps_filename(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    component = _component(session)
    att = _attach(session, "component", component.id, filename="../../etc/passwd.txt")

    # The original name is preserved for download...
    assert att.filename == "../../etc/passwd.txt"
    # ...but the file lands directly under the store, with a safe extension only.
    assert (store / att.file_path).parent == store
    assert att.file_path.endswith(".txt")


def test_create_rejects_unknown_entity_type(session: Session, store) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError):
        _attach(session, "widget", 1)
    assert list(store.iterdir()) == []  # nothing written


def test_create_rejects_missing_entity(session: Session, store) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(NotFoundError):
        _attach(session, "component", 999)


def test_create_rejects_empty_file(session: Session, store) -> None:  # type: ignore[no-untyped-def]
    component = _component(session)
    with pytest.raises(ValidationError):
        _attach(session, "component", component.id, data=b"")


def test_create_rejects_oversize(session: Session, store, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config, "MAX_ATTACHMENT_BYTES", 4)
    component = _component(session)
    with pytest.raises(ValidationError):
        _attach(session, "component", component.id, data=b"too long")
    assert list(store.iterdir()) == []  # oversize file never written


def test_list_filters_by_entity(session: Session, store) -> None:  # type: ignore[no-untyped-def]
    component = _component(session)
    invoice = _invoice(session)
    a1 = _attach(session, "component", component.id)
    a2 = _attach(session, "component", component.id)
    _attach(session, "invoice", invoice.id, kind=AttachmentKind.INVOICE_PDF)

    got = ats.list_attachments(session, entity_type="component", entity_id=component.id)
    assert [a.id for a in got] == [a1.id, a2.id]


def test_list_rejects_unknown_entity_type(session: Session, store) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError):
        ats.list_attachments(session, entity_type="widget", entity_id=1)


def test_list_of_missing_entity_raises(session: Session, store) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(NotFoundError):
        ats.list_attachments(session, entity_type="component", entity_id=999)


def test_create_rejects_overlong_filename(session: Session, store) -> None:  # type: ignore[no-untyped-def]
    component = _component(session)
    with pytest.raises(ValidationError):
        _attach(session, "component", component.id, filename="x" * 300)


def test_create_rejects_overlong_notes(session: Session, store) -> None:  # type: ignore[no-untyped-def]
    component = _component(session)
    with pytest.raises(ValidationError):
        _attach(session, "component", component.id, notes="x" * 3000)


def test_get_unknown_raises(session: Session, store) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(NotFoundError):
        ats.get_attachment(session, 999)


def test_delete_removes_row_and_file(session: Session, store) -> None:  # type: ignore[no-untyped-def]
    component = _component(session)
    att = _attach(session, "component", component.id)
    stored = store / att.file_path
    assert stored.exists()

    ats.delete_attachment(session, att.id)

    assert not stored.exists()
    with pytest.raises(NotFoundError):
        ats.get_attachment(session, att.id)


def test_delete_tolerates_a_missing_file(session: Session, store) -> None:  # type: ignore[no-untyped-def]
    component = _component(session)
    att = _attach(session, "component", component.id)
    (store / att.file_path).unlink()  # file gone, row remains

    ats.delete_attachment(session, att.id)  # must not raise

    with pytest.raises(NotFoundError):
        ats.get_attachment(session, att.id)


def test_stored_file_path_rejects_traversal(store) -> None:  # type: ignore[no-untyped-def]
    att = Attachment(
        entity_type="component",
        entity_id=1,
        kind=AttachmentKind.OTHER,
        file_path="../secret",
        filename="x",
    )
    with pytest.raises(NotFoundError):
        ats.stored_file_path(att)


def test_thumbnail_generates_and_caches_a_downscaled_png(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    from PIL import Image

    component = _component(session)
    att = _image(session, component.id)

    thumb = ats.thumbnail_file(session, att.id)
    assert thumb.parent == store / ".thumbs"
    assert thumb.suffix == ".png"
    with Image.open(thumb) as image:
        assert max(image.size) <= config.THUMBNAIL_PX  # actually downscaled

    # A second call reuses the cache rather than regenerating.
    mtime = thumb.stat().st_mtime_ns
    assert ats.thumbnail_file(session, att.id) == thumb
    assert thumb.stat().st_mtime_ns == mtime


def test_thumbnail_of_a_non_image_returns_the_original(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    component = _component(session)
    att = _attach(
        session, "component", component.id, filename="ds.pdf", data=b"%PDF-1.4"
    )
    assert ats.thumbnail_file(session, att.id) == ats.stored_file_path(att)


def test_thumbnail_of_a_corrupt_image_falls_back_to_the_original(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    component = _component(session)
    # An image extension but non-image bytes: Pillow can't decode it.
    att = _attach(
        session, "component", component.id, filename="broken.png", data=b"not an image"
    )
    assert ats.thumbnail_file(session, att.id) == ats.stored_file_path(att)


def test_thumbnail_skips_an_oversized_image(
    session: Session, store, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    # Cap the source pixels tiny; the real 400x300 PNG exceeds it.
    monkeypatch.setattr(ats, "_MAX_SOURCE_PIXELS", 4)
    component = _component(session)
    att = _image(session, component.id)
    assert ats.thumbnail_file(session, att.id) == ats.stored_file_path(att)
    assert not ats._thumbnail_path(att).exists()  # nothing was decoded/written


def test_thumbnail_survives_a_decompression_bomb_error(
    session: Session, store, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from PIL import Image as PILImage

    def boom(*_a: object, **_k: object) -> object:
        raise PILImage.DecompressionBombError("bomb")

    component = _component(session)
    att = _image(session, component.id)
    monkeypatch.setattr(ats.Image, "open", boom)
    # DecompressionBombError isn't an OSError — the broad catch still degrades.
    assert ats.thumbnail_file(session, att.id) == ats.stored_file_path(att)


def test_delete_removes_the_thumbnail_cache(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    component = _component(session)
    att = _image(session, component.id)
    thumb = ats.thumbnail_file(session, att.id)
    assert thumb.is_file()

    ats.delete_attachment(session, att.id)
    assert not thumb.is_file()


def test_delete_attachments_for_removes_only_that_entity(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    component = _component(session)
    invoice = _invoice(session)
    on_component = _image(session, component.id)
    file_path = ats.stored_file_path(on_component)
    thumb = ats.thumbnail_file(session, on_component.id)
    _attach(session, "invoice", invoice.id, kind=AttachmentKind.INVOICE_PDF)

    ats.delete_attachments_for(
        session, entity_type="component", entity_id=component.id
    )
    session.commit()

    assert ats.list_attachments(
        session, entity_type="component", entity_id=component.id
    ) == []
    assert not file_path.exists()
    assert not thumb.exists()
    # The invoice's attachment is untouched.
    assert len(
        ats.list_attachments(session, entity_type="invoice", entity_id=invoice.id)
    ) == 1


def test_hard_delete_component_removes_its_attachments(
    session: Session, store
) -> None:  # type: ignore[no-untyped-def]
    component = _component(session)
    att = _image(session, component.id)
    file_path = ats.stored_file_path(att)
    thumb = ats.thumbnail_file(session, att.id)
    assert file_path.exists() and thumb.exists()

    cs.hard_delete_component(session, component.id)

    with pytest.raises(NotFoundError):
        ats.get_attachment(session, att.id)
    assert not file_path.exists()
    assert not thumb.exists()
