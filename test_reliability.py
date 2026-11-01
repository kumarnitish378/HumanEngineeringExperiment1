"""Concurrency, failure injection and recovery tests.

Threads and processes wait on barriers and events, never on timed sleeps, so
each race starts at the same moment and each failure happens at a known point.
"""

import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from datetime import date

from errors import (
    AlreadyPopulatedError,
    CorruptedDataError,
    DatabaseBusyError,
    IntegrityViolationError,
    NotOnLoanError,
    StorageError,
    UnavailableError,
    ValidationError,
    WrongMemberError,
)
from legacy_import import import_legacy_json
from library import Library
from models import Book
from storage import MIGRATIONS, SCHEMA, SCHEMA_VERSION, SqliteStore

PROJECT = os.path.dirname(os.path.abspath(__file__))
SYNC_TIMEOUT_SECONDS = 120
LONG_BUSY_TIMEOUT_SECONDS = 30

PROCESS_CONTEXT = multiprocessing.get_context("spawn")


def dump(path):
    """Return the full contents of the database, for exact comparison."""
    connection = sqlite3.connect(path)
    try:
        return {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("books", "copies", "members", "loans")
        }
    finally:
        connection.close()


def invariant_violations(path):
    """Return every broken invariant, read directly from the file."""
    connection = sqlite3.connect(path)
    queries = {
        "copy with two active loans":
            "SELECT copy_id FROM loans WHERE returned_on IS NULL "
            "GROUP BY copy_id HAVING COUNT(*) > 1",
        "member with two copies of a title":
            "SELECT book_id FROM loans WHERE returned_on IS NULL "
            "GROUP BY book_id, member_id HAVING COUNT(*) > 1",
        "loan copy belongs to another book":
            "SELECT l.id FROM loans l JOIN copies c ON c.id = l.copy_id "
            "WHERE c.book_id <> l.book_id",
        "active loan without a copy":
            "SELECT id FROM loans WHERE copy_id IS NULL AND returned_on IS NULL",
        "returned before issue":
            "SELECT id FROM loans WHERE returned_on < issued_on",
        "book without copies":
            "SELECT b.id FROM books b LEFT JOIN copies c ON c.book_id = b.id "
            "WHERE c.id IS NULL",
    }
    try:
        problems = [name for name, sql in queries.items()
                    if connection.execute(sql).fetchall()]
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            problems.append("integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            problems.append("foreign key check failed")
        return problems
    finally:
        connection.close()


class FaultyConnection:
    """Wraps a real connection and fails one chosen statement.

    SQLite cannot be told to fail a COMMIT on demand, so this sits between the
    store and the driver, and raises what the driver itself would raise.
    """

    def __init__(self, connection):
        self._connection = connection
        self.should_fail = lambda sql: None
        self.statements = 0

    def execute(self, sql, params=()):
        self.statements += 1
        failure = self.should_fail(sql)
        if failure is not None:
            raise failure
        return self._connection.execute(sql, params)

    @property
    def in_transaction(self):
        return self._connection.in_transaction

    def close(self):
        self._connection.close()


def sqlite_failure(code, message):
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    return error


class ReliabilityTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, "library.db")

    def open_store(self, **options):
        options.setdefault("busy_timeout", LONG_BUSY_TIMEOUT_SECONDS)
        store = SqliteStore(self.path, **options)
        self.addCleanup(store.close)
        return store

    def seed(self, members=2, copies=2):
        with SqliteStore(self.path) as store:
            library = Library(store)
            library.add_book("Dune", "Frank Herbert", copies=copies)
            for number in range(members):
                library.register_member(f"Member {number}")

    def faulty_store(self, **options):
        wrappers = []

        def connect(*args, **kwargs):
            wrappers.append(FaultyConnection(sqlite3.connect(*args, **kwargs)))
            return wrappers[-1]

        return self.open_store(connect=connect, **options), wrappers[0]

    def assert_consistent(self):
        self.assertEqual(invariant_violations(self.path), [])

    def hold_write_lock(self):
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        self.addCleanup(connection.close)
        return connection


