"""SQLite persistence for the library.

Every change happens inside a transaction: it is either committed whole or
rolled back whole. No application state is cached in memory, so a failed write
cannot leave memory and disk disagreeing. The schema also enforces the key
rules itself, so a bug in the code above cannot store an invalid loan.

atomic() is the normal way to change data. It takes the write lock before the
first read, so all checks in the operation hold until the commit, and it reruns
the whole operation if the database stays locked. Only a whole operation can be
retried: a rolled-back transaction has no partial work left to resume.
"""

import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date

from errors import (
    CorruptedDataError,
    DatabaseBusyError,
    IntegrityViolationError,
    StorageError,
)
from models import Book, Copy, Loan, Member

BUSY_TIMEOUT_SECONDS = 5
RETRY_ATTEMPTS = 5
RETRY_BASE_DELAY_SECONDS = 0.05
TABLES = {"books", "copies", "members", "loans"}

SCHEMA = (
    """CREATE TABLE books (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL CHECK (length(trim(title)) > 0),
        author TEXT NOT NULL CHECK (length(trim(author)) > 0)
    )""",
    """CREATE TABLE copies (
        id TEXT PRIMARY KEY,
        book_id TEXT NOT NULL REFERENCES books (id),
        UNIQUE (id, book_id)
    )""",
    """CREATE TABLE members (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL CHECK (length(trim(name)) > 0)
    )""",
    # Dates use IS, not =, because date() gives NULL for invalid input and a
    # CHECK that evaluates to NULL passes.
    """CREATE TABLE loans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id TEXT NOT NULL REFERENCES books (id),
        copy_id TEXT,
        member_id TEXT NOT NULL REFERENCES members (id),
        issued_on TEXT NOT NULL CHECK (issued_on IS date(issued_on)),
        returned_on TEXT CHECK (
            returned_on IS NULL
            OR (returned_on IS date(returned_on) AND returned_on >= issued_on)
        ),
        CHECK (copy_id IS NOT NULL OR returned_on IS NOT NULL),
        FOREIGN KEY (copy_id, book_id) REFERENCES copies (id, book_id)
    )""",
    # A physical copy can only be out once, and a member can only hold one
    # copy of a title. The database refuses anything else.
    """CREATE UNIQUE INDEX one_active_loan_per_copy
        ON loans (copy_id) WHERE returned_on IS NULL""",
    """CREATE UNIQUE INDEX one_active_copy_per_member
        ON loans (book_id, member_id) WHERE returned_on IS NULL""",
    """CREATE TRIGGER loan_history_is_permanent BEFORE DELETE ON loans
        BEGIN SELECT RAISE(ABORT, 'loan history cannot be deleted'); END""",
)

# Each entry upgrades the schema by one version. A new database runs them too,
# so new and upgraded databases are always identical.
MIGRATIONS = (
    (
        # The only permitted change to a loan is closing it, once.
        """CREATE TRIGGER loan_history_is_immutable BEFORE UPDATE ON loans
            WHEN OLD.returned_on IS NOT NULL
                OR NEW.returned_on IS NULL
                OR NEW.id IS NOT OLD.id
                OR NEW.book_id IS NOT OLD.book_id
                OR NEW.copy_id IS NOT OLD.copy_id
                OR NEW.member_id IS NOT OLD.member_id
                OR NEW.issued_on IS NOT OLD.issued_on
            BEGIN SELECT RAISE(ABORT, 'loan history cannot be changed'); END""",
        # A copy ID always names one physical item, so it is never reused.
        """CREATE TRIGGER copy_id_matches_book BEFORE INSERT ON copies
            WHEN NEW.id NOT GLOB NEW.book_id || '-C[1-9]*'
            BEGIN SELECT RAISE(ABORT, 'copy ID does not match its book'); END""",
        """CREATE TRIGGER copies_cannot_be_deleted BEFORE DELETE ON copies
            BEGIN SELECT RAISE(ABORT, 'copies cannot be deleted'); END""",
        """CREATE TRIGGER copies_cannot_be_changed BEFORE UPDATE ON copies
            BEGIN SELECT RAISE(ABORT, 'copies cannot be changed'); END""",
    ),
)
SCHEMA_VERSION = 1 + len(MIGRATIONS)


