"""Server-rendered web UI routes (spec §11-12, §14-15).

The pages use Pico.css for styling, Tabulator for the component table, and small
vanilla-JS helpers for the stock dialogs. Data-mutating actions reuse the JSON
API (``/api/stock/*``) via ``fetch`` from the browser.
"""

from __future__ import annotations

import math
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app.api.deps import get_session
from app.auth.deps import get_optional_user, issue_csrf_token
from app.models.component import ComponentType, ParameterDefinition
from app.models.enums import (
    AttachmentKind,
    LinkKind,
    LocationType,
    MatchDomain,
    MountingType,
    ParameterDataType,
    UserRole,
)
from app.models.user import User
from app.services import attachment_service as ats
from app.services import bom_service as boms_svc
from app.services import component_service as cs
from app.services import invoice_import_service as imp
from app.services import invoice_service as inv
from app.services import label_service as lbl
from app.services import location_service as ls
from app.services import match_rule_service as mrs
from app.services import shops
from app.services import stock_service as ss
from app.services import user_service as us
from app.services._common import require_entity
from app.web.presenter import (
    build_component_table,
    build_invoice_table,
    build_location_stock,
    format_money,
    format_parameter_value,
)

# Cap the invoice list until real pagination lands; the template shows a hint
# when the cap is hit so older invoices are not dropped silently.
_INVOICE_LIST_LIMIT = 200

# Most parts listed inside ONE location on the Locations page. The whole tree is
# rendered up front (collapsed, but present), so without this a single location
# holding thousands of distinct parts would weigh down every visit. The template
# says how many it left out rather than truncating silently.
#
# It bounds one location, not the page: nothing caps the number of locations, and
# build_location_stock loads every stocked slot regardless. See its docstring.
_PARTS_PER_LOCATION = 50

# SQLite's signed 64-bit rowid ceiling; anything above overflows the driver.
_MAX_ROWID = 2**63 - 1

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _static_version() -> str:
    """Cache-busting token: the newest static-file mtime.

    Exposed as a Jinja global so templates can append ``?v={{ static_version() }}``
    to asset links. The token changes whenever a CSS/JS file is edited, so
    browsers fetch the new file without a manual hard refresh.
    """
    files = (f for f in _STATIC_DIR.glob("*") if f.is_file())
    newest = max((f.stat().st_mtime for f in files), default=0.0)
    return str(int(newest))


templates.env.globals["static_version"] = _static_version
templates.env.globals["format_money"] = format_money
# Build a distributor product-page link from an invoice line's shop + part number,
# so the review row can hand it to the New Component dialog's "open in shop" button.
templates.env.globals["shop_product_url"] = shops.product_url

router = APIRouter(tags=["web"])


def _location_options(tree: list[ls.LocationNode]) -> list[dict[str, Any]]:
    """Flat, path-sorted location options for a parent/location ``<select>``."""
    options = [
        {"id": node.location.id, "path": node.path} for node in ls.flatten_tree(tree)
    ]
    options.sort(key=lambda option: str(option["path"]).lower())
    return options


def require_web_user(request: Request, session: Session = Depends(get_session)) -> User:
    """Return the logged-in user or redirect to the login page (session, D11).

    Also heals a session that carries a user but no CSRF token: the signing
    secret is stable across restarts, so a cookie minted before CSRF existed (or
    by an older build) keeps authenticating yet leaves the ``<meta csrf-token>``
    empty, which makes every browser write fail the CSRF check. Issuing the
    token here on render keeps such sessions able to write without a re-login.
    """
    user = get_optional_user(request, session)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"}
        )
    if not request.session.get("csrf_token"):
        issue_csrf_token(request)
    return user


def require_web_admin(
    request: Request, session: Session = Depends(get_session)
) -> User:
    """Return the logged-in admin; redirect anon to login and non-admins home."""
    user = require_web_user(request, session)
    if user.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/"}
        )
    return user


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {})


@router.post("/login", response_model=None)
def login_submit(
    request: Request,
    username: str = Form(),
    password: str = Form(),
    session: Session = Depends(get_session),
) -> HTMLResponse | RedirectResponse:
    user = us.authenticate(session, username, password)
    if user is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    request.session["user_id"] = user.id
    issue_csrf_token(request)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_web_user),
) -> HTMLResponse:
    """Main page: component table with a type filter (spec §11)."""
    tree = ls.location_tree(session)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "types": cs.list_types(session),
            "location_tree": tree,
            # For the "New location" dialog reachable inline from the stock picker.
            "location_types": [lt.value for lt in LocationType],
            "location_options": _location_options(tree),
            "data_types": [dt.value for dt in ParameterDataType],
            "mounting_types": [mt.value for mt in MountingType],
            "current_user": user,
        },
    )


