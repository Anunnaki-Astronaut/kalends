"""Durable at-most-once admission. Reservations survive lost replies and restarts."""
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path

from .client import IndeterminateError
from .security import SecurityError


def operation_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value):
        raise ValueError('operation_id must be 1-100 ASCII letters, digits, underscores or hyphens')
    return value


class Operations:
    def __init__(self, path):
        self.path=Path(path)

    def check(self, identity):
        operation_id(identity)
        try:
            fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        try:
            if os.fstat(fd).st_mode & 0o077:
                raise SecurityError('operation store must be owner-only')
        finally: os.close(fd)
        db=sqlite3.connect(f'file:{self.path}?mode=ro',uri=True,timeout=2)
        try:
            if not db.execute("SELECT name FROM sqlite_master WHERE name='operations'").fetchone():return
            previous=db.execute('SELECT href FROM operations WHERE id=?',(identity,)).fetchone()
            if previous:
                error=IndeterminateError(previous[0]);error.operation_id=identity
                raise error
        finally: db.close()

    def reserve(self, identity, href):
        operation_id(identity)
        self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            if os.fstat(fd).st_mode & 0o077:
                raise SecurityError('operation store must be owner-only')
        finally: os.close(fd)
        db=sqlite3.connect(self.path,timeout=2)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, href TEXT NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT href FROM operations WHERE id=?',(identity,)).fetchone()
            if previous:
                error=IndeterminateError(previous[0]);error.operation_id=identity
                raise error
            if db.execute('SELECT count(*) FROM operations').fetchone()[0]>=10000:
                raise ValueError('operation ledger budget exhausted; reconcile before maintenance')
            db.execute('INSERT INTO operations (id, href) VALUES (?,?)',(identity,href))
            db.commit()
        finally: db.close()

    def release(self, identity):
        """Release only after transport proves the conditional write did not occur."""
        operation_id(identity)
        try:
            fd=os.open(self.path,os.O_RDWR|os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        try:
            if os.fstat(fd).st_mode & 0o077:
                raise SecurityError('operation store must be owner-only')
        finally:
            os.close(fd)
        db=sqlite3.connect(self.path,timeout=2)
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM operations WHERE id=?',(identity,))
            db.commit()
        finally:
            db.close()

    @contextmanager
    def _move_db(self, *, create=False, readonly=False):
        """Use the shared ledger, with the same owner-only boundary as CRUD."""
        if create:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = (os.O_RDONLY if readonly else os.O_RDWR) | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(self.path, flags | (os.O_CREAT if create else 0), 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or info.st_uid != os.getuid() or info.st_nlink != 1):
                raise SecurityError('operation store must be an owner-only regular file')
            db = sqlite3.connect(self.path.absolute().as_uri() +
                                 ('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            try:
                if not readonly:
                    db.execute('PRAGMA synchronous=FULL')
                yield db
            finally:
                db.close()
        finally:
            os.close(fd)

    @staticmethod
    def _matching_move(row, fingerprint):
        if row is None:
            return None
        row = dict(row)
        if row.get('move_fingerprint') != fingerprint:
            raise ValueError('operation_id already belongs to a different request')
        return row

    def find_move(self, identity, fingerprint):
        operation_id(identity)
        try:
            with self._move_db(readonly=True) as db:
                if not db.execute("SELECT name FROM sqlite_master WHERE name='operations'").fetchone():
                    return None
                return self._matching_move(db.execute(
                    'SELECT * FROM operations WHERE id=?', (identity,)).fetchone(), fingerprint)
        except FileNotFoundError:
            return None

    def reserve_move(self, identity, href, fingerprint, digest):
        """Atomic admission; only the inserting caller is allowed to write."""
        operation_id(identity)
        with self._move_db(create=True) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, href TEXT NOT NULL)')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(operations)')}
            for column in ('move_fingerprint', 'move_digest', 'move_phase'):
                if column not in columns:
                    db.execute(f'ALTER TABLE operations ADD COLUMN {column} TEXT')
            previous = self._matching_move(db.execute(
                'SELECT * FROM operations WHERE id=?', (identity,)).fetchone(), fingerprint)
            if previous is not None:
                return previous
            if db.execute('SELECT count(*) FROM operations').fetchone()[0] >= 10000:
                raise ValueError('operation ledger budget exhausted; reconcile before maintenance')
            db.execute('INSERT INTO operations (id, href, move_fingerprint, move_digest, move_phase) '
                       'VALUES (?, ?, ?, ?, ?)', (identity, href, fingerprint, digest, 'put_intent'))
            db.commit()
            return None

    def move_phase(self, identity, fingerprint, expected, phase):
        with self._move_db() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute('UPDATE operations SET move_phase=? WHERE id=? '
                                 'AND move_fingerprint=? AND move_phase=?',
                                 (phase, identity, fingerprint, expected)).rowcount
            if changed != 1:
                raise ValueError('move phase changed; reconcile without retrying writes')
            db.commit()
