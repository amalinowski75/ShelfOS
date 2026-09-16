"""Tests for scripts/backup.py: backup/restore of database + attachments."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
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
    assert archive.stat().st_mode & 0o777 == 0o600  # holds password hashes
    assert manifest["format"] == 1
    assert manifest["attachments"]["count"] == 2  # .thumbs excluded
    assert set(manifest["attachments"]["files"]) == {
        "components/1/datasheet.pdf",
        "invoice.pdf",
    }

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
    # No staging leftovers next to the live directories — neither the
    # ".restore-<pid>" staging dir nor the ".old-<pid>" pre-restore copy.
    leftovers = [
        p
        for p in source["attachments"].parent.iterdir()
        if ".restore" in p.name or ".old" in p.name
    ]
    assert leftovers == []


def test_create_backup_works_with_no_attachments_dir(source: dict[str, Path]) -> None:
    archive = source["root"] / "backup.tar.gz"
    manifest = backup.create_backup(
        source["db"], source["root"] / "does-not-exist", archive
    )
    assert manifest["attachments"] == {"count": 0, "bytes": 0, "files": {}}
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

    # Right app and format but missing the database/attachments sections: the
    # shape is validated up front, so no downstream KeyError (e.g. in the
    # pre-restore summary main() prints).
    hollow = source["root"] / "hollow.tar.gz"
    manifest_file.write_text(json.dumps({"app": "shelfos", "format": 1}))
    with tarfile.open(hollow, "w:gz") as tar:
        tar.add(manifest_file, arcname="manifest.json")
    with pytest.raises(backup.BackupError, match="malformed manifest"):
        backup.read_manifest(hollow)


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
    # A query string is connection config, not part of the filename.
    assert backup._sqlite_path("sqlite:///data/shelfos.db?timeout=30") == Path(
        "data/shelfos.db"
    )
    for url in ("sqlite:///:memory:", "postgresql://host/db"):
        with pytest.raises(backup.BackupError, match="file-backed SQLite"):
            backup._sqlite_path(url)


def test_restore_rolls_back_the_live_dir_when_the_swap_fails_midway(
    source: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = source["root"] / "backup.tar.gz"
    backup.create_backup(source["db"], source["attachments"], archive)
    (source["attachments"] / "added-later.pdf").write_bytes(b"live content")

    # Fail exactly once, on the rename that moves the staged tree into place —
    # i.e. after the live directory has already been moved aside.
    live = source["attachments"].resolve()
    real_rename = os.rename
    tripped = False

    def failing_rename(src: object, dst: object) -> None:
        nonlocal tripped
        if not tripped and Path(str(dst)) == live:
            tripped = True
            raise OSError("disk went away")
        real_rename(str(src), str(dst))

    monkeypatch.setattr(backup.os, "rename", failing_rename)
    with pytest.raises(OSError, match="disk went away"):
        backup.restore_backup(archive, source["db"], source["attachments"])
    assert tripped

    # The pre-restore attachments tree is back under its original name, and
    # neither the staging dir nor the ".old" copy survives.
    assert (source["attachments"] / "added-later.pdf").read_bytes() == b"live content"
    leftovers = [
        p
        for p in source["attachments"].parent.iterdir()
        if ".restore" in p.name or ".old" in p.name
    ]
    assert leftovers == []


def test_restore_detects_a_tampered_attachment(source: dict[str, Path]) -> None:
    archive = source["root"] / "backup.tar.gz"
    backup.create_backup(source["db"], source["attachments"], archive)

    # Repack the archive with one attachment's bytes flipped; the manifest
    # (and the database) stay pristine.
    tampered = source["root"] / "tampered.tar.gz"
    with tarfile.open(archive) as src, tarfile.open(tampered, "w:gz") as dst:
        for member in src.getmembers():
            handle = src.extractfile(member)
            data = handle.read() if handle else None
            if member.name == "attachments/invoice.pdf":
                data = b"flipped bits"
                member.size = len(data)
            dst.addfile(member, io.BytesIO(data) if data is not None else None)

    target = source["root"] / "restored"
    with pytest.raises(backup.BackupError, match="attachment checksum mismatch"):
        backup.restore_backup(tampered, target / "shelfos.db", target / "attachments")
    assert not (target / "shelfos.db").exists()  # verified before any replacement


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs Linux /proc")
def test_restore_refuses_while_another_process_holds_the_db_open(
    source: dict[str, Path],
) -> None:
    archive = source["root"] / "backup.tar.gz"
    backup.create_backup(source["db"], source["attachments"], archive)

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sqlite3, sys, time\n"
            "conn = sqlite3.connect(sys.argv[1])\n"
            "print('open', flush=True)\n"
            "time.sleep(60)\n",
            str(source["db"]),
        ],
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == b"open"
        with pytest.raises(backup.BackupError, match="open by process"):
            backup.restore_backup(archive, source["db"], source["attachments"])
        # --force overrides the guard.
        backup.restore_backup(archive, source["db"], source["attachments"], force=True)
    finally:
        holder.kill()
        holder.wait()


def test_restore_swaps_the_target_of_a_symlinked_attachments_dir(
    source: dict[str, Path],
) -> None:
    archive = source["root"] / "backup.tar.gz"
    backup.create_backup(source["db"], source["attachments"], archive)

    real = source["root"] / "nas" / "shelfos-attachments"
    real.mkdir(parents=True)
    link = source["root"] / "attachments-link"
    link.symlink_to(real)

    backup.restore_backup(archive, source["db"], link)
    assert link.is_symlink()  # the link survives...
    assert (real / "invoice.pdf").is_file()  # ...and its target got the content


def test_restore_refuses_an_archive_larger_than_free_disk(
    source: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = source["root"] / "backup.tar.gz"
    backup.create_backup(source["db"], source["attachments"], archive)

    class _Tiny:
        total = used = 0
        free = 1

    monkeypatch.setattr(backup.shutil, "disk_usage", lambda _path: _Tiny)
    with pytest.raises(backup.BackupError, match="not extracting"):
        backup.restore_backup(
            archive, source["root"] / "r" / "db", source["root"] / "r" / "att"
        )


def test_the_archive_is_not_named_after_the_temporary_file(
    source: dict[str, Path],
) -> None:
    """gzip records the uncompressed name in its header, and the archive is
    built under "<name>.part" — so without care that temporary name is what
    every tool reports for good, and a finished backup reads like a truncated
    download.
    """
    import gzip as gzip_module

    output = source["root"] / "shelfos-backup-20260907.tar.gz"
    backup.create_backup(source["db"], source["attachments"], output)

    with output.open("rb") as raw:
        header = raw.read(10)
        assert header[3] & 0x08, "no filename stored in the gzip header"
        name = b""
        while (byte := raw.read(1)) not in (b"\x00", b""):
            name += byte
    assert not name.endswith(b".part"), name
    assert name == b"shelfos-backup-20260907.tar"
    # And it is still a readable archive after all that.
    with gzip_module.open(output) as stream:
        assert stream.read(2)


def _aged_archives(directory: Path, names_oldest_first: list[str]) -> list[Path]:
    """Archives a second apart, in the order given, oldest first."""
    directory.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    for index, name in enumerate(names_oldest_first):
        path = directory / name
        path.write_bytes(b"archive")
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
        made.append(path)
    return made


def test_prune_keeps_the_newest_and_deletes_the_rest(tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    old, older_still, newer, newest = _aged_archives(
        directory,
        [
            "shelfos-backup-20260901-031500.tar.gz",
            "shelfos-backup-20260902-031500.tar.gz",
            "shelfos-backup-20260903-031500.tar.gz",
            "shelfos-backup-20260904-031500.tar.gz",
        ],
    )
    removed = backup.prune_backups(directory, 3)
    assert removed == [old]
    assert not old.exists()
    assert [path.name for path in sorted(directory.iterdir())] == [
        older_still.name,
        newer.name,
        newest.name,
    ]
    # A second sweep with nothing to do is not an error and deletes nothing.
    assert backup.prune_backups(directory, 3) == []
    assert len(list(directory.iterdir())) == 3


def test_prune_goes_by_age_not_by_name(tmp_path: Path) -> None:
    """The timestamp in the name is when a run started, and an -o can put any
    name at all in the directory. What is kept is what was written last."""
    directory = tmp_path / "backups"
    _aged_archives(
        directory,
        [
            "shelfos-backup-zzzz.tar.gz",  # oldest, but last alphabetically
            "shelfos-backup-20260101-000000.tar.gz",
        ],
    )
    removed = backup.prune_backups(directory, 1)
    assert [path.name for path in removed] == ["shelfos-backup-zzzz.tar.gz"]
    assert [path.name for path in directory.iterdir()] == [
        "shelfos-backup-20260101-000000.tar.gz"
    ]


def test_prune_only_touches_files_named_the_way_create_names_them(
    tmp_path: Path,
) -> None:
    """The archives share a directory with whatever an operator put there — a
    copy kept before an upgrade, a note. None of it is the sweep's business."""
    directory = tmp_path / "backups"
    _aged_archives(
        directory,
        [
            "keep-me-before-the-upgrade.tar.gz",
            "shelfos-backup-20260101.tar.gz.sha256",
            "notes.txt",
            "shelfos-backup-20260101-000000.tar.gz",
            "shelfos-backup-20260102-000000.tar.gz",
        ],
    )
    (directory / "subdir").mkdir()
    (directory / "subdir" / "shelfos-backup-20250101-000000.tar.gz").write_bytes(b"x")

    removed = backup.prune_backups(directory, 1)

    assert [path.name for path in removed] == ["shelfos-backup-20260101-000000.tar.gz"]
    assert {path.name for path in directory.iterdir()} == {
        "keep-me-before-the-upgrade.tar.gz",
        "shelfos-backup-20260101.tar.gz.sha256",
        "notes.txt",
        "shelfos-backup-20260102-000000.tar.gz",
        "subdir",
    }
    assert (directory / "subdir" / "shelfos-backup-20250101-000000.tar.gz").exists()


