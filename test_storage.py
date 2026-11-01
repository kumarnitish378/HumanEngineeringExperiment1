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
    CorruptedDataError,
    StorageError,
    UnavailableError,
    ValidationError,
)
from library import Library
from storage import SqliteStore


class TempFileTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.path = os.path.join(self.folder, "library.db")

    def open_store(self):
        store = SqliteStore(self.path)
        self.addCleanup(store.close)
        return store

    def seed(self):
        with SqliteStore(self.path) as store:
            library = Library(store)
            library.add_book("Dune", "Frank Herbert", copies=2)
            library.register_member("Ana")
            library.register_member("Ben")

    def raw_sql(self, *statements):
        """Change the file directly, bypassing every application check."""
        connection = sqlite3.connect(self.path)
        try:
            for statement in statements:
                connection.execute(statement)
            connection.commit()
        finally:
            connection.close()

    def read_bytes(self):
        with open(self.path, "rb") as file:
            return file.read()

    def write_bytes(self, data):
        with open(self.path, "wb") as file:
            file.write(data)


class RestartTest(TempFileTestCase):
    def test_everything_survives_a_restart(self):
        self.seed()
        with SqliteStore(self.path) as store:
            library = Library(store, today=lambda: date(2026, 1, 1))
            library.issue_book("B1", "M1", "B1-C2")
            library.issue_book("B1", "M2", "B1-C1")
            library.return_book("B1", "M2")

        with SqliteStore(self.path) as store:
            library = Library(store)
            self.assertEqual(len(library.list_books()), 1)
            self.assertEqual(len(library.list_members()), 2)
            self.assertEqual(library.total_copies("B1"), 2)
            self.assertEqual(library.available_copies("B1"), 1)
            [active] = library.active_loans()
            self.assertEqual((active.copy_id, active.member_id), ("B1-C2", "M1"))
            self.assertEqual(active.issued_on, date(2026, 1, 1))
            self.assertEqual(len(library.loan_history()), 2)

    def test_rules_still_apply_after_a_restart(self):
        self.seed()
        with SqliteStore(self.path) as store:
            Library(store).issue_book("B1", "M1", "B1-C1")
        with SqliteStore(self.path) as store:
            with self.assertRaises(UnavailableError):
                Library(store).issue_book("B1", "M2", "B1-C1")

    def test_ids_continue_after_a_restart(self):
        self.seed()
        with SqliteStore(self.path) as store:
            library = Library(store)
            self.assertEqual(library.add_book("Emma", "Jane Austen").id, "B2")
            self.assertEqual(library.add_copies("B1")[0].id, "B1-C3")

    def test_missing_file_starts_an_empty_library(self):
        store = self.open_store()
        self.assertTrue(store.is_empty())
        self.assertEqual(Library(store).list_books(), [])

    def test_two_open_stores_cannot_double_issue_a_copy(self):
        self.seed()
        stores = [self.open_store() for _ in range(2)]
        barrier = threading.Barrier(2)
        outcomes = []

        def borrow(store, member_id):
            barrier.wait()
            try:
                outcomes.append(Library(store).issue_book("B1", member_id, "B1-C1"))
            except UnavailableError as error:
                outcomes.append(error)

        threads = [threading.Thread(target=borrow, args=pair)
                   for pair in zip(stores, ["M1", "M2"])]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(
            sorted(type(outcome).__name__ for outcome in outcomes),
            ["Loan", "UnavailableError"])

    def test_change_from_another_instance_is_seen_immediately(self):
        self.seed()
        first, second = Library(self.open_store()), Library(self.open_store())
        first.issue_book("B1", "M1", "B1-C1")
        self.assertEqual(second.available_copies("B1"), 1)


