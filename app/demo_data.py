"""Sample data generator for demos and manual UI exploration.

Populates the database with a few dozen **fictional** components (made-up
manufacturers and part numbers), a location hierarchy, stock, and a couple of
finalized invoices — enough to see the UI populated. Not real catalog data.

Use via ``scripts/seed_demo.py`` or call :func:`populate_demo` directly in a
session. Everything goes through the service layer, so all invariants (stock
cache, EAV validation, invoice finalization) hold.
"""

from __future__ import annotations

import random
from datetime import date

from sqlmodel import Session

from app.models.component import ComponentType
from app.models.enums import (
    ContainerType,
    LocationType,
    MountingType,
    ParameterDataType,
    StockReason,
)
from app.seed import ensure_system_user
from app.services import bom_service as bs
from app.services import component_service as cs
from app.services import invoice_service as inv
from app.services import location_service as ls
from app.services import stock_service as ss
from app.units import parse_engineering

_MANUFACTURERS = ["Acme", "Contoso", "Globex", "Initech", "Umbrella", "Stark"]
_QUANTITIES = [5, 10, 25, 50, 100, 250, 500, 1000]


def populate_demo(session: Session, *, seed: int = 1) -> dict[str, int]:
    """Insert demo data and return a summary of how much was created."""
    rng = random.Random(seed)
    user = ensure_system_user(session)
    assert user.id is not None

    drawers = _build_locations(session)
    types = _build_types(session)
    components = _build_components(session, types, rng)
    movements = _stock_components(session, components, drawers, user.id, rng)
    invoices = _build_invoices(session, components, drawers, user.id, rng)
    _build_demo_bom(session, user.id)

    return {
        "types": len(cs.list_types(session)),
        "locations": len(ls.list_all(session)),
        "components": len(components),
        "movements": movements,
        "invoices": invoices,
        "boms": len(bs.list_boms(session)),
    }


def _build_locations(session: Session) -> list[int]:
    """Create a room → rack → shelf → drawers hierarchy; return drawer ids."""
    lab = ls.create_location(session, type=LocationType.ROOM, name="Lab")
    rack = ls.create_location(
        session, type=LocationType.RACK, name="Rack A", parent_id=lab.id
    )
    shelf = ls.create_location(
        session, type=LocationType.SHELF, name="Shelf 1", parent_id=rack.id
    )
    drawers = [
        ls.create_location(
            session, type=LocationType.DRAWER, name=f"Drawer {i}", parent_id=shelf.id
        )
        for i in range(1, 7)
    ]
    return [d.id for d in drawers if d.id is not None]


def _add_param(
    session: Session,
    type_id: int,
    name: str,
    label: str,
    data_type: ParameterDataType,
    *,
    unit: str | None = None,
    enum_values: list[str] | None = None,
    sort_order: int = 0,
) -> int:
    definition = cs.add_parameter_definition(
        session,
        type_id,
        name=name,
        label=label,
        data_type=data_type,
        unit=unit,
        is_filterable=True,
        is_table_column=True,
        sort_order=sort_order,
        enum_values=enum_values,
    )
    assert definition.id is not None
    return definition.id


def _tid(ctype: ComponentType) -> int:
    """Return a freshly-created type's id (always set after persistence)."""
    assert ctype.id is not None
    return ctype.id


