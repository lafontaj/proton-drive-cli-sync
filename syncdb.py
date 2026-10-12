"""SQLite sync database, one file per mappings file.

The path comes from ``config.DATA_DIR`` (or an explicit directory). This
module does not call the Proton CLI and does not decide uploads or downloads.
"""

import os
import sqlite3


STATES = (
    "synced",
    "pending-up",
    "pending-down",
    "conflict",
    "error",
    "unsynced",
)

_COLUMNS = (
    "local_path",
    "remote_path",
    "remote_node_id",
    "sha1",
    "claimed_size",
    "claimed_mtime",
    "local_inode",
    "local_mtime",
    "state",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    local_path TEXT PRIMARY KEY,
    remote_path TEXT NOT NULL,
    remote_node_id TEXT,
    sha1 TEXT,
    claimed_size INTEGER,
    claimed_mtime REAL,
    local_inode INTEGER,
    local_mtime REAL,
    state TEXT NOT NULL CHECK (
        state IN ('synced', 'pending-up', 'pending-down', 'conflict', 'error', 'unsynced')
    )
);
CREATE INDEX IF NOT EXISTS files_by_remote ON files (remote_path);
CREATE INDEX IF NOT EXISTS files_by_state ON files (state);
"""

_FILES_V2 = """
CREATE TABLE files_v2 (
    local_path TEXT PRIMARY KEY,
    remote_path TEXT NOT NULL,
    remote_node_id TEXT,
    sha1 TEXT,
    claimed_size INTEGER,
    claimed_mtime REAL,
    local_inode INTEGER,
    local_mtime REAL,
    state TEXT NOT NULL CHECK (
        state IN ('synced', 'pending-up', 'pending-down', 'conflict', 'error', 'unsynced')
    )
);
"""


def database_path(mappings_file, data_dir=None):
    """Path of the SQLite file for ``mappings_file``.

    ``data_dir`` defaults to ``config.DATA_DIR``. The file name follows the
    cache: ``mappings-user1.json`` becomes ``mappings-user1.sync.sqlite``.
    """
    if data_dir is None:
        import config as appconfig
        data_dir = appconfig.DATA_DIR
    base = os.path.basename(os.fspath(mappings_file))
    if base.lower().endswith(".json"):
        base = base[:-5]
    if not base or base in (".", ".."):
        raise ValueError("mappings file name is empty")
    return os.path.join(os.fspath(data_dir), base + ".sync.sqlite")


def _optional_text(value):
    if isinstance(value, str) and value:
        return value
    return None


def _optional_int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


class SyncDB:
    """Rows for files this mappings file has already reconciled."""

    def __init__(self, path):
        parent = os.path.dirname(os.fspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.path = os.fspath(path)
        self._conn = sqlite3.connect(self.path, timeout=5)
        try:
            self._ensure()
        except Exception:
            self._conn.close()
            raise

    def _ensure(self):
        self._conn.executescript(_SCHEMA)
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', '2')"
            )
            self._conn.commit()
            return
        if row[0] == "1":
            self._migrate_unsynced()
            return
        if row[0] != "2":
            raise ValueError("unsupported sync database version")

    def _migrate_unsynced(self):
        """Rebuild the file table so ``unsynced`` passes the state check."""
        self._conn.executescript(
            _FILES_V2 + """
            INSERT INTO files_v2
                SELECT local_path, remote_path, remote_node_id, sha1,
                       claimed_size, claimed_mtime, local_inode, local_mtime, state
                FROM files;
            DROP TABLE files;
            ALTER TABLE files_v2 RENAME TO files;
            CREATE INDEX IF NOT EXISTS files_by_remote ON files (remote_path);
            CREATE INDEX IF NOT EXISTS files_by_state ON files (state);
            UPDATE meta SET value = '2' WHERE key = 'schema_version';
            """
        )
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def get(self, local_path):
        row = self._conn.execute(
            "SELECT {cols} FROM files WHERE local_path = ?".format(
                cols=", ".join(_COLUMNS)
            ),
            (local_path,),
        ).fetchone()
        if row is None:
            return None
        return dict(zip(_COLUMNS, row))

    def rows(self):
        found = self._conn.execute(
            "SELECT {cols} FROM files".format(cols=", ".join(_COLUMNS))
        ).fetchall()
        return [dict(zip(_COLUMNS, row)) for row in found]

    def upsert(self, row):
        if not isinstance(row, dict):
            raise TypeError("row must be a dict")
        local_path = row.get("local_path")
        remote_path = row.get("remote_path")
        state = row.get("state")
        if not isinstance(local_path, str) or not local_path:
            raise ValueError("local_path is required")
        if not isinstance(remote_path, str) or not remote_path:
            raise ValueError("remote_path is required")
        if state not in STATES:
            raise ValueError("unknown state")
        values = (
            local_path,
            remote_path,
            _optional_text(row.get("remote_node_id")),
            _optional_text(row.get("sha1")),
            _optional_int(row.get("claimed_size")),
            _optional_float(row.get("claimed_mtime")),
            _optional_int(row.get("local_inode")),
            _optional_float(row.get("local_mtime")),
            state,
        )
        assignments = ", ".join(
            "{c} = excluded.{c}".format(c=column)
            for column in _COLUMNS
            if column != "local_path"
        )
        self._conn.execute(
            "INSERT INTO files ({cols}) VALUES ({marks}) "
            "ON CONFLICT(local_path) DO UPDATE SET {assignments}".format(
                cols=", ".join(_COLUMNS),
                marks=", ".join("?" for _ in _COLUMNS),
                assignments=assignments,
            ),
            values,
        )
        self._conn.commit()

    def delete(self, local_path):
        self._conn.execute(
            "DELETE FROM files WHERE local_path = ?",
            (local_path,),
        )
        self._conn.commit()
