# ShelfOS v1.0
## Architecture and Product Specification

## 1. Overview

ShelfOS is a lightweight electronic component inventory and information management system.

The primary goals are:

- component inventory management,
- parametric component search,
- purchase and invoice tracking,
- location management,
- simple web-based user interface,
- maintainable architecture,
- strong automated test coverage.

ShelfOS is intentionally not an ERP, accounting system, or advanced warehouse management platform.

---

# 2. Technology Stack

## Backend

- Python
- FastAPI
- SQLModel / SQLAlchemy

## Database

Initial:
- SQLite

Future:
- PostgreSQL

## Frontend

- Jinja2
- HTMX
- Vanilla JavaScript
- Tabulator

## Styling

- Pico.css preferred
- Minimal custom CSS

---

# 3. Architectural Principles

## Separation of Concerns

Business logic must not exist in:

- UI code
- FastAPI endpoints
- ORM models

Business logic belongs to service/domain layers.

Examples:

- component_service
- stock_service
- invoice_service
- location_service

## Testability

All business operations must be executable without:

- HTTP
- browser UI

---

# 4. Domain Model

## Components

A component represents a logical electronic part.

Examples:

- 10k 1% 0603 resistor
- 100nF 50V X7R capacitor
- STM32F042F6P6
- AO3400A MOSFET

Common fields:

- type
- manufacturer
- MPN
- package
- mounting_type
- notes

## Mounting Type

Supported values:

- SMT
- THT
- Panel
- Wire
- Other

---

# 5. Component Types

Different component types expose different parameter sets.

Examples:

Resistor:
- resistance
- tolerance
- power
- voltage_rating

Capacitor:
- capacitance
- tolerance
- voltage_rating
- dielectric

MOSFET:
- vds_max
- id_max
- rds_on

LED:
- color
- forward_voltage
- luminous_intensity

Component types may be hierarchical.

Example:

- transistor
  - mosfet
  - bjt
- diode
  - led

---

# 6. Parameter System

Use controlled EAV.

## component_types

- id
- name
- parent_id

## parameter_definitions

- id
- type_id
- name
- label
- data_type
- unit
- is_filterable
- is_table_column
- sort_order

## component_parameters

- component_id
- parameter_definition_id
- value_num
- value_text
- value_bool

Numeric values must be stored in base units.

Examples:

- resistance in ohms
- capacitance in farads
- voltage in volts
- current in amperes

Display formatting may use engineering prefixes.

---

# 7. Locations

Locations are hierarchical.

Structure:

room
→ rack
→ shelf
→ partition
→ drawer
→ compartment

Future types may include:

- feeder
- box

Suggested model:

- id
- parent_id
- type
- name

---

# 8. Stock Storage

A component may exist in multiple locations.

## component_locations

- component_id
- location_id
- quantity
- container_type
- note

Container types:

- reel
- bag
- feeder
- loose
- box

ShelfOS tracks:

- where components are
- how many exist in each location

Lot-level invoice tracking is out of scope.

---

# 9. Invoices

Components may exist without invoices.

Invoices must contain at least one invoice line.

## invoices

- supplier
- invoice_number
- invoice_date
- currency
- total_net
- total_gross
- file_path
- notes

## invoice_lines

- invoice_id
- component_id
- supplier_part_number
- quantity
- unit_price
- total_price

Navigation must support:

component → invoices

invoice → components

---

# 10. Attachments

Supported attachment types:

- photo
- datasheet
- invoice_pdf
- note
- other

Files are stored on disk.

Database stores metadata and paths only.

---

# 11. Main UI

## Generic Component View

When multiple component types are displayed:

Show only common columns:

- type
- manufacturer
- MPN
- package
- mounting_type
- quantity
- location

## Type-Specific View

When a single type is selected:

Show common columns plus type-specific parameters.

---

# 12. Component Details View

Contains:

- image
- parameters
- locations
- purchase history
- invoice references
- datasheets
- notes

Images are displayed only in details view.

Never directly inside the main table.

---

# 13. Creating Component Types

When creating a component:

1. Select component type.
2. If type does not exist:
   - click New Type
   - create type
   - define parameters
3. Save type.
4. Newly created type becomes automatically selected.

---

# 14. Adding Stock

## Manual Add Stock

Workflow:

1. Find existing component.
2. Click Add Stock.
3. Enter:
   - quantity
   - location
   - note (optional)
4. Save.

Stock movement must be recorded.

Location creation must be possible directly from the dialog.

---

# 15. Removing Stock

## Manual Removal

Workflow:

1. Click Take From Stock.
2. Enter:
   - quantity
   - location
   - note/reason
3. Save.

Stock movement must be recorded.

## Row Actions

To reduce visual clutter:

Buttons should only appear when hovering over a row.

Suggested actions:

- Add Stock
- Take From Stock
- Details

---

# 16. Invoice Entry Workflow

Workflow:

1. Create invoice.
2. Enter invoice metadata.
3. Add invoice lines.
4. Link lines to components.
5. Create missing components if needed.
6. Assign locations.
7. Finalize invoice.

After finalization:

- invoice becomes read-only
- purchase history is updated
- stock movements are generated

Implementation may use:

- dedicated invoice screen
- invoice edit mode based on component table

Developer may choose simpler implementation.

---

# 17. Stock Movement History

## stock_movements

- component_id
- location_id
- delta_quantity
- reason
- timestamp
- user_id

Examples:

- purchase
- manual correction
- usage
- damaged/lost

Manual corrections are allowed.

---

# 18. Users and Permissions

Roles:

- admin
- user
- read-only

---

# 19. Audit Logging

Track:

- quantity changes
- location changes
- invoice modifications
- parameter modifications

---

# 20. Deletion Policy

Components cannot be deleted from normal UI.

Supported statuses:

- active
- archived
- obsolete
- hidden

## Soft Delete

Fields:

- deleted_at
- deleted_reason
- deleted_by

## Administrative Delete

Allowed only through backend API.

Example:

DELETE /api/admin/components/{id}

---

# 21. Import / Export

Initial:

- CSV import
- CSV export

Future:

- BOM import
- OCR invoice import

---

# 22. BOM and KiCad Integration

Future functionality.

Potential features:

- BOM import
- project management
- component matching
- reservation of stock

Potential metadata:

- kicad_symbol
- kicad_footprint
- kicad_value

---

# 23. Backup Strategy

Backup must include:

- SQLite database
- attachments directory

---

# 24. Testing Requirements

Testing is a first-class requirement.

## Unit Tests

Cover:

- business logic
- stock calculations
- stock movements
- invoice processing
- parameter handling
- location hierarchy
- permissions

## Integration Tests

Cover:

- database operations
- API endpoints
- invoice workflows
- stock workflows
- authentication

## UI Tests

Future support:

- component creation
- stock addition
- stock removal
- invoice entry

---

# 25. Development Tooling

Required:

- pytest
- pytest-cov
- mypy
- ruff
- black

Optional:

- Playwright
- factory_boy

---

# 26. Definition of Done

A feature is complete only if:

- implementation exists
- automated tests exist
- linting passes
- type checking passes

Bug fixes should include regression tests whenever practical.

---

# 27. Initial Scope

Implement first:

- components
- types
- dynamic parameters
- locations
- invoices
- stock management
- filtering and sorting
- attachments
- lightweight UI

Avoid initially:

- ERP functionality
- accounting
- advanced lot tracking
- BOM workflows
- project workflows
- heavy frontend frameworks
