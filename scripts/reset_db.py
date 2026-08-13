#!/usr/bin/env python3
"""Wipe the configured database AND every stored attachment.

Destructive and NOT reversible — asks for confirmation unless ``--yes`` is given.

By default empties the whole database (drop + recreate the schema). With
``--keep-types`` it preserves the component-type taxonomy (types + their parameter
definitions and enum values) and the user accounts, and wipes everything else
(components, invoices, imports, stock, locations, BOMs, links, audit) plus all
attachments — so an import test can be repeated against your existing types
without rebuilding them each time.

Usage::

    python scripts/reset_db.py               # full wipe, prompts first
    python scripts/reset_db.py --yes         # full wipe, no prompt
    python scripts/reset_db.py --keep-types  # keep types + users, wipe the rest

Targets the same places as the app: ``DATABASE_URL`` (default ``data/shelfos.db``)
and ``SHELFOS_ATTACHMENTS_DIR`` (default ``attachments``).
"""

from __future__ import annotations

import argparse
import shutil

import app.models  # noqa: F401  (registers every table on SQLModel.metadata)
from app import config
from app.db import engine, init_db
from sqlmodel import SQLModel

# Tables kept by --keep-types: the component-type taxonomy plus the login accounts.
_KEEP_TABLES = {
    "component_types",
    "parameter_definitions",
    "parameter_enum_values",
    "users",
}


def _wipe_attachments() -> int:
    """Delete every file/dir under ATTACHMENTS_DIR; return how many were removed."""
    directory = config.ATTACHMENTS_DIR
    if not directory.exists():
        return 0
    removed = 0
    for child in directory.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
        removed += 1
    return removed


def _empty_database(keep_types: bool) -> None:
    init_db()  # make sure the schema exists before we touch it
    if not keep_types:
        SQLModel.metadata.drop_all(engine)
        init_db()
        return
    # Delete rows from every table except the kept ones, children first so foreign
    # keys are never orphaned.
    with engine.begin() as conn:
        for table in reversed(SQLModel.metadata.sorted_tables):
            if table.name not in _KEEP_TABLES:
                conn.execute(table.delete())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt"
    )
    parser.add_argument(
        "--keep-types",
        action="store_true",
        help="preserve component types (+ params) and users; wipe everything else",
    )
    args = parser.parse_args()

    print("This will PERMANENTLY delete:")
    if args.keep_types:
        print(f"  - all data in {engine.url} EXCEPT component types and users")
    else:
        print(f"  - all data in {engine.url}")
    print(f"  - all files under {config.ATTACHMENTS_DIR}/")
    if not args.yes and input("Type 'yes' to continue: ").strip() != "yes":
        print("Aborted.")
        return

    _empty_database(args.keep_types)
    removed = _wipe_attachments()

    kept = " (types and users kept)" if args.keep_types else ""
    print(
        f"Database wiped{kept}; {removed} attachment entr"
        f"{'y' if removed == 1 else 'ies'} removed."
    )
    if not args.keep_types:
        print("Start the app to seed the admin account, or run scripts/seed_demo.py.")


if __name__ == "__main__":
    main()
