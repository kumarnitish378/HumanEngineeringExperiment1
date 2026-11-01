"""SQLite persistence for the library.

Every change happens inside transaction(): it is either committed whole or
rolled back whole. No application state is cached in memory, so a failed write
cannot leave memory and disk disagreeing. The schema also enforces the key
rules itself, so a bug in the code above cannot store an invalid loan.
"""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import date

from errors import CorruptedDataError, StorageError
from models import Book, Copy, Loan, Member

SCHEMA_VERSION = 1
BUSY_TIMEOUT_SECONDS = 5
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


class SqliteStore:
    """Reads and writes library records in one SQLite database."""

    def __init__(self, path=":memory:"):
        self.path = path
        # One connection is shared, so the lock also stops two threads from
        # interleaving statements inside the same transaction.
        self._lock = threading.RLock()
        self._connection = None
        try:
            self._connection = sqlite3.connect(
                path,
                timeout=BUSY_TIMEOUT_SECONDS,
                isolation_level=None,  # transactions are managed explicitly
                check_same_thread=False,
            )
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._create_schema_if_new()
            self._verify()
        except sqlite3.OperationalError as error:
            self.close()
            raise StorageError(f"Cannot open '{path}': {error}") from error
        except sqlite3.DatabaseError as error:
            self.close()
            raise CorruptedDataError(
                f"'{path}' is corrupted or is not a library database: {error}"
            ) from error
        except CorruptedDataError:
            self.close()
            raise

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

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
            self._run(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            for table in TABLES
        )

    # Books and copies

    def next_book_id(self):
        return f"B{self._next_number('books')}"

    def add_book(self, book):
        self._run("INSERT INTO books VALUES (?, ?, ?)",
                  (book.id, book.title, book.author))

    def get_book(self, book_id):
        row = self._run("SELECT id, title, author FROM books WHERE id = ?",
                        (book_id,)).fetchone()
        return Book(*row) if row else None

    def list_books(self):
        rows = self._run("SELECT id, title, author FROM books "
                         "ORDER BY CAST(substr(id, 2) AS INTEGER)")
        return [Book(*row) for row in rows.fetchall()]

    def add_copy(self, copy):
        self._run("INSERT INTO copies VALUES (?, ?)", (copy.id, copy.book_id))

    def get_copy(self, copy_id):
        row = self._run("SELECT id, book_id FROM copies WHERE id = ?",
                        (copy_id,)).fetchone()
        return Copy(*row) if row else None

    def list_copies(self, book_id):
        rows = self._run(
            "SELECT id, book_id FROM copies WHERE book_id = ? ORDER BY rowid",
            (book_id,))
        return [Copy(*row) for row in rows.fetchall()]

    def list_available_copies(self, book_id):
        rows = self._run(
            """SELECT id, book_id FROM copies WHERE book_id = ? AND id NOT IN (
                   SELECT copy_id FROM loans
                   WHERE returned_on IS NULL AND copy_id IS NOT NULL
               ) ORDER BY rowid""",
            (book_id,))
        return [Copy(*row) for row in rows.fetchall()]

    # Members

    def next_member_id(self):
        return f"M{self._next_number('members')}"

    def add_member(self, member):
        self._run("INSERT INTO members VALUES (?, ?)",
                  (member.id, member.name))

    def get_member(self, member_id):
        row = self._run("SELECT id, name FROM members WHERE id = ?",
                        (member_id,)).fetchone()
        return Member(*row) if row else None

    def list_members(self):
        rows = self._run("SELECT id, name FROM members "
                         "ORDER BY CAST(substr(id, 2) AS INTEGER)")
        return [Member(*row) for row in rows.fetchall()]

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
        rows = self._run(
            "SELECT id, book_id, copy_id, member_id, issued_on, returned_on "
            f"FROM loans WHERE {condition} ORDER BY id", params)
        return [self._to_loan(row) for row in rows.fetchall()]

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
        row = self._run("SELECT COALESCE(MAX(CAST(substr(id, 2) AS INTEGER)), 0) "
                        f"FROM {table}").fetchone()
        return row[0] + 1

    def _run(self, sql, params=()):
        with self._lock:
            if self._connection is None:
                raise StorageError("The data store is closed.")
            try:
                return self._connection.execute(sql, params)
            except sqlite3.Error as error:
                raise StorageError(f"Could not access data: {error}") from error

    def _rollback(self):
        if self._connection is not None and self._connection.in_transaction:
            try:
                self._connection.execute("ROLLBACK")
            except sqlite3.Error as error:
                raise StorageError(f"Rollback failed: {error}") from error

    def _create_schema_if_new(self):
        connection = self._connection
        if self._schema_version() != 0:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            # Checked again under the lock in case another process just did this.
            if self._schema_version() == 0 and not self._table_names():
                for statement in SCHEMA:
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