class ThreadConcurrencyTest(ReliabilityTestCase):
    def race(self, count, work):
        """Run work(index) in count threads released at the same moment."""
        barrier = threading.Barrier(count)
        results = [None] * count

        def run(index):
            barrier.wait(SYNC_TIMEOUT_SECONDS)
            try:
                results[index] = work(index)
            except Exception as error:  # the assertions inspect each outcome
                results[index] = error

        threads = [threading.Thread(target=run, args=(index,))
                   for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(SYNC_TIMEOUT_SECONDS)
        return results

    def assert_single_winner(self, results):
        winners = [r for r in results if not isinstance(r, Exception)]
        losers = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(winners), 1, results)
        for loser in losers:
            self.assertIsInstance(loser, UnavailableError)

    def test_25_threads_sharing_a_connection_issue_one_copy(self):
        self.seed(members=25, copies=1)
        library = Library(self.open_store())
        results = self.race(
            25, lambda i: library.issue_book("B1", f"M{i + 1}", "B1-C1"))
        self.assert_single_winner(results)
        self.assertEqual(len(library.active_loans()), 1)
        self.assert_consistent()

    def test_25_independent_connections_issue_one_copy(self):
        self.seed(members=25, copies=1)
        libraries = [Library(self.open_store()) for _ in range(25)]
        results = self.race(
            25, lambda i: libraries[i].issue_book("B1", f"M{i + 1}", "B1-C1"))
        self.assert_single_winner(results)
        self.assertEqual(len(dump(self.path)["loans"]), 1)
        self.assert_consistent()

    def test_auto_selected_copies_never_collide(self):
        self.seed(members=20, copies=5)
        libraries = [Library(self.open_store()) for _ in range(20)]
        results = self.race(20, lambda i: libraries[i].issue_book("B1", f"M{i + 1}"))
        winners = [r for r in results if not isinstance(r, Exception)]
        self.assertEqual(len(winners), 5, results)
        self.assertEqual(len({loan.copy_id for loan in winners}), 5)
        self.assert_consistent()

    def test_100_books_and_100_members_on_a_shared_connection(self):
        library = Library(self.open_store())

        def create(index):
            return (library.add_book(f"Book {index}", "Author", copies=2),
                    library.register_member(f"Member {index}"))

        results = self.race(100, create)
        self.assert_all_ids_unique(results)

    def test_100_books_and_100_members_on_independent_connections(self):
        libraries = [Library(self.open_store()) for _ in range(10)]

        def create(index):
            library = libraries[index % 10]
            return (library.add_book(f"Book {index}", "Author", copies=2),
                    library.register_member(f"Member {index}"))

        results = self.race(100, create)
        self.assert_all_ids_unique(results)

    def assert_all_ids_unique(self, results):
        for result in results:
            self.assertNotIsInstance(result, Exception)
        expected_ids = {f"B{n}" for n in range(1, 101)}
        self.assertEqual({book.id for book, _ in results}, expected_ids)
        self.assertEqual({member.id for _, member in results},
                         {f"M{n}" for n in range(1, 101)})
        data = dump(self.path)
        self.assertEqual((len(data["books"]), len(data["members"])), (100, 100))
        self.assertEqual(len(data["copies"]), 200)
        self.assert_consistent()

    def test_concurrent_add_copies_gives_unique_copy_ids(self):
        self.seed(copies=1)
        libraries = [Library(self.open_store()) for _ in range(20)]
        results = self.race(20, lambda i: libraries[i].add_copies("B1", 2))
        ids = [copy.id for result in results for copy in result]
        self.assertEqual(len(set(ids)), 40)
        self.assertEqual(len(dump(self.path)["copies"]), 41)

    def test_only_the_borrower_wins_concurrent_returns(self):
        self.seed(members=20, copies=1)
        libraries = [Library(self.open_store()) for _ in range(20)]
        libraries[0].issue_book("B1", "M1")

        def attempt(index):
            return libraries[index].return_book("B1", f"M{index + 1}")

        results = self.race(20, attempt)
        self.assertEqual(results[0].member_id, "M1")
        for result in results[1:]:
            self.assertIsInstance(result, (WrongMemberError, NotOnLoanError))
        self.assertEqual(len(dump(self.path)["loans"]), 1)
        self.assert_consistent()

    def test_first_opens_racing_create_one_schema(self):
        results = self.race(20, lambda _: self.open_store())
        for result in results:
            self.assertIsInstance(result, SqliteStore)

    def test_readers_never_see_a_transaction_in_progress(self):
        store = self.open_store()
        library = Library(store)
        library.add_book("Dune", "Frank Herbert")
        in_transaction, finish = threading.Event(), threading.Event()
        counts = []

        def writer():
            def change():
                store.add_book(Book("B2", "T", "A"))
                in_transaction.set()
                finish.wait(SYNC_TIMEOUT_SECONDS)
                raise RuntimeError("abandon this transaction")

            with self.assertRaises(RuntimeError):
                store.atomic(change)

        def reader():
            counts.append(len(store.list_books()))

        write_thread = threading.Thread(target=writer)
        write_thread.start()
        self.assertTrue(in_transaction.wait(SYNC_TIMEOUT_SECONDS))
        read_thread = threading.Thread(target=reader)
        read_thread.start()
        finish.set()
        write_thread.join(SYNC_TIMEOUT_SECONDS)
        read_thread.join(SYNC_TIMEOUT_SECONDS)
        self.assertEqual(counts, [1])


