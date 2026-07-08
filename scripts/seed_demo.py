#!/usr/bin/env python3
"""Populate the configured database with fictional demo data.

Usage::

    python scripts/seed_demo.py           # only if the database is empty
    python scripts/seed_demo.py --force   # add demo data regardless

The target database follows the same configuration as the app (``DATABASE_URL``
env var, defaulting to ``data/shelfos.db``).
"""

from __future__ import annotations

import argparse

from app.db import engine, init_db
from app.demo_data import populate_demo
from app.models.component import Component
from sqlmodel import Session, select


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="insert demo data even if components already exist",
    )
    args = parser.parse_args()

    init_db()
    with Session(engine) as session:
        already_populated = session.exec(select(Component)).first() is not None
        if already_populated and not args.force:
            print(
                "Database already contains components. "
                "Re-run with --force to add demo data anyway."
            )
            return
        counts = populate_demo(session)

    print("Demo data inserted:")
    for name, count in counts.items():
        print(f"  {name:<11} {count}")


if __name__ == "__main__":
    main()
