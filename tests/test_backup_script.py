"""Tests for scripts/backup.py: backup/restore of database + attachments."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup.py"
_spec = importlib.util.spec_from_file_location("backup_script", _SCRIPT)
assert _spec and _spec.loader
backup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup)


def _make_database(path: Path, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE items (name TEXT)")
        conn.executemany("INSERT INTO items VALUES (?)", [(row,) for row in rows])


def _read_rows(path: Path) -> list[str]:
    with sqlite3.connect(path) as conn:
        return [row[0] for row in conn.execute("SELECT name FROM items ORDER BY name")]


@pytest.fixture()
def source(tmp_path: Path) -> dict[str, Path]:
    db = tmp_path / "live" / "shelfos.db"
    _make_database(db, ["resistor", "capacitor"])
    attachments = tmp_path / "live" / "attachments"
    (attachments / "components" / "1").mkdir(parents=True)
    (attachments / "components" / "1" / "datasheet.pdf").write_bytes(b"%PDF-fake")
    (attachments / "invoice.pdf").write_bytes(b"%PDF-invoice")
    # The thumbnail cache must NOT be backed up — the app regenerates it.
    (attachments / ".thumbs").mkdir()
    (attachments / ".thumbs" / "1.png").write_bytes(b"png")
    return {"db": db, "attachments": attachments, "root": tmp_path}


def test_create_and_restore_round_trip(source: dict[str, Path]) -> None:
    archive = source["root"] / "backup.tar.gz"
    manifest = backup.create_backup(source["db"], source["attachments"], archive)
    assert archive.is_file()
    assert manifest["format"] == 1
    assert manifest["attachments"]["count"] == 2  # .thumbs excluded

    # Restore into a completely different location (as after moving hosts).
    target_db = source["root"] / "restored" / "data" / "shelfos.db"
    target_attachments = source["root"] / "restored" / "attachments"
    restored = backup.restore_backup(archive, target_db, target_attachments)
    assert restored["database"]["sha256"] == manifest["database"]["sha256"]
    assert _read_rows(target_db) == ["capacitor", "resistor"]
    stored = target_attachments / "components" / "1" / "datasheet.pdf"
    assert stored.read_bytes() == b"%PDF-fake"
    assert (target_attachments / "invoice.pdf").read_bytes() == b"%PDF-invoice"
    assert not (target_attachments / ".thumbs").exists()


def test_restore_replaces_current_state_and_drops_stale_sidecars(
    source: dict[str, Path],
) -> None:
    archive = source["root"] / "backup.tar.gz"
    backup.create_backup(source["db"], source["attachments"], archive)

    # Mutate the live state after the backup was taken.
    _make_database(source["root"] / "ignored.db", [])  # unrelated file, untouched
    with sqlite3.connect(source["db"]) as conn:
        conn.execute("INSERT INTO items VALUES ('added-later')")
    (source["attachments"] / "added-later.pdf").write_bytes(b"junk")
    stale_wal = source["db"].with_name(source["db"].name + "-wal")
    stale_wal.write_bytes(b"stale wal")

    backup.restore_backup(archive, source["db"], source["attachments"])
    assert _read_rows(source["db"]) == ["capacitor", "resistor"]
    assert not stale_wal.exists()
    # The attachments tree is swapped whole: post-backup files do not survive.
    assert not (source["attachments"] / "added-later.pdf").exists()
    assert (source["attachments"] / "invoice.pdf").is_file()
    # No staging leftovers next to the live directories.
    leftovers = [
        p for p in source["attachments"].parent.iterdir() if ".restore" in p.name
    ]
    assert leftovers == []


def test_create_backup_works_with_no_attachments_dir(source: dict[str, Path]) -> None:
    archive = source["root"] / "backup.tar.gz"
    manifest = backup.create_backup(
        source["db"], source["root"] / "does-not-exist", archive
    )
    assert manifest["attachments"] == {"count": 0, "bytes": 0}
    target = source["root"] / "restored"
    backup.restore_backup(archive, target / "shelfos.db", target / "attachments")
    assert (target / "attachments").is_dir()


def test_create_refuses_missing_db_and_existing_archive(
    source: dict[str, Path],
) -> None:
    archive = source["root"] / "backup.tar.gz"
    with pytest.raises(backup.BackupError, match="database not found"):
        backup.create_backup(source["root"] / "nope.db", source["attachments"], archive)
    archive.write_bytes(b"precious")
    with pytest.raises(backup.BackupError, match="refusing to overwrite"):
        backup.create_backup(source["db"], source["attachments"], archive)
    assert archive.read_bytes() == b"precious"  # untouched


def test_restore_rejects_foreign_and_corrupt_archives(source: dict[str, Path]) -> None:
    not_tar = source["root"] / "junk.tar.gz"
    not_tar.write_bytes(b"not a tarball")
    with pytest.raises(backup.BackupError, match="not a readable"):
        backup.read_manifest(not_tar)

    # A well-formed tarball with a manifest from the future is refused too.
    future = source["root"] / "future.tar.gz"
    manifest_file = source["root"] / "manifest.json"
    manifest_file.write_text(json.dumps({"app": "shelfos", "format": 999}))
    with tarfile.open(future, "w:gz") as tar:
        tar.add(manifest_file, arcname="manifest.json")
    with pytest.raises(backup.BackupError, match="unsupported backup format"):
        backup.read_manifest(future)


def test_restore_detects_checksum_mismatch(source: dict[str, Path]) -> None:
    archive = source["root"] / "backup.tar.gz"
    manifest = backup.create_backup(source["db"], source["attachments"], archive)

    # Rebuild the archive with a tampered database but the original manifest.
    tampered = source["root"] / "tampered.tar.gz"
    with sqlite3.connect(source["db"]) as conn:
        conn.execute("INSERT INTO items VALUES ('tampered')")
    manifest_file = source["root"] / "manifest.json"
    manifest_file.write_text(json.dumps(manifest))
    with tarfile.open(tampered, "w:gz") as tar:
        tar.add(manifest_file, arcname="manifest.json")
        tar.add(source["db"], arcname="database.sqlite")

    target = source["root"] / "restored"
    with pytest.raises(backup.BackupError, match="checksum mismatch"):
        backup.restore_backup(tampered, target / "shelfos.db", target / "attachments")
    assert not (target / "shelfos.db").exists()  # nothing was replaced


def test_restore_blocks_path_traversal_members(source: dict[str, Path]) -> None:
    # tarfile's "data" filter must reject members that escape the extraction
    # directory; nothing may be written outside it.
    archive = source["root"] / "backup.tar.gz"
    manifest = backup.create_backup(source["db"], source["attachments"], archive)
    evil = source["root"] / "evil.tar.gz"
    manifest_file = source["root"] / "manifest.json"
    manifest_file.write_text(json.dumps(manifest))
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(manifest_file, arcname="manifest.json")
        tar.add(source["db"], arcname="database.sqlite")
        tar.add(source["db"], arcname="attachments/../../escaped.db")

    target = source["root"] / "restored"
    with pytest.raises((backup.BackupError, tarfile.FilterError)):
        backup.restore_backup(evil, target / "shelfos.db", target / "attachments")
    assert not (source["root"] / "escaped.db").exists()


def test_sqlite_path_accepts_only_file_backed_urls() -> None:
    assert backup._sqlite_path("sqlite:///data/shelfos.db") == Path("data/shelfos.db")
    for url in ("sqlite:///:memory:", "postgresql://host/db"):
        with pytest.raises(backup.BackupError, match="file-backed SQLite"):
            backup._sqlite_path(url)