def issue_worker(path, member_id, barrier, results):
    with SqliteStore(path, busy_timeout=LONG_BUSY_TIMEOUT_SECONDS) as store:
        library = Library(store)
        barrier.wait(SYNC_TIMEOUT_SECONDS)
        try:
            results.put(("won", library.issue_book("B1", member_id, "B1-C1").copy_id))
        except UnavailableError:
            results.put(("lost", member_id))
        except Exception as error:
            results.put(("error", repr(error)))


def create_worker(path, worker, count, barrier, results):
    with SqliteStore(path, busy_timeout=LONG_BUSY_TIMEOUT_SECONDS) as store:
        library = Library(store)
        barrier.wait(SYNC_TIMEOUT_SECONDS)
        try:
            for number in range(count):
                book = library.add_book(f"Book {worker}.{number}", "Author", copies=2)
                member = library.register_member(f"Member {worker}.{number}")
                results.put((book.id, member.id))
        except Exception as error:
            results.put(("error", repr(error)))


def open_worker(path, barrier, results):
    barrier.wait(SYNC_TIMEOUT_SECONDS)
    try:
        with SqliteStore(path, busy_timeout=LONG_BUSY_TIMEOUT_SECONDS):
            results.put("ok")
    except Exception as error:
        results.put(repr(error))


def hold_transaction_worker(path, locked):
    """Take the lock, write, then wait forever to be killed mid-transaction."""
    store = SqliteStore(path)

    def change():
        store.add_book(Book("B2", "Emma", "Austen"))
        locked.set()
        threading.Event().wait()

    store.atomic(change)


class ProcessConcurrencyTest(ReliabilityTestCase):
    def run_processes(self, target, argument_lists):
        processes = [PROCESS_CONTEXT.Process(target=target, args=arguments)
                     for arguments in argument_lists]
        for process in processes:
            process.start()
        return processes

    def collect(self, processes, results, expected):
        collected = [results.get(timeout=SYNC_TIMEOUT_SECONDS)
                     for _ in range(expected)]
        for process in processes:
            process.join(SYNC_TIMEOUT_SECONDS)
            self.assertEqual(process.exitcode, 0)
        return collected

    def test_20_processes_issue_one_copy(self):
        self.seed(members=20, copies=1)
        barrier = PROCESS_CONTEXT.Barrier(20)
        results = PROCESS_CONTEXT.Queue()
        processes = self.run_processes(issue_worker, [
            (self.path, f"M{n}", barrier, results) for n in range(1, 21)])
        outcomes = self.collect(processes, results, 20)

        self.assertEqual(sorted(kind for kind, _ in outcomes),
                         ["lost"] * 19 + ["won"], outcomes)
        self.assertEqual(len(dump(self.path)["loans"]), 1)
        self.assert_consistent()

    def test_4_processes_create_100_books_and_100_members(self):
        barrier = PROCESS_CONTEXT.Barrier(4)
        results = PROCESS_CONTEXT.Queue()
        processes = self.run_processes(create_worker, [
            (self.path, worker, 25, barrier, results) for worker in range(4)])
        outcomes = self.collect(processes, results, 100)

        self.assertEqual({book for book, _ in outcomes},
                         {f"B{n}" for n in range(1, 101)}, outcomes)
        self.assertEqual({member for _, member in outcomes},
                         {f"M{n}" for n in range(1, 101)})
        data = dump(self.path)
        self.assertEqual(len(data["copies"]), 200)
        self.assert_consistent()

    def test_processes_racing_to_create_the_database(self):
        barrier = PROCESS_CONTEXT.Barrier(8)
        results = PROCESS_CONTEXT.Queue()
        processes = self.run_processes(open_worker, [
            (self.path, barrier, results)] * 8)
        self.assertEqual(self.collect(processes, results, 8), ["ok"] * 8)

    def test_process_killed_while_holding_the_lock_releases_it(self):
        self.seed(copies=1)
        before = dump(self.path)
        locked = PROCESS_CONTEXT.Event()
        [process] = self.run_processes(
            hold_transaction_worker, [(self.path, locked)])
        self.addCleanup(process.kill)
        self.assertTrue(locked.wait(SYNC_TIMEOUT_SECONDS))

        waiter = self.open_store(busy_timeout=0, retry_attempts=1)
        library = Library(waiter)
        with self.assertRaises(DatabaseBusyError):
            library.register_member("Blocked")

        process.kill()
        process.join(SYNC_TIMEOUT_SECONDS)

        self.assertEqual(dump(self.path), before)
        self.assertEqual(library.issue_book("B1", "M1").copy_id, "B1-C1")
        self.assert_consistent()


