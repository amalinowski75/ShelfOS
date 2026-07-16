"""Server-rendered web UI routes (spec §11-12, §14-15).

The pages use Pico.css for styling, Tabulator for the component table, and small
vanilla-JS helpers for the stock dialogs. Data-mutating actions reuse the JSON
API (``/api/stock/*``) via ``fetch`` from the browser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app.api.deps import get_session
from app.auth.deps import get_optional_user, issue_csrf_token
from app.models.component import ComponentType
from app.models.enums import (
    AttachmentKind,
    LocationType,
    MountingType,
    ParameterDataType,
    UserRole,
)
from app.models.user import User
from app.services import attachment_service as ats
from app.services import bom_service as boms_svc
from app.services import component_service as cs
from app.services import invoice_service as inv
from app.services import location_service as ls
from app.services import stock_service as ss
from app.services import user_service as us
from app.services._common import require_entity
from app.web.presenter import (
    build_component_table,
    build_invoice_table,
    format_money,
    format_parameter_value,
)

# Cap the invoice list until real pagination lands; the template shows a hint
# when the cap is hit so older invoices are not dropped silently.
_INVOICE_LIST_LIMIT = 200

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

router = APIRouter(tags=["web"])


def _location_options(tree: list[ls.LocationNode]) -> list[dict[str, Any]]:
    """Flat, path-sorted location options for a parent/location ``<select>``."""
    options = [
        {"id": node.location.id, "path": node.path}
        for node in ls.flatten_tree(tree)
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
    """Live availability report for one BOM (§21). 404 if it doesn't exist."""
    report = boms_svc.build_bom_report(session, bom_id)  # raises NotFound → 404
    # The original CSV is the only thing attached to a bom, added first at import,
    # so the oldest-first list's first entry is it (None if it was removed).
    csv_attachments = ats.list_attachments(
        session, entity_type="bom", entity_id=bom_id
    )
    return templates.TemplateResponse(
        request,
        "bom_report.html",
        {
            "report": report,
            "csv_attachment": csv_attachments[0] if csv_attachments else None,
            "current_user": user,
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
    return templates.TemplateResponse(
        request,
        "invoice_detail.html",
        {
            "invoice": invoice,
            "lines": lines,
            # The line dialog (location tree-picker + "New component") only renders
            # for a writer on a draft, so only fetch its data then.
            "location_tree": tree,
            # For the "New location" dialog reachable inline from the line picker.
            "location_types": [lt.value for lt in LocationType] if can_edit else [],
            "location_options": _location_options(tree) if can_edit else [],
            "types": cs.list_types(session) if can_edit else [],
            "mounting_types": [mt.value for mt in MountingType],
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
            "all_locations": ls.list_all(session),
            "attachment_kinds": [k.value for k in AttachmentKind],
            "current_user": user,
        },
    )
