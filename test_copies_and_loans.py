import threading
import unittest
from datetime import date, timedelta

from errors import (
    AlreadyBorrowedError,
    LibraryError,
    NotFoundError,
    NotOnLoanError,
    UnavailableError,
    ValidationError,
    WrongMemberError,
)
from library import Library


class Clock:
    """A date source the tests can move forward."""

    def __init__(self):
        self.current = date(2026, 1, 1)

    def __call__(self):
        return self.current

    def advance(self, days=1):
        self.current += timedelta(days=days)


class LibraryTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.library = Library(today=self.clock)
        self.addCleanup(self.library.close)
        self.book = self.library.add_book("Dune", "Frank Herbert", copies=2)
        self.ana = self.library.register_member("Ana")
        self.ben = self.library.register_member("Ben")
        self.carl = self.library.register_member("Carl")


class MultipleCopiesTest(LibraryTestCase):
    def test_each_copy_has_a_unique_id(self):
        ids = [copy.id for copy in self.library.list_copies("B1")]
        self.assertEqual(ids, ["B1-C1", "B1-C2"])

    def test_members_borrow_different_copies_at_the_same_time(self):
        first = self.library.issue_book("B1", "M1")
        second = self.library.issue_book("B1", "M2")
        self.assertEqual({first.copy_id, second.copy_id}, {"B1-C1", "B1-C2"})
        self.assertEqual(self.library.available_copies("B1"), 0)

    def test_issue_a_specific_copy(self):
        loan = self.library.issue_book("B1", "M1", "b1-c2")
        self.assertEqual(loan.copy_id, "B1-C2")
        self.assertEqual(self.library.issue_book("B1", "M2").copy_id, "B1-C1")

    def test_same_copy_cannot_be_issued_twice(self):
        self.library.issue_book("B1", "M1", "B1-C1")
        with self.assertRaises(UnavailableError) as caught:
            self.library.issue_book("B1", "M2", "B1-C1")
        self.assertIn("B1-C1", str(caught.exception))

    def test_copy_of_another_book_is_rejected(self):
        self.library.add_book("Emma", "Jane Austen")
        with self.assertRaises(NotFoundError):
            self.library.issue_book("B1", "M1", "B2-C1")

    def test_returned_copy_can_be_issued_to_someone_else(self):
        self.library.issue_book("B1", "M1", "B1-C1")
        self.library.return_book("B1", "M1")
        loan = self.library.issue_book("B1", "M2", "B1-C1")
        self.assertEqual(loan.member_id, "M2")

    def test_add_copies_continues_numbering(self):
        added = self.library.add_copies("B1", 2)
        self.assertEqual([copy.id for copy in added], ["B1-C3", "B1-C4"])
        self.assertEqual(self.library.total_copies("B1"), 4)

    def test_only_one_of_many_concurrent_issues_gets_the_last_copy(self):
        book = self.library.add_book("Emma", "Jane Austen", copies=1)
        members = [self.library.register_member(f"Member {n}") for n in range(8)]
        results = []
        barrier = threading.Barrier(len(members))

        def borrow(member):
            barrier.wait()
            try:
                results.append(self.library.issue_book(book.id, member.id))
            except UnavailableError:
                pass

        threads = [threading.Thread(target=borrow, args=(member,))
                   for member in members]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 1)


class MemberVerificationTest(LibraryTestCase):
    def test_wrong_member_is_rejected_and_loan_stays_open(self):
        self.library.issue_book("B1", "M1")
        with self.assertRaises(WrongMemberError) as caught:
            self.library.return_book("B1", "M2")
        self.assertIn("Ben", str(caught.exception))
        self.assertIn("Dune", str(caught.exception))
        self.assertEqual(len(self.library.active_loans()), 1)

    def test_borrower_of_one_copy_cannot_return_another_copy(self):
        self.library.issue_book("B1", "M1", "B1-C1")
        self.library.issue_book("B1", "M2", "B1-C2")
        with self.assertRaises(WrongMemberError):
            self.library.return_book("B1", "M2", "B1-C1")
        self.assertEqual(len(self.library.active_loans()), 2)

    def test_return_with_the_right_copy(self):
        self.library.issue_book("B1", "M1", "B1-C2")
        loan = self.library.return_book("B1", "M1", "B1-C2")
        self.assertEqual(loan.copy_id, "B1-C2")

    def test_returning_a_book_nobody_borrowed(self):
        with self.assertRaises(NotOnLoanError):
            self.library.return_book("B1", "M1")
        with self.assertRaises(NotOnLoanError):
            self.library.return_book("B1", "M1", "B1-C1")

    def test_second_return_is_rejected(self):
        self.library.issue_book("B1", "M1")
        self.library.return_book("B1", "M1")
        with self.assertRaises(NotOnLoanError):
            self.library.return_book("B1", "M1")

    def test_member_rule_still_blocks_a_second_copy(self):
        self.library.issue_book("B1", "M1")
        with self.assertRaises(AlreadyBorrowedError):
            self.library.issue_book("B1", "M1", "B1-C2")