@router.get("/web/api/components")
def components_feed(
    type_id: int | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(require_web_user),
) -> dict[str, Any]:
    """JSON feed for the Tabulator component table."""
    return build_component_table(session, type_id)


@router.get("/web/api/components/{component_id}/location-usage")
def component_location_usage(
    component_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_web_user),
) -> dict[str, list[int]]:
    """Which locations hold this component, and which hold anything at all.

    Drives the stock dialog's location filter, which depends on both the mode and
    the component and so can't be baked into the server-rendered picker: **Take**
    offers only ``holding``; **Add** offers everything outside ``occupied``, plus
    ``holding`` so a restock can go back into the drawer the part already lives in.

    Advisory, not a constraint — the dialog offers "show all locations", and the
    write endpoints accept any location as they always have.
    """
    from app.models.component import Component

    require_entity(session, Component, component_id, "component")
    return {
        "holding": [
            cl.location_id for cl in ss.list_component_locations(session, component_id)
        ],
        "occupied": ss.occupied_location_ids(session),
    }


@router.get("/locations", response_class=HTMLResponse)
def locations_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_web_user),
) -> HTMLResponse:
    """Storage-location tree with a create dialog (spec §7)."""
    tree = ls.location_tree(session)
    return templates.TemplateResponse(
        request,
        "locations.html",
        {
            "location_tree": tree,
            "location_types": [lt.value for lt in LocationType],
            "location_options": _location_options(tree),
            # What each location holds, so the tree can show its contents and mark
            # itself empty or occupied for the page's two filters.
            "location_stock": build_location_stock(session),
            "parts_per_location": _PARTS_PER_LOCATION,
            "current_user": user,
        },
    )


@router.get("/labels/locations", response_class=HTMLResponse)
def location_labels_page(
    request: Request,
    ids: str | None = None,
    root: int | None = None,
    w: float = 57,
    h: float = 32,
    sheet: bool = False,
    session: Session = Depends(get_session),
    user: User = Depends(require_web_user),
) -> HTMLResponse:
    """Print-ready location labels: QR + readable path (spec §7).

    ``ids`` picks explicit locations, ``root`` a whole subtree, neither prints
    everything. ``w``/``h`` are the label size in mm — adjust to the stock the
    label printer holds; the page's ``@page`` rule makes one label per page.
    ``sheet`` flows the labels onto A4 pages instead, for an ordinary printer.
    """
    id_list: list[int] | None = None
    if ids:
        try:
            id_list = [int(part) for part in ids.split(",") if part.strip()]
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="ids must be a comma-separated list of numbers",
            ) from exc
    # Python ints parse at any size, but past SQLite's 64-bit rowid range the
    # driver raises OverflowError mid-query — an unmapped 500. Refuse up front.
    for value in [*(id_list or []), *([root] if root is not None else [])]:
        if not 0 < value <= _MAX_ROWID:
            raise HTTPException(
                status_code=422,
                detail="location ids must be positive 64-bit integers",
            )
    labels = lbl.build_labels(session, ids=id_list, root=root)
    # Clamped so a typo'd size can't produce absurd page CSS. NaN slides
    # through min/max (every comparison is False), so non-finite → default.
    if not math.isfinite(w):
        w = 57.0
    if not math.isfinite(h):
        h = 32.0
    return templates.TemplateResponse(
        request,
        "labels.html",
        {
            "labels": labels,
            "w": min(max(w, 20.0), 200.0),
            "h": min(max(h, 10.0), 200.0),
            "sheet": sheet,
            "current_user": user,
        },
    )


@router.get("/web/api/invoices")
def invoices_feed(
    session: Session = Depends(get_session),
    user: User = Depends(require_web_user),
) -> dict[str, Any]:
    """JSON feed for the Tabulator invoice list (newest first, §16)."""
    return build_invoice_table(session, _INVOICE_LIST_LIMIT)


@router.get("/invoices", response_class=HTMLResponse)
def invoices_list(
    request: Request,
    user: User = Depends(require_web_user),
) -> HTMLResponse:
    """Invoice list shell; rows load from /web/api/invoices (spec §16)."""
    return templates.TemplateResponse(
        request, "invoices_list.html", {"current_user": user}
    )


@router.get("/web/api/users")
def users_feed(
    response: Response,
    session: Session = Depends(get_session),
    user: User = Depends(require_web_admin),
) -> dict[str, Any]:
    """JSON feed for the Tabulator user table (admin only, §18)."""
    # Account listings are sensitive; keep them out of shared/proxy caches and
    # the back/forward cache so they don't linger after logout.
    response.headers["Cache-Control"] = "no-store"
    return {
        "data": [
            {
                "id": account.id,
                "name": account.name,
                "role": account.role.value,
                "is_active": account.is_active,
            }
            for account in us.list_users(session)
        ]
    }