class CorruptedDataTest(TempFileTestCase):
    def assert_refused_and_untouched(self):
        before = self.read_bytes()
        with self.assertRaises(CorruptedDataError):
            SqliteStore(self.path)
        self.assertEqual(self.read_bytes(), before)

    def test_file_that_is_not_a_database(self):
        self.write_bytes(b'{"books": []}' * 100)
        self.assert_refused_and_untouched()

    def test_truncated_database(self):
        self.seed()
        self.write_bytes(self.read_bytes()[:5000])
        self.assert_refused_and_untouched()

    def test_damaged_pages(self):
        self.seed()
        data = bytearray(self.read_bytes())
        data[100:] = b"\xff" * (len(data) - 100)
        self.write_bytes(bytes(data))
        self.assert_refused_and_untouched()

    def test_missing_table(self):
        self.seed()
        self.raw_sql("DROP TABLE members")
        self.assert_refused_and_untouched()

    def test_unknown_schema_version(self):
        self.seed()
        self.raw_sql("PRAGMA user_version = 99")
        self.assert_refused_and_untouched()

    def test_tables_without_a_schema_version(self):
        self.raw_sql("CREATE TABLE something_else (id INTEGER)")
        self.assert_refused_and_untouched()

    def test_broken_links_between_records(self):
        self.seed()
        self.raw_sql("PRAGMA foreign_keys = OFF",
                     "INSERT INTO copies VALUES ('B9-C1', 'B9')")
        self.assert_refused_and_untouched()

    def test_invalid_field_value(self):
        self.seed()
        self.raw_sql(
            "PRAGMA ignore_check_constraints = ON",
            "INSERT INTO loans (book_id, copy_id, member_id, issued_on) "
            "VALUES ('B1', 'B1-C1', 'M1', 'not-a-date')")
        self.assert_refused_and_untouched()

    def test_path_that_cannot_be_opened_is_a_storage_error(self):
        with self.assertRaises(StorageError) as caught:
            SqliteStore(self.folder)
        self.assertNotIsInstance(caught.exception, CorruptedDataError)


