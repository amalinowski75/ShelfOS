#!/usr/bin/env python3
"""Create and restore full ShelfOS backups: the database plus every attachment.

A backup is a single ``.tar.gz`` archive containing a consistent snapshot of the
SQLite database (taken with SQLite's online backup API, so it is safe to run
while the app is serving requests), the whole attachments tree, and a
``manifest.json`` with a format version and a SHA-256 checksum of the database
that ``restore`` verifies before touching anything. Thumbnails
(``attachments/.thumbs``) are a cache and are not backed up — the app
regenerates them on demand.

Restore replaces the current database and attachments with the archive's
content (confirming first unless ``--yes`` is given) and removes stale SQLite
``-wal``/``-shm``/``-journal`` sidecar files. **Stop the app before
restoring** — a running instance would keep writing to the database being
swapped out from under it.

Configuration (secret key, API keys, admin password) lives in environment
variables, not on disk, so it is intentionally not part of the archive.

Usage::

    python scripts/backup.py create    # writes backups/shelfos-backup-<stamp>.tar.gz
    python scripts/backup.py create -o /mnt/nas/shelfos.tar.gz
    python scripts/backup.py restore backups/shelfos-backup-<stamp>.tar.gz
    python scripts/backup.py restore backup.tar.gz --yes

Targets the same places as the app: ``DATABASE_URL`` (default
``data/shelfos.db``; only file-backed SQLite is supported — for PostgreSQL use
``pg_dump``) and ``SHELFOS_ATTACHMENTS_DIR`` (default ``attachments``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

# Bump when the archive layout changes; restore refuses formats it doesn't know.
_FORMAT = 1

# Archive member names (the database gets a fixed name so a backup can be
# restored on a host whose DATABASE_URL points somewhere else).
_MANIFEST_NAME = "manifest.json"
_DATABASE_NAME = "database.sqlite"
_ATTACHMENTS_PREFIX = "attachments"

_THUMBS_DIR = ".thumbs"


class BackupError(Exception):
    """A backup or restore could not proceed; the message says why."""


def _sqlite_path(database_url: str) -> Path:
    """The on-disk file behind a SQLite ``DATABASE_URL``.

    Anything else (PostgreSQL, in-memory SQLite) has no file to copy, so it is
    out of scope for this tool.
    """
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url == f"{prefix}:memory:":
        raise BackupError(
            f"only file-backed SQLite databases are supported, got {database_url!r} "
            "(for PostgreSQL use pg_dump/pg_restore)"
        )
    return Path(database_url[len(prefix) :])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_database(db_path: Path, destination: Path) -> None:
    """Copy the database with SQLite's online backup API.

    Unlike a plain file copy this yields a consistent snapshot even mid-write,
    and it folds any WAL content into the single output file.
    """
    with (
        sqlite3.connect(db_path) as source,
        sqlite3.connect(destination) as snapshot,
    ):
        source.backup(snapshot)


def _attachment_files(attachments_dir: Path) -> list[Path]:
    """Every stored attachment file, skipping the regenerable thumbnail cache."""
    if not attachments_dir.is_dir():
        return []
    return sorted(
        path
        for path in attachments_dir.rglob("*")
        if path.is_file() and _THUMBS_DIR not in path.relative_to(attachments_dir).parts
    )


def create_backup(db_path: Path, attachments_dir: Path, output: Path) -> dict[str, Any]:
    """Write a backup archive to ``output`` and return its manifest."""
    if not db_path.is_file():
        raise BackupError(f"database not found: {db_path}")
    if output.exists():
        raise BackupError(f"refusing to overwrite existing file: {output}")

    with tempfile.TemporaryDirectory(prefix="shelfos-backup-") as tmp:
        snapshot = Path(tmp) / _DATABASE_NAME
        _snapshot_database(db_path, snapshot)

        files = _attachment_files(attachments_dir)
        manifest: dict[str, Any] = {
            "app": "shelfos",
            "format": _FORMAT,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "database": {
                "source": db_path.name,
                "size": snapshot.stat().st_size,
                "sha256": _sha256(snapshot),
            },
            "attachments": {
                "count": len(files),
                "bytes": sum(path.stat().st_size for path in files),
            },
        }
        manifest_path = Path(tmp) / _MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

        # Build under a temporary name and rename into place at the end, so an
        # interrupted run never leaves a truncated file that looks like a backup.
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_name(output.name + ".part")
        try:
            with tarfile.open(partial, "w:gz") as archive:
                archive.add(manifest_path, arcname=_MANIFEST_NAME)
                archive.add(snapshot, arcname=_DATABASE_NAME)
                for path in files:
                    relative = path.relative_to(attachments_dir)
                    archive.add(path, arcname=f"{_ATTACHMENTS_PREFIX}/{relative}")
            os.replace(partial, output)
        finally:
            partial.unlink(missing_ok=True)
    return manifest


def read_manifest(archive_path: Path) -> dict[str, Any]:
    """The archive's manifest, after checking it is a ShelfOS backup we can read."""
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            member = archive.extractfile(_MANIFEST_NAME)
            if member is None:  # pragma: no cover - tarfile quirk, not reachable
                raise KeyError(_MANIFEST_NAME)
            manifest: dict[str, Any] = json.loads(member.read())
    except (tarfile.TarError, KeyError, json.JSONDecodeError, OSError) as exc:
        raise BackupError(
            f"not a readable ShelfOS backup: {archive_path} ({exc})"
        ) from exc
    if manifest.get("app") != "shelfos" or manifest.get("format") != _FORMAT:
        raise BackupError(
            f"unsupported backup format {manifest.get('format')!r} "
            f"(this tool reads format {_FORMAT})"
        )
    return manifest