@router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    user: User = Depends(require_web_admin),
) -> HTMLResponse:
    """User-account management shell; rows load from /web/api/users (admin, §18)."""
    return templates.TemplateResponse(
        request,
        "users.html",
        {"current_user": user, "roles": [role.value for role in UserRole]},
    )


@router.get("/web/api/match-rules")
def match_rules_feed(
    session: Session = Depends(get_session),
    user: User = Depends(require_web_admin),
) -> dict[str, Any]:
    """JSON feed for the matching-rules table (admin only).

    A scoped rule (param_name/enum_value) is attached to a parameter definition; the
    table shows that as "Type / Parameter" so the admin knows what it applies to.
    """
    rules = mrs.list_rules(session)
    # Resolve each scoped rule's parameter to a readable "Type / Label" once.
    scope_labels: dict[int, str] = {}
    for rule in rules:
        pid = rule.parameter_definition_id
        if pid is None or pid in scope_labels:
            continue
        definition = session.get(ParameterDefinition, pid)
        if definition is None:
            scope_labels[pid] = "(deleted parameter)"
            continue
        owner = session.get(ComponentType, definition.type_id)
        prefix = f"{owner.name} / " if owner is not None else ""
        scope_labels[pid] = f"{prefix}{definition.label}"
    # An enum_value rule's target must be one of its parameter's allowed values, so
    # ship that list with the row — the inline Target editor offers exactly it (a
    # free-typed target for this domain can never fire). One batched query.
    enum_pids = [
        rule.parameter_definition_id
        for rule in rules
        if rule.domain is MatchDomain.ENUM_VALUE
        and rule.parameter_definition_id is not None
    ]
    enum_values = cs.enum_values_by_definition(session, enum_pids)
    return {
        "data": [
            {
                "id": rule.id,
                "domain": rule.domain.value,
                "alias": rule.alias,
                "canonical": rule.canonical,
                "parameter_definition_id": rule.parameter_definition_id,
                "parameter": (
                    scope_labels.get(rule.parameter_definition_id)
                    if rule.parameter_definition_id is not None
                    else None
                ),
                "enum_values": (
                    enum_values.get(rule.parameter_definition_id, [])
                    if rule.domain is MatchDomain.ENUM_VALUE
                    and rule.parameter_definition_id is not None
                    else []
                ),
                "sort_order": rule.sort_order,
            }
            for rule in rules
        ]
    }


@router.get("/match-rules", response_class=HTMLResponse)
def match_rules_page(
    request: Request,
    user: User = Depends(require_web_admin),
) -> HTMLResponse:
    """Matching-rules shell; rows load from /web/api/match-rules (admin only)."""
    return templates.TemplateResponse(
        request,
        "match_rules.html",
        {
            "current_user": user,
            "domains": [d.value for d in MatchDomain],
            "mounting_types": [m.value for m in MountingType],
        },
    )


@router.get("/boms", response_class=HTMLResponse)
def boms_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_web_user),
) -> HTMLResponse:
    """Saved BOMs + an upload control (writer); rows are server-rendered (§21)."""
    return templates.TemplateResponse(
        request,
        "boms_list.html",
        {"boms": boms_svc.list_boms(session), "current_user": user},
    )


