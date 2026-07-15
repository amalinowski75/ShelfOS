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