def restore_backup(
    archive_path: Path, db_path: Path, attachments_dir: Path
) -> dict[str, Any]:
    """Replace the database and attachments with the archive's content.

    The database is verified against the manifest checksum before anything is
    replaced; the attachments directory is swapped whole, so files that are not
    in the backup do not survive it.
    """
    manifest = read_manifest(archive_path)

    with tempfile.TemporaryDirectory(prefix="shelfos-restore-") as tmp:
        # The "data" filter rejects absolute names, ``..`` traversal, links and
        # special files — nothing from the archive can land outside ``tmp``.
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(tmp, filter="data")

        snapshot = Path(tmp) / _DATABASE_NAME
        if not snapshot.is_file():
            raise BackupError(f"archive has no {_DATABASE_NAME}: {archive_path}")
        digest = _sha256(snapshot)
        expected = manifest["database"]["sha256"]
        if digest != expected:
            raise BackupError(
                f"database checksum mismatch (archive is corrupt): "
                f"expected {expected}, got {digest}"
            )

        # Database: copy next to the target, then swap atomically; drop stale
        # journal sidecars so SQLite cannot replay them over the restored file.
        db_path.parent.mkdir(parents=True, exist_ok=True)
        staged_db = db_path.with_name(db_path.name + ".restore")
        shutil.copy2(snapshot, staged_db)
        os.replace(staged_db, db_path)
        for suffix in ("-wal", "-shm", "-journal"):
            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)

        # Attachments: stage the restored tree beside the live one, then swap
        # directories, so a failure mid-way never leaves a half-copied mix.
        extracted = Path(tmp) / _ATTACHMENTS_PREFIX
        if not extracted.is_dir():
            extracted.mkdir()  # a backup with zero attachments
        pid = os.getpid()
        staged = attachments_dir.parent / f"{attachments_dir.name}.restore-{pid}"
        previous = attachments_dir.parent / f"{attachments_dir.name}.old-{pid}"
        attachments_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extracted), staged)
        try:
            if attachments_dir.exists():
                os.rename(attachments_dir, previous)
            os.rename(staged, attachments_dir)
        except BaseException:
            # Put the live directory back if the swap died between the renames.
            if not attachments_dir.exists() and previous.exists():
                os.rename(previous, attachments_dir)
            shutil.rmtree(staged, ignore_errors=True)
            raise
        shutil.rmtree(previous, ignore_errors=True)
    return manifest


def _default_output() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path("backups") / f"shelfos-backup-{stamp}.tar.gz"


def _describe(manifest: dict[str, Any]) -> str:
    database = manifest["database"]
    attachments = manifest["attachments"]
    return (
        f"created {manifest['created_at']}: database {database['size']:,} bytes, "
        f"{attachments['count']} attachment file(s) ({attachments['bytes']:,} bytes)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="write a backup archive")
    create.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="archive path (default: backups/shelfos-backup-<timestamp>.tar.gz)",
    )

    restore = commands.add_parser(
        "restore", help="replace database and attachments from an archive"
    )
    restore.add_argument("archive", type=Path, help="backup archive to restore")
    restore.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt"
    )

    args = parser.parse_args()

    # Resolve the same targets the app uses (imported lazily so --help works
    # without the app's dependencies installed).
    from app import config
    from app.db import engine

    db_path = _sqlite_path(str(engine.url))
    attachments_dir = config.ATTACHMENTS_DIR

    if args.command == "create":
        output = args.output or _default_output()
        manifest = create_backup(db_path, attachments_dir, output)
        print(f"Backup written to {output}")
        print(f"  {_describe(manifest)}")
        return

    manifest = read_manifest(args.archive)
    print(f"About to restore {args.archive}")
    print(f"  {_describe(manifest)}")
    print("This will PERMANENTLY replace:")
    print(f"  - the database at {db_path}")
    print(f"  - all files under {attachments_dir}/")
    print("Stop the app before restoring.")
    if not args.yes and input("Type 'yes' to continue: ").strip() != "yes":
        print("Aborted.")
        return
    restore_backup(args.archive, db_path, attachments_dir)
    print("Restore complete. Start the app to use the restored data.")


if __name__ == "__main__":
    try:
        main()
    except BackupError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