def test_prune_never_deletes_the_archive_just_written(tmp_path: Path) -> None:
    """A host whose clock jumped backwards writes an archive that looks older
    than the ones it is replacing. Deleting that one is deleting the only backup
    this run is certain about."""
    directory = tmp_path / "backups"
    _aged_archives(
        directory,
        [
            "shelfos-backup-20260101-000000.tar.gz",
            "shelfos-backup-20260102-000000.tar.gz",
        ],
    )
    fresh = directory / "shelfos-backup-19990101-000000.tar.gz"
    fresh.write_bytes(b"just written")
    os.utime(fresh, (1, 1))  # older than everything else, by the clock

    removed = backup.prune_backups(directory, 1, protect=fresh)

    assert [path.name for path in removed] == ["shelfos-backup-20260101-000000.tar.gz"]
    assert fresh.exists()


def test_prune_refuses_to_keep_nothing_and_survives_a_missing_directory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "backups"
    _aged_archives(directory, ["shelfos-backup-20260101-000000.tar.gz"])
    for keep in (0, -1):
        with pytest.raises(backup.BackupError, match="fewer than one"):
            backup.prune_backups(directory, keep)
    assert len(list(directory.iterdir())) == 1
    assert backup.prune_backups(tmp_path / "never-backed-up", 3) == []