class SqliteStore:
    """Reads and writes library records in one SQLite database."""

    def __init__(self, path=":memory:", busy_timeout=BUSY_TIMEOUT_SECONDS,
                 retry_attempts=RETRY_ATTEMPTS,
                 retry_base_delay=RETRY_BASE_DELAY_SECONDS,
                 sleep=time.sleep, connect=sqlite3.connect):
        """Opens path, creating or upgrading the schema if needed.

        busy_timeout is how long SQLite waits for a lock on each attempt.
        sleep and connect can be replaced to test failures deterministically.
        """
        self.path = path
        self._retry_attempts = retry_attempts
        self._retry_base_delay = retry_base_delay
        self._sleep = sleep
        # One connection is shared, so the lock also stops two threads from
        # interleaving statements inside the same transaction.
        self._lock = threading.RLock()
        self._connection = None
        try:
            self._connection = connect(
                path,
                timeout=busy_timeout,
                isolation_level=None,  # transactions are managed explicitly
                check_same_thread=False,
            )
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._retrying(self._prepare)
        except sqlite3.Error as error:
            self.close()
            raise _translate(error, f"Cannot open '{path}'") from error
        except StorageError:
            self.close()
            raise

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def atomic(self, operation):
        """Run operation in one transaction and return its result.

        If the database stays locked the whole operation runs again, up to
        the retry limit, so it must make no changes outside the store.
        """
        def attempt():
            with self.transaction():
                return operation()
        return self._retrying(attempt)

    @contextmanager
    def transaction(self):
        """Commit everything in the block, or roll all of it back on any error.

        BEGIN IMMEDIATE takes the write lock up front, so checks made inside
        the block stay true until the commit, even across processes.
        """
        with self._lock:
            self._run("BEGIN IMMEDIATE")
            try:
                yield
                self._run("COMMIT")
            except BaseException:
                self._rollback()
                raise

    def is_empty(self):
        return not any(
            self._query(f"SELECT 1 FROM {table} LIMIT 1") for table in TABLES
        )

    # Books and copies

    def next_book_id(self):
        return f"B{self._next_number('books')}"

    def add_book(self, book):
        self._run("INSERT INTO books VALUES (?, ?, ?)",
                  (book.id, book.title, book.author))

    def get_book(self, book_id):
        rows = self._query("SELECT id, title, author FROM books WHERE id = ?",
                           (book_id,))
        return Book(*rows[0]) if rows else None

    def list_books(self):
        rows = self._query("SELECT id, title, author FROM books "
                           "ORDER BY CAST(substr(id, 2) AS INTEGER)")
        return [Book(*row) for row in rows]

    def add_copy(self, copy):
        self._run("INSERT INTO copies VALUES (?, ?)", (copy.id, copy.book_id))

    def get_copy(self, copy_id):
        rows = self._query("SELECT id, book_id FROM copies WHERE id = ?",
                           (copy_id,))
        return Copy(*rows[0]) if rows else None

    def list_copies(self, book_id):
        rows = self._query(
            "SELECT id, book_id FROM copies WHERE book_id = ? ORDER BY rowid",
            (book_id,))
        return [Copy(*row) for row in rows]

    def list_available_copies(self, book_id):
        rows = self._query(
            """SELECT id, book_id FROM copies WHERE book_id = ? AND id NOT IN (
                   SELECT copy_id FROM loans
                   WHERE returned_on IS NULL AND copy_id IS NOT NULL
               ) ORDER BY rowid""",
            (book_id,))
        return [Copy(*row) for row in rows]

    # Members

    def next_member_id(self):
        return f"M{self._next_number('members')}"

    def add_member(self, member):
        self._run("INSERT INTO members VALUES (?, ?)",
                  (member.id, member.name))

    def get_member(self, member_id):
        rows = self._query("SELECT id, name FROM members WHERE id = ?",
                           (member_id,))
        return Member(*rows[0]) if rows else None

    def list_members(self):
        rows = self._query("SELECT id, name FROM members "
                           "ORDER BY CAST(substr(id, 2) AS INTEGER)")
        return [Member(*row) for row in rows]

    # Loans

    def add_loan(self, book_id, copy_id, member_id, issued_on, returned_on=None):
        cursor = self._run(
            "INSERT INTO loans (book_id, copy_id, member_id, issued_on, "
            "returned_on) VALUES (?, ?, ?, ?, ?)",
            (book_id, copy_id, member_id, issued_on.isoformat(),
             returned_on.isoformat() if returned_on else None))
        return Loan(cursor.lastrowid, book_id, copy_id, member_id, issued_on,
                    returned_on)

    def mark_returned(self, loan_id, returned_on):
        cursor = self._run(
            "UPDATE loans SET returned_on = ? "
            "WHERE id = ? AND returned_on IS NULL",
            (returned_on.isoformat(), loan_id))
        if cursor.rowcount != 1:
            raise StorageError(f"Loan {loan_id} is not active.")

    def get_loan(self, loan_id):
        loans = self._select_loans("id = ?", (loan_id,))
        return loans[0] if loans else None

    def active_loan_for_copy(self, copy_id):
        loans = self._select_loans("copy_id = ? AND returned_on IS NULL",
                                   (copy_id,))
        return loans[0] if loans else None

    def active_loans_for_book(self, book_id):
        return self._select_loans("book_id = ? AND returned_on IS NULL",
                                  (book_id,))

    def active_loan_for_member(self, book_id, member_id):
        loans = self._select_loans(
            "book_id = ? AND member_id = ? AND returned_on IS NULL",
            (book_id, member_id))
        return loans[0] if loans else None

    def list_active_loans(self):
        return self._select_loans("returned_on IS NULL")

    def list_loans(self, book_id=None, member_id=None):
        conditions = ["1 = 1"]
        params = []
        if book_id is not None:
            conditions.append("book_id = ?")
            params.append(book_id)
        if member_id is not None:
            conditions.append("member_id = ?")
            params.append(member_id)
        return self._select_loans(" AND ".join(conditions), tuple(params))

    # Internals

    def _select_loans(self, condition, params=()):
        rows = self._query(
            "SELECT id, book_id, copy_id, member_id, issued_on, returned_on "
            f"FROM loans WHERE {condition} ORDER BY id", params)
        return [self._to_loan(row) for row in rows]

    @staticmethod
    def _to_loan(row):
        loan_id, book_id, copy_id, member_id, issued_on, returned_on = row
        try:
            return Loan(
                loan_id, book_id, copy_id, member_id,
                date.fromisoformat(issued_on),
                date.fromisoformat(returned_on) if returned_on else None)
        except (TypeError, ValueError) as error:
            raise CorruptedDataError(
                f"Loan {loan_id} has an invalid date: {error}") from error

    def _next_number(self, table):
        rows = self._query(
            "SELECT COALESCE(MAX(CAST(substr(id, 2) AS INTEGER)), 0) "
            f"FROM {table}")
        return rows[0][0] + 1

    def _run(self, sql, params=()):
        """Execute one statement that returns no rows the caller needs."""
        with self._lock:
            return self._execute(sql, params)

    def _query(self, sql, params=()):
        """Return all rows of one read.

        Rows are fetched under the lock, because a cursor read later could run
        in the middle of another thread's transaction on this connection.
        """
        def fetch():
            with self._lock:
                try:
                    return self._execute(sql, params).fetchall()
                except sqlite3.Error as error:
                    raise _translate(error, "Could not read data") from error

        connection = self._connection
        if connection is not None and connection.in_transaction:
            return fetch()  # the transaction already holds the lock it needs
        return self._retrying(fetch)

    def _execute(self, sql, params):
        if self._connection is None:
            raise StorageError("The data store is closed.")
        try:
            return self._connection.execute(sql, params)
        except sqlite3.Error as error:
            raise _translate(error, "Could not access data") from error

    def _retrying(self, attempt):
        """Call attempt, backing off and repeating while the database is busy."""
        for attempt_number in range(self._retry_attempts - 1):
            try:
                return attempt()
            except DatabaseBusyError:
                # Random delays stop competing processes retrying in lockstep.
                limit = self._retry_base_delay * 2 ** attempt_number
                self._sleep(random.uniform(0, limit))
        return attempt()

    def _rollback(self):
        if self._connection is None or not self._connection.in_transaction:
            return
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error as error:
            # Closing discards the open transaction. Keeping the connection
            # would leave later work inside a transaction that cannot commit.
            self.close()
            raise _translate(error, "Rollback failed") from error

    def _prepare(self):
        """Create or upgrade the schema if needed, then check the data."""
        try:
            if self._schema_version() < SCHEMA_VERSION:
                self._upgrade_schema()
            self._verify()
        except sqlite3.Error as error:
            raise _translate(error, f"Cannot open '{self.path}'") from error

    def _upgrade_schema(self):
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            # Read under the lock in case another process just did this.
            version = self._schema_version()
            if version == 0 and not self._table_names():
                for statement in SCHEMA:
                    connection.execute(statement)
                version = 1
            if version > 0:
                for migration in MIGRATIONS[version - 1:]:
                    for statement in migration:
                        connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise

    def _verify(self):
        version = self._schema_version()
        if version != SCHEMA_VERSION:
            raise CorruptedDataError(
                f"'{self.path}' has schema version {version}; "
                f"this program expects {SCHEMA_VERSION}.")
        missing = TABLES - self._table_names()
        if missing:
            raise CorruptedDataError(
                f"'{self.path}' is missing tables: {sorted(missing)}.")
        connection = self._connection
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise CorruptedDataError(
                f"'{self.path}' failed its integrity check: {integrity[0][0]}")
        broken_links = connection.execute("PRAGMA foreign_key_check").fetchall()
        if broken_links:
            raise CorruptedDataError(
                f"'{self.path}' has {len(broken_links)} broken record links.")

    def _schema_version(self):
        return self._connection.execute("PRAGMA user_version").fetchone()[0]

    def _table_names(self):
        rows = self._connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()
        return {row[0] for row in rows}


def _translate(error, context):
    """Turn a sqlite3 error into the specific exception callers can act on."""
    code = getattr(error, "sqlite_errorcode", None)
    primary_code = None if code is None else code & 0xFF
    message = f"{context}: {error}"
    if primary_code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return DatabaseBusyError(f"{message}. Please try again.")
    if primary_code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
        return CorruptedDataError(f"{message}. The data may be damaged.")
    if isinstance(error, sqlite3.IntegrityError):
        return IntegrityViolationError(message)
    return StorageError(message)