class InputValidationTest(LibraryTestCase):
    def test_malformed_ids_are_rejected(self):
        for bad_id in ["", "   ", "B", "B0", "B1x", "1", "M1", "B-1", "B1;--", None, 1]:
            with self.subTest(bad_id=bad_id), self.assertRaises(ValidationError):
                self.library.issue_book(bad_id, "M1")
        for bad_id in ["", "M", "M0", "B1", None, 1.5]:
            with self.subTest(bad_id=bad_id), self.assertRaises(ValidationError):
                self.library.return_book("B1", bad_id)
        for bad_id in ["", "B1", "C1", "B1-C0", "B1-C", 7]:
            with self.subTest(bad_id=bad_id), self.assertRaises(ValidationError):
                self.library.issue_book("B1", "M1", bad_id)

    def test_well_formed_unknown_ids_are_not_found(self):
        for action in (self.library.issue_book, self.library.return_book):
            with self.assertRaises(NotFoundError):
                action("B99", "M1")
            with self.assertRaises(NotFoundError):
                action("B1", "M99")
        with self.assertRaises(NotFoundError):
            self.library.issue_book("B1", "M1", "B1-C99")

    def test_invalid_copy_counts_are_rejected(self):
        for count in [0, -1, 1.5, "2", True, None, 1001]:
            with self.subTest(count=count), self.assertRaises(ValidationError):
                self.library.add_book("Title", "Author", count)
            with self.subTest(count=count), self.assertRaises(ValidationError):
                self.library.add_copies("B1", count)

    def test_non_text_names_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.library.add_book(None, "Author")
        with self.assertRaises(ValidationError):
            self.library.register_member(42)

    def test_rejected_add_book_creates_nothing(self):
        with self.assertRaises(LibraryError):
            self.library.add_book("Title", "Author", 0)
        self.assertEqual(len(self.library.list_books()), 1)


class LoanHistoryTest(LibraryTestCase):
    def test_return_keeps_the_record_with_both_dates(self):
        self.library.issue_book("B1", "M1")
        self.clock.advance(3)
        self.library.return_book("B1", "M1")

        [loan] = self.library.loan_history()
        self.assertEqual(loan.member_id, "M1")
        self.assertEqual(loan.book_id, "B1")
        self.assertEqual(loan.copy_id, "B1-C1")
        self.assertEqual(loan.issued_on, date(2026, 1, 1))
        self.assertEqual(loan.returned_on, date(2026, 1, 4))
        self.assertFalse(loan.is_active)
        self.assertEqual(self.library.active_loans(), [])

    def test_repeated_issue_and_return_builds_history(self):
        for _ in range(5):
            self.library.issue_book("B1", "M1")
            self.clock.advance()
            self.library.return_book("B1", "M1")
            self.clock.advance()

        history = self.library.loan_history(member_id="M1")
        self.assertEqual(len(history), 5)
        self.assertEqual(len({loan.id for loan in history}), 5)
        self.assertTrue(all(loan.returned_on for loan in history))
        self.assertEqual(self.library.available_copies("B1"), 2)

    def test_history_lists_active_and_returned_loans(self):
        self.library.issue_book("B1", "M1")
        self.library.return_book("B1", "M1")
        self.library.issue_book("B1", "M2")

        self.assertEqual([loan.is_active for loan in self.library.loan_history()],
                         [False, True])
        self.assertEqual([loan.member_id for loan in self.library.active_loans()],
                         ["M2"])

    def test_history_filters(self):
        self.library.add_book("Emma", "Jane Austen")
        self.library.issue_book("B1", "M1")
        self.library.issue_book("B2", "M1")
        self.library.issue_book("B1", "M2")

        self.assertEqual(len(self.library.loan_history(book_id="B1")), 2)
        self.assertEqual(len(self.library.loan_history(member_id="M1")), 2)
        self.assertEqual(len(self.library.loan_history("b2", "m1")), 1)

    def test_history_rejects_bad_filters(self):
        with self.assertRaises(ValidationError):
            self.library.loan_history(book_id="nope")
        with self.assertRaises(NotFoundError):
            self.library.loan_history(member_id="M99")


if __name__ == "__main__":
    unittest.main()
