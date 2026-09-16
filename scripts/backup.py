#!/usr/bin/env python3
"""Create and restore full ShelfOS backups: the database plus every attachment.

A backup is a single ``.tar.gz`` archive (mode 0600 — it contains password
hashes) with a consistent snapshot of the SQLite database (taken with SQLite's
online backup API, so it is safe to run while the app is serving requests), the
whole attachments tree, and a ``manifest.json`` with a format version and
SHA-256 checksums of the database and of every attachment, all verified before
``restore`` touches anything. Thumbnails (``attachments/.thumbs``) are a cache
and are not backed up — the app regenerates them on demand.

Restore replaces the current database and attachments with the archive's
content (confirming first unless ``--yes`` is given) and removes stale SQLite
``-wal``/``-shm``/``-journal`` sidecar files. **Stop the app before
restoring** — a running instance would keep writing to the old database file
through its open handle, and those writes would silently vanish. On Linux the
tool checks ``/proc`` for processes holding the database open and refuses to
proceed while any exist (``--force`` overrides).

Configuration (secret key, API keys, admin password) lives in environment
variables, not on disk, so it is intentionally not part of the archive.

``create --keep N`` sweeps the directory it just wrote into afterwards, leaving
the ``N`` newest archives and deleting the rest; without it nothing is ever
deleted. Only files named the way ``create`` names them without an ``-o``
(``shelfos-backup-*.tar.gz``) are ever deleted, so an archive given some other
name is never swept — and ``create`` says so rather than reporting a retention
it is not applying. The sweep runs only after the new archive is safely in
place, so a run that fails cannot be the run that frees the disk.

Usage::

    python scripts/backup.py create    # writes backups/shelfos-backup-<stamp>.tar.gz
    python scripts/backup.py create -o /mnt/nas/shelfos.tar.gz
    python scripts/backup.py create --keep 3    # and delete all but the 3 newest
    python scripts/backup.py restore backups/shelfos-backup-<stamp>.tar.gz
    python scripts/backup.py restore backup.tar.gz --yes

Targets the same places as the app: ``DATABASE_URL`` (default
``data/shelfos.db``; only file-backed SQLite is supported — for PostgreSQL use
``pg_dump``) and ``SHELFOS_ATTACHMENTS_DIR`` (default ``attachments``).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from contextlib import closing
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

# What `create` names an archive when no -o says otherwise, as a glob. The
# retention sweep only ever considers files matching it, so an operator's own
# copy in the same directory — `pre-upgrade.tar.gz`, say — is never a candidate
# for deletion, however old it is.
_ARCHIVE_GLOB = "shelfos-backup-*.tar.gz"


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
    # Drop any query string (e.g. ``?timeout=30``) — it is not part of the path.
    return Path(database_url[len(prefix) :].split("?", 1)[0])


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
        closing(sqlite3.connect(db_path)) as source,
        closing(sqlite3.connect(destination)) as snapshot,
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

    # Claim the output name atomically (O_EXCL), so two runs racing for the
    # same path cannot clobber each other; the mode also keeps the finished
    # archive at 0600 — it carries password hashes and tends to get copied to
    # shared storage.
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        raise BackupError(f"refusing to overwrite existing file: {output}") from None

    try:
        with tempfile.TemporaryDirectory(
            prefix="shelfos-backup-", ignore_cleanup_errors=True
        ) as tmp:
            snapshot = Path(tmp) / _DATABASE_NAME
            _snapshot_database(db_path, snapshot)

            files = _attachment_files(attachments_dir)
            checksums = {
                path.relative_to(attachments_dir).as_posix(): _sha256(path)
                for path in files
            }
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
                    "files": checksums,
                },
            }
            manifest_path = Path(tmp) / _MANIFEST_NAME
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

            # Build under a temporary name and swap over the placeholder at the
            # end, so an interrupted run never leaves a truncated file that
            # looks like a backup.
            partial = output.with_name(output.name + ".part")
            try:
                # The gzip header records an original filename, and building
                # under "<name>.part" would put the temporary one in there for
                # good — so `file` reports a finished backup as having been
                # something.part, which reads like a truncated download. Name
                # the stream after what the archive will be called.
                with (
                    partial.open("wb") as raw,
                    gzip.GzipFile(
                        filename=output.name, mode="wb", fileobj=raw
                    ) as compressed,
                    tarfile.open(fileobj=compressed, mode="w") as archive,
                ):
                    archive.add(manifest_path, arcname=_MANIFEST_NAME)
                    archive.add(snapshot, arcname=_DATABASE_NAME)
                    for path in files:
                        relative = path.relative_to(attachments_dir)
                        archive.add(path, arcname=f"{_ATTACHMENTS_PREFIX}/{relative}")
                partial.chmod(0o600)
                os.replace(partial, output)
            finally:
                partial.unlink(missing_ok=True)
    except BaseException:
        output.unlink(missing_ok=True)  # never leave a bogus placeholder behind
        raise
    return manifest


def _archives(directory: Path) -> list[Path]:
    """Finished archives in ``directory``, newest first.

    Newest by modification time, with the name as the tiebreaker so that two
    archives written in the same second still have a defined order.

    Empty files are not archives yet. ``create_backup`` claims its output name
    with O_EXCL before it starts writing and swaps the real content in at the
    end, so for as long as a tar over the attachments tree takes there is a
    0-byte file in the directory that matches the glob and whose mtime is now.
    Counting that as one of the newest would mean a second run — a catch-up, or
    somebody taking one by hand at 03:16 — keeping a placeholder in place of a
    real archive, and deleting a real one to make room for it.
    """
    if not directory.is_dir():
        return []

    def stamp(path: Path) -> tuple[float, str] | None:
        try:
            info = path.stat()
        except OSError:  # vanished under us between the glob and here
            return None
        return (info.st_mtime, path.name) if info.st_size > 0 else None

    dated = [
        (key, path)
        for path in directory.glob(_ARCHIVE_GLOB)
        if path.is_file() and (key := stamp(path)) is not None
    ]
    return [path for _, path in sorted(dated, key=lambda pair: pair[0], reverse=True)]


def prune_backups(
    directory: Path, keep: int, *, protect: Path | None = None
) -> list[Path]:
    """Delete all but the ``keep`` newest archives in ``directory``.

    Only files named the way ``create`` names them are considered, and
    ``protect`` (the archive this run just wrote) is never deleted whatever the
    clock says about it — a host whose time jumped backwards would otherwise
    throw away the only backup it is certain about.

    A file that cannot be removed is reported and stepped over rather than
    raising: the backup it is being kept beside has already succeeded, and
    failing here would turn a full directory into a failed nightly run.

    Returns the paths removed.
    """
    if keep < 1:
        raise BackupError(f"refusing to keep fewer than one backup (got {keep})")

    protected = protect.resolve() if protect is not None else None
    ordered = _archives(directory)
    removed: list[Path] = []
    for path in ordered[keep:]:
        if protected is not None and path.resolve() == protected:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            print(f"Warning: could not remove {path}: {error}", file=sys.stderr)
            continue
        removed.append(path)
    return removed


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
    # Validate the shape here, once, so everything downstream (including the
    # pre-restore summary) can index into the manifest without a KeyError.
    database = manifest.get("database")
    attachments = manifest.get("attachments")
    if (
        not isinstance(manifest.get("created_at"), str)
        or not isinstance(database, dict)
        or not isinstance(database.get("sha256"), str)
        or not isinstance(database.get("size"), int)
        or not isinstance(attachments, dict)
        or not isinstance(attachments.get("count"), int)
        or not isinstance(attachments.get("bytes"), int)
        or not isinstance(attachments.get("files"), dict)
    ):
        raise BackupError(f"malformed manifest in {archive_path}")
    return manifest


def _pids_with_open_file(path: Path) -> list[int]:
    """Other processes holding ``path`` open — best effort, Linux ``/proc`` only.

    On systems without ``/proc`` (or for processes we may not inspect) this
    simply finds nothing; the check is a guard rail, not a guarantee.
    """
    target = str(path.resolve())
    pids: list[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return pids
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(fd) == target:
                    pids.append(int(entry.name))
                    break
            except OSError:
                continue
    return pids


def restore_backup(
    archive_path: Path,
    db_path: Path,
    attachments_dir: Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Replace the database and attachments with the archive's content.

    Everything — the database and each attachment — is verified against the
    manifest checksums before anything is replaced; the attachments directory
    is swapped whole, so files that are not in the backup do not survive it.
    ``force=True`` skips the is-the-app-still-running guard.
    """
    manifest = read_manifest(archive_path)
    # Operate on the real locations: a symlinked attachments dir (external
    # storage) must have its *target* swapped, not the link itself replaced.
    db_path = db_path.resolve()
    attachments_dir = attachments_dir.resolve()

    with tempfile.TemporaryDirectory(prefix="shelfos-restore-") as tmp:
        # The "data" filter rejects absolute names, ``..`` traversal, links and
        # special files — nothing from the archive can land outside ``tmp``.
        with tarfile.open(archive_path, "r:gz") as archive:
            needed = sum(member.size for member in archive.getmembers())
            free = shutil.disk_usage(tmp).free
            if needed > free:
                raise BackupError(
                    f"archive claims {needed:,} bytes but only {free:,} are free "
                    f"in {tmp} — not extracting"
                )
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

        # Attachments must match the manifest exactly — same file list, same
        # checksums — before anything live is touched.
        extracted = Path(tmp) / _ATTACHMENTS_PREFIX
        if not extracted.is_dir():
            extracted.mkdir()  # a backup with zero attachments
        found = {
            path.relative_to(extracted).as_posix(): path
            for path in extracted.rglob("*")
            if path.is_file()
        }
        recorded: dict[str, str] = manifest["attachments"]["files"]
        if set(found) != set(recorded):
            raise BackupError(
                "attachment list does not match the manifest (archive is corrupt)"
            )
        for relative, checksum in recorded.items():
            if _sha256(found[relative]) != checksum:
                raise BackupError(
                    f"attachment checksum mismatch (archive is corrupt): {relative}"
                )

        # A running app would keep writing to the swapped-out database file
        # through its open handle — writes that silently vanish. Refuse.
        if not force and db_path.exists():
            pids = _pids_with_open_file(db_path)
            if pids:
                raise BackupError(
                    f"database {db_path} is open by process(es) "
                    f"{', '.join(map(str, pids))} — stop the app first "
                    "(or pass --force to restore anyway)"
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


def _keep_count(value: str) -> int:
    """argparse type for ``--keep``: a whole number of archives, at least one.

    Checked here rather than in the sweep so that a bad number is a usage error
    *before* a backup is taken. Refusing it afterwards would mean a unit edited
    to ``--keep 0`` writes a perfectly good archive every night and still exits
    non-zero — a healthy backup that `systemctl status` reports as failed, and
    that stops `update` in its tracks.
    """
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {value!r}") from None
    if count < 1:
        raise argparse.ArgumentTypeError(
            f"refusing to keep fewer than one backup (got {count})"
        )
    return count


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
    create.add_argument(
        "--keep",
        type=_keep_count,
        default=None,
        metavar="N",
        help=(
            "after the backup succeeds, delete all but the N newest archives "
            f"named {_ARCHIVE_GLOB} in the directory it was written to; nothing "
            "else there is touched (default: keep every archive)"
        ),
    )

    restore = commands.add_parser(
        "restore", help="replace database and attachments from an archive"
    )
    restore.add_argument("archive", type=Path, help="backup archive to restore")
    restore.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt"
    )
    restore.add_argument(
        "--force",
        action="store_true",
        help="restore even if another process holds the database open",
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
        if args.keep is not None:
            # Only now, with the new archive in place: sweeping first would mean
            # a run that then failed had deleted the backups it was replacing.
            removed = prune_backups(output.parent, args.keep, protect=output)
            for path in removed:
                print(f"  removed {path.name}")
            # What is actually there afterwards, not the number that was asked
            # for. An -o naming the archive something else — the directory is
            # then full of files the sweep will never consider — otherwise
            # prints "keeping the 7 newest" every night while nothing is ever
            # deleted and the disk fills.
            kept = _archives(output.parent)
            print(f"  {len(kept)} archive(s) named {_ARCHIVE_GLOB} in {output.parent}")
            if output not in kept:
                print(
                    f"Warning: --keep manages archives named {_ARCHIVE_GLOB}, and "
                    f"{output.name} is not one — it will never be swept, and "
                    "neither will anything else written under that name",
                    file=sys.stderr,
                )
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
    restore_backup(args.archive, db_path, attachments_dir, force=args.force)
    print("Restore complete. Start the app to use the restored data.")


if __name__ == "__main__":
    try:
        main()
    except BackupError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
