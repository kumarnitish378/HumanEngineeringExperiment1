import os
import tempfile
import unittest

from library import Library, LibraryError


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.library = Library()
        self.book = self.library.add_book("Dune", "Frank Herbert", copies=2)
        self.ana = self.library.register_member("Ana")
        self.ben = self.library.register_member("Ben")

    def test_ids_are_sequential(self):
        self.assertEqual(self.book.id, "B1")
        self.assertEqual((self.ana.id, self.ben.id), ("M1", "M2"))

    def test_add_book_rejects_invalid_input(self):
        for title, author, copies in [(" ", "A", 1), ("T", "", 1), ("T", "A", 0)]:
            with self.assertRaises(LibraryError):
                self.library.add_book(title, author, copies)

    def test_register_member_rejects_blank_name(self):
        with self.assertRaises(LibraryError):
            self.library.register_member("   ")

    def test_issue_reduces_available_copies(self):
        self.library.issue_book(self.book.id, self.ana.id)
        self.assertEqual(self.library.available_copies(self.book.id), 1)
        self.assertEqual(len(self.library.active_loans()), 1)

    def test_issue_fails_when_no_copies_left(self):
        carl = self.library.register_member("Carl")
        self.library.issue_book(self.book.id, self.ana.id)
        self.library.issue_book(self.book.id, self.ben.id)
        with self.assertRaises(LibraryError):
            self.library.issue_book(self.book.id, carl.id)

    def test_member_cannot_hold_two_copies_of_a_book(self):
        self.library.issue_book(self.book.id, self.ana.id)
        with self.assertRaises(LibraryError):
            self.library.issue_book(self.book.id, self.ana.id)

    def test_issue_rejects_unknown_ids(self):
        with self.assertRaises(LibraryError):
            self.library.issue_book("B99", self.ana.id)
        with self.assertRaises(LibraryError):
            self.library.issue_book(self.book.id, "M99")

    def test_ids_ignore_case_and_whitespace(self):
        loan = self.library.issue_book(" b1 ", "m1")
        self.assertEqual((loan.book_id, loan.member_id), ("B1", "M1"))

    def test_return_restores_copy_and_allows_reissue(self):
        self.library.issue_book(self.book.id, self.ana.id)
        loan = self.library.return_book(self.book.id, self.ana.id)
        self.assertIsNotNone(loan.returned_on)
        self.assertEqual(self.library.available_copies(self.book.id), 2)
        self.library.issue_book(self.book.id, self.ana.id)

    def test_return_fails_without_active_loan(self):
        with self.assertRaises(LibraryError):
            self.library.return_book(self.book.id, self.ana.id)

    def test_member_cannot_return_another_members_loan(self):
        self.library.issue_book(self.book.id, self.ana.id)
        with self.assertRaises(LibraryError):
            self.library.return_book(self.book.id, self.ben.id)


class PersistenceTest(unittest.TestCase):
    def test_state_survives_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "library.json")
            first = Library(path)
            first.add_book("Dune", "Frank Herbert")
            first.register_member("Ana")
            first.issue_book("B1", "M1")

            second = Library(path)
            self.assertEqual(second.books["B1"].title, "Dune")
            self.assertEqual(second.members["M1"].name, "Ana")
            self.assertEqual(second.available_copies("B1"), 0)
            self.assertEqual(second.register_member("Ben").id, "M2")


if __name__ == "__main__":
    unittest.main()