def test_prune_reports_an_archive_it_cannot_remove_and_keeps_going(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The backup beside it has already succeeded; a directory nobody may write
    to is worth a line in the journal, not a failed nightly run."""
    directory = tmp_path / "backups"
    stuck, also_old, newest = _aged_archives(
        directory,
        [
            "shelfos-backup-20260101-000000.tar.gz",
            "shelfos-backup-20260102-000000.tar.gz",
            "shelfos-backup-20260103-000000.tar.gz",
        ],
    )
    real_unlink = Path.unlink

    def refuse_one(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == stuck.name:
            raise PermissionError(13, "Permission denied")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "unlink", refuse_one)
        removed = backup.prune_backups(directory, 1)

    assert removed == [also_old]
    assert stuck.exists() and newest.exists()
    assert "could not remove" in capsys.readouterr().err


def test_cli_create_with_keep_sweeps_only_after_a_successful_backup(
    source: dict[str, Path], tmp_path: Path
) -> None:
    """End to end, the way the nightly unit runs it: the archive lands, the
    older ones go, and a run that cannot back up deletes nothing."""
    directory = tmp_path / "backups"
    old, newer = _aged_archives(
        directory,
        [
            "shelfos-backup-20260101-000000.tar.gz",
            "shelfos-backup-20260102-000000.tar.gz",
        ],
    )
    output = directory / "shelfos-backup-20260103-000000.tar.gz"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{source['db']}",
        "SHELFOS_ATTACHMENTS_DIR": str(source["attachments"]),
    }
    argv = [sys.executable, str(_SCRIPT), "create", "-o", str(output), "--keep", "2"]
    result = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)

    assert result.returncode == 0, result.stderr
    assert output.is_file()
    assert not old.exists()
    assert newer.exists()
    assert "removed shelfos-backup-20260101-000000.tar.gz" in result.stdout

    # A failed backup sweeps nothing: the archives it would replace are all the
    # machine has.
    env["DATABASE_URL"] = f"sqlite:///{tmp_path / 'not-a-database.db'}"
    argv = [
        sys.executable,
        str(_SCRIPT),
        "create",
        "-o",
        str(directory / "shelfos-backup-20260104-000000.tar.gz"),
        "--keep",
        "1",
    ]
    result = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)

    assert result.returncode == 1
    assert "database not found" in result.stderr
    assert newer.exists() and output.exists()


def test_prune_ignores_a_placeholder_another_run_is_still_writing(
    tmp_path: Path,
) -> None:
    """`create_backup` claims its name with O_EXCL and fills it in at the end,
    so a backup in progress is a 0-byte file with the newest mtime in the
    directory. Counting it would keep a placeholder in place of an archive —
    and delete a real one to make room for it."""
    directory = tmp_path / "backups"
    oldest, older, newest = _aged_archives(
        directory,
        [
            "shelfos-backup-20260101-000000.tar.gz",
            "shelfos-backup-20260102-000000.tar.gz",
            "shelfos-backup-20260103-000000.tar.gz",
        ],
    )
    in_flight = directory / "shelfos-backup-20260104-031500.tar.gz"
    in_flight.touch()  # another run, mid-tar

    removed = backup.prune_backups(directory, 3)

    assert removed == []
    assert in_flight.exists()  # not ours to delete either
    assert oldest.exists() and older.exists() and newest.exists()


def test_cli_refuses_a_keep_that_would_leave_nothing_before_backing_up(
    source: dict[str, Path], tmp_path: Path
) -> None:
    """An operator raising or lowering the number in the installed unit can type
    0. Refusing it after the archive is written would mean a good backup every
    night that systemd reports as failed — and that stops `update`."""
    directory = tmp_path / "backups"
    (existing,) = _aged_archives(directory, ["shelfos-backup-20260101-000000.tar.gz"])
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{source['db']}",
        "SHELFOS_ATTACHMENTS_DIR": str(source["attachments"]),
    }
    for value in ("0", "-1", "three"):
        output = directory / f"shelfos-backup-2026020{value.lstrip('-')[0]}.tar.gz"
        argv = [sys.executable, str(_SCRIPT), "create", "-o", str(output)]
        result = subprocess.run(
            [*argv, "--keep", value],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert result.returncode == 2, result.stdout  # a usage error, not a failure
        assert "--keep" in result.stderr
        assert not output.exists(), "took a backup before refusing the number"
    assert existing.exists()


def test_cli_says_when_the_archive_it_wrote_is_not_one_the_sweep_can_ever_see(
    source: dict[str, Path], tmp_path: Path
) -> None:
    """`-o /mnt/nas/whatever.tar.gz --keep 7` sweeps nothing, because nothing
    there is named the way `create` names archives. Printing "keeping the 7
    newest" at that point reports a retention that is not happening, and the
    disk fills anyway."""
    directory = tmp_path / "nas"
    directory.mkdir()
    output = directory / "shelfos.tar.gz"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{source['db']}",
        "SHELFOS_ATTACHMENTS_DIR": str(source["attachments"]),
    }
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "create", "-o", str(output), "--keep", "7"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert output.is_file()
    assert "keeping the 7 newest" not in result.stdout
    assert "0 archive(s) named shelfos-backup-*.tar.gz" in result.stdout
    assert "will never be swept" in result.stderr


def test_cli_counts_what_is_left_rather_than_what_was_asked_for(
    source: dict[str, Path], tmp_path: Path
) -> None:
    """Two archives and `--keep 5` is two archives kept, not five."""
    directory = tmp_path / "backups"
    _aged_archives(directory, ["shelfos-backup-20260101-000000.tar.gz"])
    output = directory / "shelfos-backup-20260102-000000.tar.gz"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{source['db']}",
        "SHELFOS_ATTACHMENTS_DIR": str(source["attachments"]),
    }
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "create", "-o", str(output), "--keep", "5"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "2 archive(s) named shelfos-backup-*.tar.gz" in result.stdout
    assert result.stderr == ""