class WriteFailureTest(TempFileTestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.store = self.open_store()
        self.library = Library(self.store)

    def fail_writes(self, name, event):
        self.raw_sql(f"CREATE TRIGGER {name} BEFORE {event} "
                     "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END")

    def test_failed_issue_changes_nothing_and_can_be_retried(self):
        self.fail_writes("no_issue", "INSERT ON loans")
        with self.assertRaises(StorageError):
            self.library.issue_book("B1", "M1")
        self.assertEqual(self.library.active_loans(), [])
        self.assertEqual(self.library.available_copies("B1"), 2)

        self.raw_sql("DROP TRIGGER no_issue")
        self.assertEqual(self.library.issue_book("B1", "M1").copy_id, "B1-C1")

    def test_failed_return_keeps_the_loan_active(self):
        self.library.issue_book("B1", "M1")
        self.fail_writes("no_return", "UPDATE ON loans")
        with self.assertRaises(StorageError):
            self.library.return_book("B1", "M1")
        self.assertEqual(len(self.library.active_loans()), 1)
        self.assertEqual(self.library.available_copies("B1"), 1)

    def test_add_book_failing_midway_leaves_no_partial_book(self):
        self.raw_sql(
            "CREATE TRIGGER no_third_copy BEFORE INSERT ON copies "
            "WHEN NEW.id LIKE '%-C3' "
            "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END")
        with self.assertRaises(StorageError):
            self.library.add_book("Emma", "Jane Austen", copies=5)
        self.assertEqual(len(self.library.list_books()), 1)
        self.assertEqual(self.store.list_copies("B2"), [])

        self.raw_sql("DROP TRIGGER no_third_copy")
        book = self.library.add_book("Emma", "Jane Austen", copies=5)
        self.assertEqual((book.id, self.library.total_copies("B2")), ("B2", 5))

    def test_read_only_database_rejects_writes_and_keeps_reading(self):
        self.store._connection.execute("PRAGMA query_only = ON")
        with self.assertRaises(StorageError):
            self.library.register_member("Carl")
        self.assertEqual(len(self.library.list_members()), 2)

    def test_failed_commit_rolls_back(self):
        class FailingCommitStore(SqliteStore):
            def _run(self, sql, params=()):
                if sql == "COMMIT":
                    raise StorageError("simulated commit failure")
                return super()._run(sql, params)

        store = FailingCommitStore(self.path)
        self.addCleanup(store.close)
        with self.assertRaises(StorageError):
            Library(store).issue_book("B1", "M1")
        self.assertEqual(self.library.active_loans(), [])
        self.assertFalse(store._connection.in_transaction)

    def test_unexpected_errors_roll_back_and_are_not_swallowed(self):
        with self.assertRaises(ZeroDivisionError):
            Library(self.store, today=lambda: 1 / 0).issue_book("B1", "M1")
        self.assertEqual(self.library.active_loans(), [])
        self.assertFalse(self.store._connection.in_transaction)

    def test_business_rule_failure_leaves_no_open_transaction(self):
        with self.assertRaises(ValidationError):
            self.library.add_copies("B1", 0)
        with self.assertRaises(UnavailableError):
            self.library.issue_book("B1", "M1", "B1-C1") and \
                self.library.issue_book("B1", "M2", "B1-C1")
        self.assertFalse(self.store._connection.in_transaction)

    def test_failed_write_is_not_durable_after_restart(self):
        self.fail_writes("no_member", "INSERT ON members")
        with self.assertRaises(StorageError):
            self.library.register_member("Carl")
        self.store.close()
        with SqliteStore(self.path) as reopened:
            self.assertEqual(len(reopened.list_members()), 2)

    def test_process_killed_mid_transaction_leaves_nothing_behind(self):
        self.store.close()
        script = textwrap.dedent("""
            import os, sys
            from models import Member
            from storage import SqliteStore
            store = SqliteStore(sys.argv[1])
            with store.transaction():
                store.add_member(Member("M3", "Carl"))
                os._exit(0)
            """)
        subprocess.run([sys.executable, "-c", script, self.path], check=True,
                       cwd=os.path.dirname(os.path.abspath(__file__)))
        with SqliteStore(self.path) as reopened:
            self.assertEqual(len(reopened.list_members()), 2)

    def test_closed_store_reports_a_storage_error(self):
        self.store.close()
        with self.assertRaises(StorageError):
            self.library.list_books()


class SchemaRulesTest(unittest.TestCase):
    """The database itself refuses bad records, whatever the code above does."""

    def setUp(self):
        self.store = SqliteStore()
        self.addCleanup(self.store.close)
        self.library = Library(self.store)
        self.library.add_book("Dune", "Frank Herbert", copies=2)
        self.library.add_book("Emma", "Jane Austen")
        self.library.register_member("Ana")
        self.library.register_member("Ben")
        self.day = date(2026, 1, 1)

    def test_copy_cannot_have_two_active_loans(self):
        self.store.add_loan("B1", "B1-C1", "M1", self.day)
        with self.assertRaises(StorageError):
            self.store.add_loan("B1", "B1-C1", "M2", self.day)

    def test_member_cannot_hold_two_copies_of_a_title(self):
        self.store.add_loan("B1", "B1-C1", "M1", self.day)
        with self.assertRaises(StorageError):
            self.store.add_loan("B1", "B1-C2", "M1", self.day)

    def test_copy_must_belong_to_the_loaned_book(self):
        with self.assertRaises(StorageError):
            self.store.add_loan("B2", "B1-C1", "M1", self.day)

    def test_loan_needs_an_existing_member(self):
        with self.assertRaises(StorageError):
            self.store.add_loan("B1", "B1-C1", "M9", self.day)

    def test_return_cannot_precede_issue(self):
        loan = self.store.add_loan("B1", "B1-C1", "M1", self.day)
        with self.assertRaises(StorageError):
            self.store.mark_returned(loan.id, date(2025, 12, 31))

    def test_invalid_dates_are_refused(self):
        with self.assertRaises(StorageError):
            self.store._run(
                "INSERT INTO loans (book_id, copy_id, member_id, issued_on) "
                "VALUES ('B1', 'B1-C1', 'M1', 'not-a-date')")
        with self.assertRaises(StorageError):
            self.store._run(
                "INSERT INTO loans (book_id, copy_id, member_id, issued_on, "
                "returned_on) VALUES ('B1', 'B1-C1', 'M1', '2026-01-01', "
                "'not-a-date')")

    def test_active_loan_must_name_a_copy(self):
        with self.assertRaises(StorageError):
            self.store.add_loan("B1", None, "M1", self.day)

    def test_blank_values_are_refused(self):
        with self.assertRaises(StorageError):
            self.store._run("INSERT INTO members VALUES ('M3', '  ')")

    def test_loan_history_cannot_be_deleted(self):
        self.store.add_loan("B1", "B1-C1", "M1", self.day)
        with self.assertRaises(StorageError):
            self.store._run("DELETE FROM loans")
        self.assertEqual(len(self.library.loan_history()), 1)

    def test_marking_a_returned_loan_again_fails(self):
        loan = self.store.add_loan("B1", "B1-C1", "M1", self.day)
        self.store.mark_returned(loan.id, self.day)
        with self.assertRaises(StorageError):
            self.store.mark_returned(loan.id, self.day)


if __name__ == "__main__":
    unittest.main()