class LockHandlingTest(ReliabilityTestCase):
    def test_lock_timeout_raises_a_specific_retryable_error(self):
        self.seed()
        before = dump(self.path)
        sleeps = []
        store = self.open_store(busy_timeout=0, retry_attempts=3,
                                sleep=sleeps.append)
        blocker = self.hold_write_lock()
        with self.assertRaises(DatabaseBusyError):
            Library(store).issue_book("B1", "M1")
        self.assertEqual(len(sleeps), 2)

        blocker.execute("ROLLBACK")
        self.assertEqual(dump(self.path), before)
        self.assertEqual(Library(store).issue_book("B1", "M1").copy_id, "B1-C1")

    def test_retry_succeeds_when_the_lock_clears(self):
        self.seed()
        blocker = self.hold_write_lock()
        store = self.open_store(
            busy_timeout=0, sleep=lambda _: blocker.execute("ROLLBACK"))
        loan = Library(store).issue_book("B1", "M1")
        self.assertEqual(loan.member_id, "M1")
        self.assertEqual(len(dump(self.path)["loans"]), 1)

    def test_retry_reruns_the_whole_operation_with_fresh_checks(self):
        self.seed(copies=1)
        rival = Library(self.open_store())

        def rival_takes_the_copy(_):
            rival.issue_book("B1", "M2", "B1-C1")

        store, connection = self.faulty_store(sleep=rival_takes_the_copy)
        failures = [sqlite_failure(sqlite3.SQLITE_BUSY, "database is locked")]
        connection.should_fail = lambda sql: (
            failures.pop() if sql == "COMMIT" and failures else None)

        with self.assertRaises(UnavailableError):
            Library(store).issue_book("B1", "M1", "B1-C1")
        self.assertEqual([row[3] for row in dump(self.path)["loans"]], ["M2"])

    def test_busy_commit_rolls_back_and_retries(self):
        self.seed()
        store, connection = self.faulty_store()
        failures = [sqlite_failure(sqlite3.SQLITE_BUSY, "database is locked")]
        connection.should_fail = lambda sql: (
            failures.pop() if sql == "COMMIT" and failures else None)
        Library(store).issue_book("B1", "M1")
        self.assertEqual(len(dump(self.path)["loans"]), 1)

    def test_reads_retry_while_busy(self):
        self.seed()
        store, connection = self.faulty_store(sleep=lambda _: None)
        failures = [sqlite_failure(sqlite3.SQLITE_BUSY, "database is locked")]
        connection.should_fail = lambda sql: (
            failures.pop() if sql.startswith("SELECT") and failures else None)
        self.assertEqual(len(Library(store).list_books()), 1)

    def test_upgrading_a_locked_database_reports_busy(self):
        self.seed()
        self.raw_downgrade()
        blocker = self.hold_write_lock()
        with self.assertRaises(DatabaseBusyError):
            SqliteStore(self.path, busy_timeout=0, retry_attempts=1)
        blocker.execute("ROLLBACK")

    def raw_downgrade(self):
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA user_version = 1")
        connection.close()

    def test_errors_while_fetching_rows_are_translated(self):
        self.seed()
        error = sqlite3.OperationalError("disk I/O error")
        error.sqlite_errorcode = sqlite3.SQLITE_IOERR

        class FailingCursor:
            def fetchall(self):
                raise error

        store, connection = self.faulty_store()
        real_execute = connection.execute

        def execute(sql, params=()):
            real_execute(sql, params)
            return FailingCursor()

        connection.execute = execute
        with self.assertRaises(StorageError):
            store.list_books()

    def test_non_busy_failures_are_not_retried(self):
        self.seed()
        sleeps = []
        store, connection = self.faulty_store(sleep=sleeps.append)
        connection.should_fail = lambda sql: (
            sqlite_failure(sqlite3.SQLITE_IOERR, "disk I/O error")
            if sql == "COMMIT" else None)
        with self.assertRaises(StorageError) as caught:
            Library(store).issue_book("B1", "M1")
        self.assertNotIsInstance(caught.exception, DatabaseBusyError)
        self.assertEqual(sleeps, [])


