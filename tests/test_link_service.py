"""Service-level tests for external links."""

from __future__ import annotations

import pytest
from app.models.enums import LinkKind
from app.services import component_service as cs
from app.services import link_service as ls
from app.services.errors import NotFoundError, ValidationError
from sqlmodel import Session


def _component(session: Session) -> object:
    ctype = cs.create_type(session, "resistor")
    return cs.create_component(session, ctype.id)


def test_create_and_list_round_trip(session: Session) -> None:
    component = _component(session)
    ls.create_link(
        session,
        entity_type="component",
        entity_id=component.id,
        kind=LinkKind.SHOP,
        url="https://www.tme.eu/pl/details/x/y/",
        label="TME page",
        notes="imported from here",
    )
    ls.create_link(
        session,
        entity_type="component",
        entity_id=component.id,
        kind=LinkKind.DATASHEET,
        url="https://example.com/ds.pdf",
    )
    links = ls.list_links(session, entity_type="component", entity_id=component.id)
    assert [link.kind for link in links] == [LinkKind.SHOP, LinkKind.DATASHEET]
    assert links[0].label == "TME page"
    assert links[1].label is None  # no label given → stored as None


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<b>x</b>",
        "file:///etc/passwd",
        "ftp://host/x",
        "notaurl",
        "http://",  # scheme but no host
        "   ",
        "\x01http://example.com",  # a leading control char urlsplit would strip
        "http://exam\x00ple.com",  # an embedded NUL
    ],
)
def test_create_rejects_a_non_web_url(session: Session, url: str) -> None:
    component = _component(session)
    with pytest.raises(ValidationError):
        ls.create_link(
            session,
            entity_type="component",
            entity_id=component.id,
            kind=LinkKind.OTHER,
            url=url,
        )


def test_create_rejects_an_over_long_url(session: Session) -> None:
    component = _component(session)
    with pytest.raises(ValidationError):
        ls.create_link(
            session,
            entity_type="component",
            entity_id=component.id,
            kind=LinkKind.OTHER,
            url="https://x.io/" + "a" * 2048,
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/x",
        "https://www.tme.eu/pl/details/a/b/",
        "  https://x.io  ",  # surrounding whitespace is trimmed
    ],
)
def test_create_accepts_and_trims_web_urls(session: Session, url: str) -> None:
    component = _component(session)
    link = ls.create_link(
        session,
        entity_type="component",
        entity_id=component.id,
        kind=LinkKind.OTHER,
        url=url,
    )
    assert link.url == url.strip()


def test_create_rejects_an_unknown_entity_type(session: Session) -> None:
    with pytest.raises(ValidationError):
        ls.create_link(
            session,
            entity_type="widget",
            entity_id=1,
            kind=LinkKind.OTHER,
            url="https://x.io",
        )


def test_create_rejects_an_unknown_entity_id(session: Session) -> None:
    with pytest.raises(NotFoundError):
        ls.create_link(
            session,
            entity_type="component",
            entity_id=9999,
            kind=LinkKind.OTHER,
            url="https://x.io",
        )


def test_delete_removes_a_single_link(session: Session) -> None:
    component = _component(session)
    link = ls.create_link(
        session,
        entity_type="component",
        entity_id=component.id,
        kind=LinkKind.OTHER,
        url="https://x.io",
    )
    ls.delete_link(session, link.id)
    assert ls.list_links(session, entity_type="component", entity_id=component.id) == []
    with pytest.raises(NotFoundError):
        ls.get_link(session, link.id)


def test_update_replaces_every_editable_field(session: Session) -> None:
    component = _component(session)
    link = ls.create_link(
        session,
        entity_type="component",
        entity_id=component.id,
        kind=LinkKind.SHOP,
        url="https://old.example/x",
        label="Old",
        notes="old notes",
    )
    updated = ls.update_link(
        session,
        link.id,
        kind=LinkKind.DATASHEET,
        url="https://new.example/y",
        label="New",
        notes="new notes",
    )
    assert updated.id == link.id  # same row, not a new one
    assert updated.kind == LinkKind.DATASHEET
    assert updated.url == "https://new.example/y"
    assert updated.label == "New"
    assert updated.notes == "new notes"


def test_update_is_a_full_replace_blank_clears_label_and_notes(
    session: Session,
) -> None:
    component = _component(session)
    link = ls.create_link(
        session,
        entity_type="component",
        entity_id=component.id,
        kind=LinkKind.SHOP,
        url="https://x.io",
        label="Kept",
        notes="Kept too",
    )
    updated = ls.update_link(
        session,
        link.id,
        kind=LinkKind.SHOP,
        url="https://x.io",
        label="   ",  # blank → cleared, not left as-is
        notes=None,
    )
    assert updated.label is None
    assert updated.notes is None


def test_update_rejects_a_non_web_url(session: Session) -> None:
    component = _component(session)
    link = ls.create_link(
        session,
        entity_type="component",
        entity_id=component.id,
        kind=LinkKind.OTHER,
        url="https://ok.example",
    )
    with pytest.raises(ValidationError):
        ls.update_link(
            session, link.id, kind=LinkKind.OTHER, url="javascript:alert(1)"
        )


def test_update_of_a_missing_link_is_not_found(session: Session) -> None:
    with pytest.raises(NotFoundError):
        ls.update_link(session, 9999, kind=LinkKind.OTHER, url="https://x.io")


def test_delete_links_for_clears_every_row(session: Session) -> None:
    component = _component(session)
    for url in ("https://a.io", "https://b.io"):
        ls.create_link(
            session,
            entity_type="component",
            entity_id=component.id,
            kind=LinkKind.OTHER,
            url=url,
        )
    ls.delete_links_for(session, entity_type="component", entity_id=component.id)
    session.commit()  # delete_links_for runs in the caller's transaction
    assert ls.list_links(session, entity_type="component", entity_id=component.id) == []