def _build_types(session: Session) -> dict[str, dict[str, int]]:
    """Create the type hierarchy and parameters; return ids keyed by name."""
    num = ParameterDataType.NUMBER
    types: dict[str, dict[str, int]] = {}

    resistor_id = _tid(cs.create_type(session, "resistor"))
    types["resistor"] = {
        "id": resistor_id,
        "resistance": _add_param(
            session, resistor_id, "resistance", "Resistance", num, unit="ohm"
        ),
        "tolerance": _add_param(
            session, resistor_id, "tolerance", "Tolerance", num, unit="%", sort_order=1
        ),
        "power": _add_param(
            session, resistor_id, "power", "Power", num, unit="W", sort_order=2
        ),
    }

    capacitor_id = _tid(cs.create_type(session, "capacitor"))
    types["capacitor"] = {
        "id": capacitor_id,
        "capacitance": _add_param(
            session, capacitor_id, "capacitance", "Capacitance", num, unit="F"
        ),
        "voltage_rating": _add_param(
            session,
            capacitor_id,
            "voltage_rating",
            "Voltage",
            num,
            unit="V",
            sort_order=1,
        ),
        "dielectric": _add_param(
            session,
            capacitor_id,
            "dielectric",
            "Dielectric",
            ParameterDataType.ENUM,
            enum_values=["C0G", "X7R", "Y5V"],
            sort_order=2,
        ),
    }

    # transistor -> mosfet (parameter inheritance, decision D3).
    transistor_id = _tid(cs.create_type(session, "transistor"))
    mosfet_id = _tid(cs.create_type(session, "mosfet", parent_id=transistor_id))
    types["mosfet"] = {
        "id": mosfet_id,
        "vds_max": _add_param(session, mosfet_id, "vds_max", "Vds max", num, unit="V"),
        "id_max": _add_param(
            session, mosfet_id, "id_max", "Id max", num, unit="A", sort_order=1
        ),
        "rds_on": _add_param(
            session, mosfet_id, "rds_on", "Rds(on)", num, unit="ohm", sort_order=2
        ),
    }

    # diode -> led.
    diode_id = _tid(cs.create_type(session, "diode"))
    types["diode"] = {"id": diode_id}
    led_id = _tid(cs.create_type(session, "led", parent_id=diode_id))
    types["led"] = {
        "id": led_id,
        "color": _add_param(
            session,
            led_id,
            "color",
            "Color",
            ParameterDataType.ENUM,
            enum_values=["red", "green", "blue", "yellow", "white"],
        ),
        "forward_voltage": _add_param(
            session, led_id, "forward_voltage", "Vf", num, unit="V", sort_order=1
        ),
    }

    connector_id = _tid(cs.create_type(session, "connector"))
    types["connector"] = {
        "id": connector_id,
        "pitch": _add_param(session, connector_id, "pitch", "Pitch", num, unit="m"),
        "positions": _add_param(
            session, connector_id, "positions", "Positions", num, sort_order=1
        ),
    }

    types["ic"] = {"id": _tid(cs.create_type(session, "ic"))}
    return types


def _make(
    session: Session,
    type_id: int,
    *,
    package: str,
    mounting: MountingType,
    rng: random.Random,
    params: dict[int, float | str],
) -> int:
    """Create one component with a fake MPN and set its parameter values."""
    manufacturer = rng.choice(_MANUFACTURERS)
    mpn = f"{manufacturer[:3].upper()}-{rng.randint(1000, 9999)}"
    component = cs.create_component(
        session,
        type_id,
        manufacturer=manufacturer,
        mpn=mpn,
        package=package,
        mounting_type=mounting,
    )
    assert component.id is not None
    for definition_id, value in params.items():
        cs.set_parameter_value(session, component.id, definition_id, value)
    return component.id


def _build_components(
    session: Session,
    types: dict[str, dict[str, int]],
    rng: random.Random,
) -> list[int]:
    """Create ~50 fictional components across all types."""
    ids: list[int] = []
    smt_packages = ["0402", "0603", "0805", "1206"]

    r = types["resistor"]
    for value in [
        "10R",
        "47R",
        "100R",
        "220R",
        "1k",
        "2k2",
        "4k7",
        "10k",
        "47k",
        "100k",
        "1M",
        "2M2",
    ]:
        ids.append(
            _make(
                session,
                r["id"],
                package=rng.choice(smt_packages),
                mounting=MountingType.SMT,
                rng=rng,
                params={
                    r["resistance"]: parse_engineering(value),
                    r["tolerance"]: float(rng.choice([1, 5])),
                    r["power"]: rng.choice([0.063, 0.1, 0.125, 0.25]),
                },
            )
        )

    c = types["capacitor"]
    for value in ["22pF", "100pF", "1nF", "10nF", "100nF", "1uF", "10uF", "47uF"]:
        ids.append(
            _make(
                session,
                c["id"],
                package=rng.choice(smt_packages),
                mounting=MountingType.SMT,
                rng=rng,
                params={
                    c["capacitance"]: parse_engineering(value),
                    c["voltage_rating"]: float(rng.choice([6, 16, 25, 50, 100])),
                    c["dielectric"]: rng.choice(["C0G", "X7R", "Y5V"]),
                },
            )
        )

    m = types["mosfet"]
    for _ in range(6):
        ids.append(
            _make(
                session,
                m["id"],
                package=rng.choice(["SOT-23", "SOT-223", "DPAK"]),
                mounting=MountingType.SMT,
                rng=rng,
                params={
                    m["vds_max"]: float(rng.choice([20, 30, 60, 100])),
                    m["id_max"]: rng.choice([1.5, 3.0, 5.0, 8.0]),
                    m["rds_on"]: rng.choice([0.005, 0.01, 0.05, 0.1]),
                },
            )
        )

    led = types["led"]
    for _ in range(6):
        ids.append(
            _make(
                session,
                led["id"],
                package=rng.choice(["0603", "0805", "3mm", "5mm"]),
                mounting=rng.choice([MountingType.SMT, MountingType.THT]),
                rng=rng,
                params={
                    led["color"]: rng.choice(
                        ["red", "green", "blue", "yellow", "white"]
                    ),
                    led["forward_voltage"]: rng.choice([1.8, 2.0, 2.1, 3.2]),
                },
            )
        )

    diode = types["diode"]
    for _ in range(4):
        ids.append(
            _make(
                session,
                diode["id"],
                package=rng.choice(["SOD-123", "SMA", "DO-41"]),
                mounting=rng.choice([MountingType.SMT, MountingType.THT]),
                rng=rng,
                params={},
            )
        )

    conn = types["connector"]
    for positions in [2, 4, 6, 8, 10]:
        ids.append(
            _make(
                session,
                conn["id"],
                package="header",
                mounting=MountingType.THT,
                rng=rng,
                params={
                    conn["pitch"]: parse_engineering("2.54m") / 1000,  # 2.54 mm
                    conn["positions"]: float(positions),
                },
            )
        )

    ic = types["ic"]
    for _ in range(6):
        ids.append(
            _make(
                session,
                ic["id"],
                package=rng.choice(["SOIC-8", "TSSOP-20", "QFN-32", "LQFP-48"]),
                mounting=MountingType.SMT,
                rng=rng,
                params={},
            )
        )

    return ids


