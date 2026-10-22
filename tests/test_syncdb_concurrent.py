"""Two SyncDB instances on the same file: WAL, busy timeout, no lock error."""

import sqlite3
import threading

import syncdb


def _row(n, who):
    return {"local_path": "/l/{}-{}".format(who, n),
            "remote_path": "/r/{}-{}".format(who, n), "state": "synced"}


def test_pragmas_are_set(tmp_path):
    with syncdb.SyncDB(tmp_path / "s.db") as db:
        assert db._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_two_instances_write_without_database_is_locked(tmp_path):
    path = tmp_path / "s.db"
    a, b = syncdb.SyncDB(path), syncdb.SyncDB(path)
    errors = []

    def work(db, who):
        try:
            for n in range(100):
                db.upsert(_row(n, who))
                db.rows()
        except sqlite3.OperationalError as exc:
            errors.append(exc)

    # sqlite3 connections are bound to their creating thread: each thread
    # opens its own instance on the shared file.
    a.close()
    b.close()

    def run(who):
        with syncdb.SyncDB(path) as db:
            work(db, who)

    threads = [threading.Thread(target=run, args=(w,)) for w in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with syncdb.SyncDB(path) as db:
        assert len(db.rows()) == 200