@router.get("/boms/{bom_id}", response_class=HTMLResponse)
def bom_report_page(
    bom_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_web_user),
) -> HTMLResponse:
    """Live availability report for one BOM (§21). 404 if it doesn't exist.

    A shell page: the lines + summary are fetched client-side from the report feed
    (``/api/boms/{id}/report``) and rendered by Tabulator, so here we only need the
    BOM header and the stored-CSV link.
    """
    bom = boms_svc.get_bom(session, bom_id)  # raises NotFound → 404
    # The original CSV is the only thing attached to a bom, added first at import,
    # so the oldest-first list's first entry is it (None if it was removed).
    csv_attachments = ats.list_attachments(session, entity_type="bom", entity_id=bom_id)
    return templates.TemplateResponse(
        request,
        "bom_report.html",
        {
            "bom": bom,
            "csv_attachment": csv_attachments[0] if csv_attachments else None,
            "current_user": user,
            # For the shared "New component" dialog (add a missing line to
            # inventory) — only writers get it, so skip the types query otherwise.
            "types": (
                cs.list_types(session) if user.role != UserRole.READ_ONLY else []
            ),
            "mounting_types": [mt.value for mt in MountingType],
        },
    )


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(
    invoice_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_web_user),
) -> HTMLResponse:
    """Invoice header, totals and lines, each line linking to its component (§9)."""
    invoice, pairs = inv.get_invoice_detail(session, invoice_id)

    # Resolve each line's location path once per distinct location so a long
    # invoice does not re-walk the location tree for every repeated slot.
    path_cache: dict[int, str] = {}

    def _location_path(location_id: int | None) -> str:
        if location_id is None:
            return ""
        if location_id not in path_cache:
            path_cache[location_id] = ls.format_path(session, location_id)
        return path_cache[location_id]

    lines = [
        {
            "supplier_part_number": line.supplier_part_number,
            "quantity": line.quantity,
            "unit_price": line.unit_price,
            "total_price": line.total_price,
            "location": _location_path(line.location_id),
            "location_id": line.location_id,
            "line_id": line.id,
            "component_id": line.component_id,
            "component": component,
        }
        for line, component in pairs
    ]

    # The "New component" dialog only renders for a writer on a draft, so only
    # fetch its data then (avoids a types query on read-only/finalized views).
    can_edit = user.role.value != "read-only" and not invoice.is_finalized
    tree = ls.location_tree(session) if can_edit else []
    # Parked PDF-import lines awaiting resolution — only shown on an editable draft.
    pending_import = imp.list_pending(session, invoice_id) if can_edit else []
    # What the staged rows will add to the net once finalized — total_net counts only
    # real lines, so a freshly imported invoice would otherwise read 0.00.
    pending_import_subtotal = sum(
        (line.unit_price * line.quantity for line in pending_import), Decimal(0)
    )
    return templates.TemplateResponse(
        request,
        "invoice_detail.html",
        {
            "invoice": invoice,
            "lines": lines,
            "pending_import_lines": pending_import,
            "pending_import_subtotal": pending_import_subtotal,
            # The line dialog (location tree-picker + "New component") only renders
            # for a writer on a draft, so only fetch its data then.
            "location_tree": tree,
            # For the "New location" dialog reachable inline from the line picker.
            "location_types": [lt.value for lt in LocationType] if can_edit else [],
            "location_options": _location_options(tree) if can_edit else [],
            "types": cs.list_types(session) if can_edit else [],
            "mounting_types": [mt.value for mt in MountingType],
            # For the New Type builder reachable via "+ New type" in the component
            # dialog on this page.
            "data_types": [dt.value for dt in ParameterDataType] if can_edit else [],
            "attachment_kinds": [k.value for k in AttachmentKind],
            "current_user": user,
        },
    )


@router.get("/components/{component_id}", response_class=HTMLResponse)
def component_detail(
    component_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_web_user),
) -> HTMLResponse:
    """Component details: parameters, stock, purchase history (spec §12)."""
    from app.models.component import Component

    component = require_entity(session, Component, component_id, "component")
    ctype = session.get(ComponentType, component.type_id)

    values = {
        p.parameter_definition_id: p
        for p in cs.list_parameter_values(session, component_id)
    }
    parameters = [
        {
            "label": definition.label,
            "value": format_parameter_value(
                definition, values.get(cast(int, definition.id))
            ),
        }
        for definition in cs.get_effective_parameter_definitions(
            session, component.type_id
        )
    ]

    locations = [
        {
            "path": ls.format_path(session, cl.location_id),
            "quantity": cl.quantity,
            "container": cl.container_type.value,
        }
        for cl in ss.list_component_locations(session, component_id)
    ]

    history = [
        {
            "invoice_id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "supplier": invoice.supplier,
            "date": invoice.invoice_date.isoformat(),
            "quantity": line.quantity,
            "unit_price": format_money(line.unit_price),
            "currency": invoice.currency,
        }
        for line, invoice in inv.list_purchase_history(session, component_id)
    ]

    movements = ss.list_movements(session, component_id)
    # Who moved the stock. The ledger stores the id; the page needs the name, and
    # one lookup for the whole table beats a join that would have to be threaded
    # through the service's return type for this one caller.
    movement_authors = us.names_by_id(session, (m.user_id for m in movements))

    # For the Add/Take stock dialog and the "New location" it can reach inline.
    # Only a writer gets that dialog, so only a writer pays for the queries.
    can_write = user.role != UserRole.READ_ONLY
    tree = ls.location_tree(session) if can_write else []

    return templates.TemplateResponse(
        request,
        "component_detail.html",
        {
            "component": component,
            "type_name": ctype.name if ctype else "",
            "parameters": parameters,
            "locations": locations,
            "history": history,
            "movements": movements,
            "movement_authors": movement_authors,
            "location_tree": tree,
            "location_types": [lt.value for lt in LocationType] if can_write else [],
            "location_options": _location_options(tree) if can_write else [],
            "attachment_kinds": [k.value for k in AttachmentKind],
            "link_kinds": [k.value for k in LinkKind],
            "mounting_types": [mt.value for mt in MountingType],
            "current_user": user,
        },
    )