def _stock_components(
    session: Session,
    components: list[int],
    drawers: list[int],
    user_id: int,
    rng: random.Random,
) -> int:
    """Add stock for most components into random drawers; return movement count."""
    movements = 0
    for component_id in components:
        # Leave a few components out of stock to exercise the empty case.
        if rng.random() < 0.1:
            continue
        for _ in range(rng.randint(1, 2)):
            ss.add_stock(
                session,
                component_id=component_id,
                location_id=rng.choice(drawers),
                quantity=rng.choice(_QUANTITIES),
                user_id=user_id,
                reason=StockReason.CORRECTION,
                container_type=rng.choice(list(ContainerType)),
                note="demo initial stock",
            )
            movements += 1
    return movements


def _build_invoices(
    session: Session,
    components: list[int],
    drawers: list[int],
    user_id: int,
    rng: random.Random,
) -> int:
    """Create two finalized invoices to populate purchase history (§12)."""
    suppliers = ["Parts Depot", "MakerSupply"]
    for index, supplier in enumerate(suppliers):
        invoice = inv.create_invoice(
            session,
            supplier=supplier,
            invoice_number=f"DEMO-{index + 1:03d}",
            invoice_date=date(2026, 6, 1 + index),
            currency="EUR",
        )
        assert invoice.id is not None
        for component_id in rng.sample(components, k=5):
            inv.add_line(
                session,
                invoice.id,
                component_id=component_id,
                quantity=rng.choice([100, 250, 500]),
                unit_price=_price(rng),
                location_id=rng.choice(drawers),
            )
        inv.finalize_invoice(session, invoice.id, user_id=user_id)
    return len(suppliers)


def _price(rng: random.Random):  # type: ignore[no-untyped-def]
    from decimal import Decimal

    cents = rng.randint(1, 500)
    return Decimal(cents) / Decimal(100)


def _build_demo_bom(session: Session, user_id: int) -> None:
    """A small demo BOM whose values match seeded stock, so its report shows
    substitute suggestions (the R/C lines carry no MPN)."""
    csv_text = (
        "Reference,Qty,Value,Footprint,MPN\n"
        '"R1,R2,R3",3,10k 1%,Resistor_SMD:R_0402_1005Metric,\n'
        '"R4,R5",2,4k7 1%,Resistor_SMD:R_0402_1005Metric,\n'
        '"C1,C2,C3",3,100nF/50V,Capacitor_SMD:C_0402_1005Metric,\n'
        '"C4,C5",2,10uF/25V,Capacitor_SMD:C_0805_2012Metric,\n'
        "U1,1,STM32F103C8T6,Package_QFP:LQFP-48,STM32F103C8T6\n"
    )
    bs.create_bom(
        session,
        name="Demo board",
        filename="demo_board.csv",
        data=csv_text.encode(),
        user_id=user_id,
    )