class FailureSweepTest(ReliabilityTestCase):
    """Fail every statement of each operation in turn.

    Whichever step fails, the database must match its previous contents
    exactly, and the same operation must then work.
    """

    def prepare(self):
        with SqliteStore(self.path) as store:
            library = Library(store)
            library.add_book("Dune", "Frank Herbert", copies=2)
            library.register_member("Ana")
            library.register_member("Ben")
            library.issue_book("B1", "M1", "B1-C1")

    OPERATIONS = {
        "add_book": lambda library: library.add_book("Emma", "Austen", copies=3),
        "add_copies": lambda library: library.add_copies("B1", 3),
        "register_member": lambda library: library.register_member("Carl"),
        "issue_book": lambda library: library.issue_book("B1", "M2"),
        "issue_specific": lambda library: library.issue_book("B1", "M2", "B1-C2"),
        "return_book": lambda library: library.return_book("B1", "M1"),
        "return_specific": lambda library: library.return_book("B1", "M1", "B1-C1"),
    }

    def sweep(self, make_failure, expected_error):
        self.prepare()
        for name, operation in self.OPERATIONS.items():
            before = dump(self.path)
            step = 0
            while True:
                step += 1
                store, connection = self.faulty_store()
                library = Library(store)
                counter = {"seen": 0}

                def fail_at_step(sql, target=step, counter=counter):
                    # Rollback must work: it is how the failure is handled.
                    if sql == "ROLLBACK":
                        return None
                    counter["seen"] += 1
                    return make_failure() if counter["seen"] == target else None

                connection.should_fail = fail_at_step
                try:
                    operation(library)
                except expected_error:
                    connection.should_fail = lambda sql: None
                    store.close()
                    with self.subTest(operation=name, step=step):
                        self.assertEqual(dump(self.path), before)
                        self.assert_consistent()
                    continue
                store.close()
                break
            # The operation has now run with no fault left to trigger.
            self.assertGreater(step, 2, name)
            self.assertNotEqual(dump(self.path), before, name)
            self.assert_consistent()
            self.reset()

    def reset(self):
        os.remove(self.path)
        self.prepare()

    def test_write_failure_at_every_step(self):
        self.sweep(lambda: sqlite_failure(sqlite3.SQLITE_IOERR, "disk I/O error"),
                   StorageError)

    def test_disk_full_at_every_step(self):
        self.sweep(lambda: sqlite_failure(sqlite3.SQLITE_FULL, "disk is full"),
                   StorageError)

    def test_unexpected_exception_at_every_step(self):
        self.sweep(lambda: RuntimeError("unexpected"), RuntimeError)

    def test_interrupt_at_every_step(self):
        self.sweep(KeyboardInterrupt, KeyboardInterrupt)


