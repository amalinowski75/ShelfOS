"""Server-rendered web UI routes (spec §11-12, §14-15).

The pages use Pico.css for styling, Tabulator for the component table, and small
vanilla-JS helpers for the stock dialogs. Data-mutating actions reuse the JSON
API (``/api/stock/*``) via ``fetch`` from the browser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app.api.deps import get_session
from app.auth.deps import get_optional_user
from app.models.component import ComponentType
from app.models.user import User
from app.services import component_service as cs
from app.services import invoice_service as inv
from app.services import location_service as ls
from app.services import stock_service as ss
from app.services import user_service as us
from app.services._common import require_entity
from app.web.presenter import build_component_table, format_parameter_value

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter(tags=["web"])


def require_web_user(request: Request, session: Session = Depends(get_session)) -> User:
    """Return the logged-in user or redirect to the login page (session, D11)."""
    user = get_optional_user(request, session)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"}
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
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "types": cs.list_types(session),
            "locations": ls.list_all(session),
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
            "invoice_number": invoice.invoice_number,
            "supplier": invoice.supplier,
            "date": invoice.invoice_date.isoformat(),
            "quantity": line.quantity,
            "unit_price": str(line.unit_price),
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
            "current_user": user,
        },
    )
