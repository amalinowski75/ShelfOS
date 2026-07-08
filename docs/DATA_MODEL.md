# ShelfOS — Data Model (v1.0, concrete schema)

Refines spec §4–20 with the decisions from `DECISIONS.md`. Types are indicative
(SQLModel / SQLite initially, PostgreSQL later).

---

## component_types
| field     | type     | notes                                  |
|-----------|----------|----------------------------------------|
| id        | int PK   |                                        |
| name      | str      | unique within parent                   |
| parent_id | int FK?  | → component_types.id (hierarchy)       |

Parameter inheritance: effective set = definitions along the whole path to the
root (see D3).

## parameter_definitions
| field           | type   | notes                                     |
|-----------------|--------|-------------------------------------------|
| id              | int PK |                                           |
| type_id         | int FK | → component_types.id                      |
| name            | str    | technical key (e.g. "resistance")         |
| label           | str    | UI label (e.g. "Resistance")              |
| data_type       | enum   | number / text / bool / enum (D6)          |
| unit            | str?   | base unit (e.g. "ohm")                    |
| is_filterable   | bool   |                                           |
| is_table_column | bool   |                                           |
| sort_order      | int    |                                           |

## parameter_enum_values  (for data_type = enum)
| field                    | type   | notes                        |
|--------------------------|--------|------------------------------|
| id                       | int PK |                              |
| parameter_definition_id  | int FK |                              |
| value                    | str    | allowed value                |
| sort_order               | int    |                              |

## components
| field          | type   | notes                                   |
|----------------|--------|-----------------------------------------|
| id             | int PK |                                         |
| type_id        | int FK | → component_types.id                    |
| manufacturer   | str?   |                                         |
| mpn            | str?   | Manufacturer Part Number                |
| package        | str?   |                                         |
| mounting_type  | enum   | SMT/THT/Panel/Wire/Other                |
| notes          | str?   |                                         |
| status         | enum   | active/archived/obsolete/hidden (D7)    |
| deleted_at     | dt?    | soft delete                             |
| deleted_reason | str?   |                                         |
| deleted_by     | int?   | → users.id                              |

## component_parameters  (EAV)
| field                   | type    | notes                       |
|-------------------------|---------|-----------------------------|
| id                      | int PK  |                             |
| component_id            | int FK  |                             |
| parameter_definition_id | int FK  |                             |
| value_num               | float?  | base unit                   |
| value_text              | str?    | text / enum                 |
| value_bool              | bool?   |                             |

Invariant: exactly one value column filled, matching `data_type`.

## locations
| field     | type   | notes                             |
|-----------|--------|-----------------------------------|
| id        | int PK |                                   |
| parent_id | int FK?| hierarchy                         |
| type      | enum   | room/rack/shelf/…/compartment     |
| name      | str    |                                   |

## component_locations  (stock; quantity = cache, D1)
| field          | type   | notes                       |
|----------------|--------|-----------------------------|
| id             | int PK |                             |
| component_id   | int FK |                             |
| location_id    | int FK |                             |
| quantity       | int    | cache = Σ delta_quantity    |
| container_type | enum   | reel/bag/feeder/loose/box   |
| note           | str?   |                             |

## stock_movements  (source of truth, D1)
| field          | type   | notes                                    |
|----------------|--------|------------------------------------------|
| id             | int PK |                                          |
| component_id   | int FK |                                          |
| location_id    | int FK |                                          |
| delta_quantity | int    | +/- (add/take)                           |
| reason         | enum   | purchase/correction/usage/damaged_lost   |
| note           | str?   | free-text note/reason (spec §14-15)      |
| timestamp      | dt     |                                          |
| user_id        | int FK | → users.id                               |
| invoice_id     | int FK?| set when movement comes from finalization|

## invoices
| field          | type    | notes                          |
|----------------|---------|--------------------------------|
| id             | int PK  |                                |
| supplier       | str     |                                |
| invoice_number | str     |                                |
| invoice_date   | date    |                                |
| currency       | str     | one currency per invoice (D5)  |
| total_net      | Decimal |                                |
| total_gross    | Decimal |                                |
| file_path      | str?    |                                |
| notes          | str?    |                                |
| is_finalized   | bool    | read-only after finalize (§16) |

An invoice must have ≥ 1 line.

## invoice_lines
| field                | type    | notes                     |
|----------------------|---------|---------------------------|
| id                   | int PK  |                           |
| invoice_id           | int FK  |                           |
| component_id         | int FK  |                           |
| supplier_part_number | str?    |                           |
| quantity             | int     |                           |
| unit_price           | Decimal |                           |
| total_price          | Decimal | computed = quantity×unit   |
| location_id          | int FK? | destination stock location (spec §16) |

## attachments
| field       | type   | notes                                        |
|-------------|--------|----------------------------------------------|
| id          | int PK |                                              |
| entity_type | str    | component / invoice / …                      |
| entity_id   | int    |                                              |
| kind        | enum   | photo/datasheet/invoice_pdf/note/other       |
| file_path   | str    | file on disk; DB stores metadata only        |
| filename    | str    |                                              |
| notes       | str?   |                                              |

## users
| field | type   | notes                        |
|-------|--------|------------------------------|
| id    | int PK |                              |
| name  | str    |                              |
| role  | enum   | admin/user/read-only (D2)    |

## audit_log
See D9 (generic change table).

---

## Key relationships / navigation
- component ↔ invoices: via `invoice_lines` (bidirectional, §9).
- component → locations (stock) → quantity.
- component → stock_movements (history).
- component → parameters (effective definitions from the type hierarchy).