class TransactionIntegrityTest(ReliabilityTestCase):
    def test_commit_failure_rolls_back_each_operation(self):
        self.seed()
        Library(self.open_store()).issue_book("B1", "M1")
        before = dump(self.path)
        store, connection = self.faulty_store()
        library = Library(store)
        connection.should_fail = lambda sql: (
            sqlite_failure(sqlite3.SQLITE_IOERR, "disk I/O error")
            if sql == "COMMIT" else None)

        for operation in (lambda: library.issue_book("B1", "M2"),
                          lambda: library.return_book("B1", "M1"),
                          lambda: library.add_book("T", "A", copies=2),
                          lambda: library.register_member("Carl")):
            with self.assertRaises(StorageError):
                operation()
            self.assertEqual(dump(self.path), before)
            self.assertFalse(connection.in_transaction)

    def test_failed_return_leaves_history_identical(self):
        self.seed()
        store, connection = self.faulty_store()
        library = Library(store)
        library.issue_book("B1", "M1")
        history = library.loan_history()
        connection.should_fail = lambda sql: (
            sqlite_failure(sqlite3.SQLITE_IOERR, "disk I/O error")
            if sql.startswith("UPDATE loans") else None)

        with self.assertRaises(StorageError):
            library.return_book("B1", "M1")
        self.assertEqual(library.loan_history(), history)
        self.assertEqual(library.available_copies("B1"), 1)

    def test_failed_issue_leaves_the_copy_available(self):
        self.seed()
        store, connection = self.faulty_store()
        library = Library(store)
        connection.should_fail = lambda sql: (
            sqlite_failure(sqlite3.SQLITE_IOERR, "disk I/O error")
            if sql.startswith("INSERT INTO loans") else None)

        with self.assertRaises(StorageError):
            library.issue_book("B1", "M1")
        connection.should_fail = lambda sql: None
        self.assertEqual(library.available_copies("B1"), 2)
        self.assertEqual(library.loan_history(), [])

    def test_failed_rollback_closes_the_connection_and_discards_the_work(self):
        self.seed()
        store, connection = self.faulty_store()
        connection.should_fail = lambda sql: (
            sqlite_failure(sqlite3.SQLITE_IOERR, "disk I/O error")
            if sql in ("COMMIT", "ROLLBACK") else None)

        with self.assertRaises(StorageError):
            Library(store).issue_book("B1", "M1")
        with self.assertRaises(StorageError) as caught:
            store.list_books()
        self.assertIn("closed", str(caught.exception))
        self.assertEqual(dump(self.path)["loans"], [])

    def test_legacy_import_race_imports_exactly_once(self):
        legacy = os.path.join(os.path.dirname(self.path), "library.json")
        with open(legacy, "w", encoding="utf-8") as file:
            file.write('{"books": [], "members": [{"id": "M1", "name": "Ana"}], '
                       '"loans": []}')
        stores = [self.open_store() for _ in range(8)]
        barrier = threading.Barrier(8)
        outcomes = []

        def run(store):
            barrier.wait(SYNC_TIMEOUT_SECONDS)
            try:
                import_legacy_json(legacy, store)
                outcomes.append("imported")
            except AlreadyPopulatedError:
                outcomes.append("skipped")

        threads = [threading.Thread(target=run, args=(store,)) for store in stores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(SYNC_TIMEOUT_SECONDS)
        self.assertEqual(sorted(outcomes), ["imported"] + ["skipped"] * 7)
        self.assertEqual(len(dump(self.path)["members"]), 1)


class CrashRecoveryTest(ReliabilityTestCase):
    def crash_during(self, body):
        """Run body in a real process that dies without any cleanup."""
        script = textwrap.dedent("""
            import os, sys
            from datetime import date
            from library import Library
            from models import Member
            from storage import SqliteStore
            store = SqliteStore(sys.argv[1])
            library = Library(store)
            def change():
            {body}
                os._exit(0)
            store.atomic(change)
            os._exit(3)
            """).format(body=textwrap.indent(body, "    "))
        subprocess.run([sys.executable, "-c", script, self.path], check=True,
                       cwd=PROJECT, timeout=SYNC_TIMEOUT_SECONDS)

    def test_crash_mid_issue_leaves_the_copy_available(self):
        self.seed()
        before = dump(self.path)
        self.crash_during(
            'store.add_loan("B1", "B1-C1", "M1", date.today())')
        with SqliteStore(self.path) as store:
            library = Library(store)
            self.assertEqual(library.available_copies("B1"), 2)
            library.issue_book("B1", "M1", "B1-C1")
        self.assertEqual(len(dump(self.path)["loans"]), len(before["loans"]) + 1)
        self.assert_consistent()

    def test_crash_mid_return_keeps_the_loan_active(self):
        self.seed()
        with SqliteStore(self.path) as store:
            Library(store).issue_book("B1", "M1")
        before = dump(self.path)
        self.crash_during(
            'store.mark_returned(1, date.today())')
        self.assertEqual(dump(self.path), before)
        with SqliteStore(self.path) as store:
            library = Library(store)
            self.assertEqual(len(library.active_loans()), 1)
            library.return_book("B1", "M1")
        self.assert_consistent()

    def test_crash_after_changes_reached_the_disk_is_recovered(self):
        """Forces uncommitted pages into the file, so recovery must undo them."""
        self.seed()
        before = dump(self.path)
        self.crash_during(textwrap.dedent("""\
            store._connection.execute("PRAGMA cache_size = 1")
            for number in range(2, 3000):
                store.add_member(Member(f"M{number + 1000}", "x" * 200))
            """))
        journal = self.path + "-journal"
        self.assertTrue(os.path.exists(journal), "the crash left no journal")

        with SqliteStore(self.path) as store:
            Library(store).register_member("After the crash")
        self.assertFalse(os.path.exists(journal))
        after = dump(self.path)
        self.assertEqual(after["members"][:len(before["members"])],
                         before["members"])
        self.assertEqual(len(after["members"]), len(before["members"]) + 1)
        self.assert_consistent()

    def test_crash_during_schema_creation_leaves_a_usable_file(self):
        script = textwrap.dedent("""
            import os, sqlite3, sys
            import storage
            connection = sqlite3.connect(sys.argv[1], isolation_level=None)
            connection.execute("BEGIN IMMEDIATE")
            for statement in storage.SCHEMA[:3]:
                connection.execute(statement)
            os._exit(0)
            """)
        subprocess.run([sys.executable, "-c", script, self.path], check=True,
                       cwd=PROJECT, timeout=SYNC_TIMEOUT_SECONDS)
        with SqliteStore(self.path) as store:
            self.assertEqual(Library(store).add_book("Dune", "F").id, "B1")


class CorruptedRecordTest(ReliabilityTestCase):
    def corrupt(self, *statements):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute("PRAGMA foreign_keys = OFF")
            for statement in statements:
                connection.execute(statement)
            connection.commit()
        finally:
            connection.close()

    def test_bad_record_written_while_running_is_reported_not_hidden(self):
        self.seed()
        library = Library(self.open_store())
        library.issue_book("B1", "M1")
        self.corrupt("DROP TRIGGER loan_history_is_immutable",
                     "UPDATE loans SET issued_on = 'garbage'")

        with self.assertRaises(CorruptedDataError):
            library.active_loans()
        with self.assertRaises(CorruptedDataError):
            library.issue_book("B1", "M1")

    def test_failed_operation_on_a_bad_record_changes_nothing(self):
        self.seed()
        library = Library(self.open_store())
        library.issue_book("B1", "M1")
        self.corrupt("DROP TRIGGER loan_history_is_immutable",
                     "UPDATE loans SET issued_on = 'garbage'")
        before = dump(self.path)
        with self.assertRaises(CorruptedDataError):
            library.return_book("B1", "M1")
        self.assertEqual(dump(self.path), before)

    def test_bad_records_stop_startup_and_are_kept(self):
        for name, statements in {
            "invalid date": ["UPDATE loans SET issued_on = 'garbage'"],
            "return before issue": ["UPDATE loans SET returned_on = '2000-01-01'"],
            "unknown member": ["UPDATE loans SET member_id = 'M99'"],
            "unknown copy": ["UPDATE loans SET copy_id = 'B1-C99'"],
            "blank name": ["UPDATE members SET name = ' '"],
        }.items():
            with self.subTest(name):
                if os.path.exists(self.path):
                    os.remove(self.path)
                self.seed()
                with SqliteStore(self.path) as store:
                    Library(store).issue_book("B1", "M1")
                self.corrupt("DROP TRIGGER loan_history_is_immutable", *statements)
                before = dump(self.path)
                with self.assertRaises(CorruptedDataError):
                    SqliteStore(self.path)
                self.assertEqual(dump(self.path), before)

    def test_damaged_file_during_a_read_is_reported_as_corruption(self):
        self.seed()
        store, connection = self.faulty_store()
        error = sqlite3.DatabaseError("database disk image is malformed")
        error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
        connection.should_fail = lambda sql: error
        with self.assertRaises(CorruptedDataError):
            Library(store).list_books()


class SchemaInvariantTest(ReliabilityTestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.store = self.open_store()
        self.library = Library(self.store)
        self.library.issue_book("B1", "M1")

    def assert_refused(self, sql):
        before = dump(self.path)
        with self.assertRaises(IntegrityViolationError):
            self.store._run(sql)
        self.assertEqual(dump(self.path), before)

    def test_loan_details_cannot_be_rewritten(self):
        for change in ("member_id = 'M2'", "book_id = 'B1', copy_id = 'B1-C2'",
                       "copy_id = 'B1-C2'", "issued_on = '2020-01-01'", "id = 9"):
            with self.subTest(change):
                self.assert_refused(f"UPDATE loans SET {change}")

    def test_closing_a_loan_cannot_also_rewrite_it(self):
        close = "returned_on = '2099-01-01'"
        for change in ("member_id = 'M2'", "copy_id = 'B1-C2'",
                       "issued_on = '2020-01-01'", "id = 9"):
            with self.subTest(change):
                self.assert_refused(f"UPDATE loans SET {close}, {change}")

    def test_returned_loan_cannot_be_reopened_or_redated(self):
        self.library.return_book("B1", "M1")
        self.assert_refused("UPDATE loans SET returned_on = NULL")
        self.assert_refused("UPDATE loans SET returned_on = '2099-01-01'")

    def test_copies_keep_their_identity(self):
        self.assert_refused("DELETE FROM copies WHERE id = 'B1-C2'")
        self.assert_refused("UPDATE copies SET id = 'B1-C9' WHERE id = 'B1-C2'")
        self.assert_refused("INSERT INTO copies VALUES ('B1-C1', 'B1')")
        self.assert_refused("INSERT INTO copies VALUES ('B7-C1', 'B1')")

    def test_records_in_use_cannot_be_deleted(self):
        self.assert_refused("DELETE FROM members WHERE id = 'M1'")
        self.assert_refused("DELETE FROM books WHERE id = 'B1'")

    def test_duplicate_ids_are_refused(self):
        self.assert_refused("INSERT INTO books VALUES ('B1', 'T', 'A')")
        self.assert_refused("INSERT INTO members VALUES ('M1', 'N')")

    def test_constraint_failure_inside_atomic_rolls_back(self):
        before = dump(self.path)

        def change():
            self.store._run("INSERT INTO members VALUES ('M50', 'New')")
            self.store._run("INSERT INTO members VALUES ('M1', 'Duplicate')")

        with self.assertRaises(IntegrityViolationError):
            self.store.atomic(change)
        self.assertEqual(dump(self.path), before)

    def test_return_before_issue_is_a_clear_rule_error(self):
        late = Library(self.store, today=lambda: date(2000, 1, 1))
        with self.assertRaises(ValidationError) as caught:
            late.return_book("B1", "M1")
        self.assertIn("before the issue date", str(caught.exception))
        self.assertEqual(len(self.library.active_loans()), 1)


class SchemaUpgradeTest(ReliabilityTestCase):
    def create_version_1(self):
        connection = sqlite3.connect(self.path)
        for statement in SCHEMA:
            connection.execute(statement)
        connection.execute("INSERT INTO books VALUES ('B1', 'Dune', 'F')")
        connection.execute("INSERT INTO copies VALUES ('B1-C1', 'B1')")
        connection.execute("INSERT INTO members VALUES ('M1', 'Ana')")
        connection.execute(
            "INSERT INTO loans (book_id, copy_id, member_id, issued_on) "
            "VALUES ('B1', 'B1-C1', 'M1', '2026-01-01')")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
        connection.close()

    def version(self):
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()

    def test_upgrade_keeps_data_and_adds_protection(self):
        self.create_version_1()
        before = dump(self.path)
        store = self.open_store()
        self.assertEqual(self.version(), SCHEMA_VERSION)
        self.assertEqual(dump(self.path), before)
        with self.assertRaises(IntegrityViolationError):
            store._run("UPDATE loans SET member_id = 'M9'")
        Library(store).return_book("B1", "M1")

    def test_failed_upgrade_leaves_the_old_version_intact(self):
        self.create_version_1()
        before = dump(self.path)
        fail_on = MIGRATIONS[-1][-1]

        class FailingConnection(FaultyConnection):
            def execute(self, sql, params=()):
                if sql == fail_on:
                    raise sqlite_failure(sqlite3.SQLITE_FULL, "disk is full")
                return super().execute(sql, params)

        with self.assertRaises(StorageError):
            SqliteStore(self.path, connect=lambda *args, **kwargs:
                        FailingConnection(sqlite3.connect(*args, **kwargs)))
        self.assertEqual(self.version(), 1)
        self.assertEqual(dump(self.path), before)
        self.open_store()
        self.assertEqual(self.version(), SCHEMA_VERSION)

    def test_upgrades_racing_apply_once(self):
        self.create_version_1()
        barrier = threading.Barrier(10)
        errors = []

        def run():
            barrier.wait(SYNC_TIMEOUT_SECONDS)
            try:
                with SqliteStore(self.path, busy_timeout=LONG_BUSY_TIMEOUT_SECONDS):
                    pass
            except Exception as error:  # reported by the assertion below
                errors.append(error)

        threads = [threading.Thread(target=run) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(SYNC_TIMEOUT_SECONDS)
        self.assertEqual(errors, [])
        self.assertEqual(self.version(), SCHEMA_VERSION)

    def test_newer_database_is_refused_untouched(self):
        self.create_version_1()
        connection = sqlite3.connect(self.path)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.close()
        with self.assertRaises(CorruptedDataError):
            SqliteStore(self.path)
        self.assertEqual(self.version(), SCHEMA_VERSION + 1)


if __name__ == "__main__":
    unittest.main()
