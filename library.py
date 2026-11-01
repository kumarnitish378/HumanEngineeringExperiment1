"""Business rules for a small library management system.

This module knows nothing about the user interface. It validates all input
here, at its boundary, and uses SqliteStore for persistence.
"""

from datetime import date

from errors import (
    AlreadyBorrowedError,
    LibraryError,
    NotFoundError,
    NotOnLoanError,
    UnavailableError,
    ValidationError,
    WrongMemberError,
)
from models import (
    BOOK_ID_PATTERN,
    COPY_ID_PATTERN,
    MEMBER_ID_PATTERN,
    Book,
    Copy,
    Member,
)
from storage import SqliteStore

__all__ = ["Library", "LibraryError"]

MAX_COPIES_PER_REQUEST = 1000


class Library:
    def __init__(self, store=None, today=date.today):
        """Uses store for persistence, or a temporary in-memory one if omitted.

        today supplies the date for loans, so tests can control it.
        """
        self._store = store if store is not None else SqliteStore()
        self._today = today

    def close(self):
        self._store.close()

    def add_book(self, title, author, copies=1):
        title = _required_text(title, "Title")
        author = _required_text(author, "Author")
        _check_copy_count(copies)
        with self._store.transaction():
            book = Book(self._store.next_book_id(), title, author)
            self._store.add_book(book)
            self._add_copies(book.id, copies)
        return book

    def add_copies(self, book_id, count=1):
        book_id = _normalize_id(book_id, BOOK_ID_PATTERN, "book")
        _check_copy_count(count)
        with self._store.transaction():
            self._require_book(book_id)
            return self._add_copies(book_id, count)

    def register_member(self, name):
        name = _required_text(name, "Member name")
        with self._store.transaction():
            member = Member(self._store.next_member_id(), name)
            self._store.add_member(member)
        return member

    def issue_book(self, book_id, member_id, copy_id=None):
        """Lend a copy to a member: the given one, or else any available one."""
        book_id = _normalize_id(book_id, BOOK_ID_PATTERN, "book")
        member_id = _normalize_id(member_id, MEMBER_ID_PATTERN, "member")
        if copy_id is not None:
            copy_id = _normalize_id(copy_id, COPY_ID_PATTERN, "copy")
        with self._store.transaction():
            book = self._require_book(book_id)
            member = self._require_member(member_id)
            if self._store.active_loan_for_member(book.id, member.id):
                raise AlreadyBorrowedError(
                    f"{member.name} already has a copy of '{book.title}'.")
            copy = self._choose_copy(book, copy_id)
            return self._store.add_loan(
                book.id, copy.id, member.id, self._today())

    def return_book(self, book_id, member_id, copy_id=None):
        """Close the member's loan of a book, optionally naming the exact copy.

        Only the member who borrowed the copy may return it.
        """
        book_id = _normalize_id(book_id, BOOK_ID_PATTERN, "book")
        member_id = _normalize_id(member_id, MEMBER_ID_PATTERN, "member")
        if copy_id is not None:
            copy_id = _normalize_id(copy_id, COPY_ID_PATTERN, "copy")
        with self._store.transaction():
            book = self._require_book(book_id)
            member = self._require_member(member_id)
            if copy_id is not None:
                self._require_copy_of(book, copy_id)
                loan = self._loan_of_copy(book, member, copy_id)
            else:
                loan = self._loan_of_book(book, member)
            self._store.mark_returned(loan.id, self._today())
            return self._store.get_loan(loan.id)

    def get_book(self, book_id):
        return self._require_book(
            _normalize_id(book_id, BOOK_ID_PATTERN, "book"))

    def get_member(self, member_id):
        return self._require_member(
            _normalize_id(member_id, MEMBER_ID_PATTERN, "member"))

    def list_books(self):
        return self._store.list_books()

    def list_members(self):
        return self._store.list_members()

    def list_copies(self, book_id):
        return self._store.list_copies(self.get_book(book_id).id)

    def total_copies(self, book_id):
        return len(self.list_copies(book_id))

    def available_copies(self, book_id):
        book = self.get_book(book_id)
        return len(self._store.list_available_copies(book.id))

    def active_loans(self):
        return self._store.list_active_loans()

    def loan_history(self, book_id=None, member_id=None):
        """All loans, active and returned, oldest first, optionally filtered."""
        if book_id is not None:
            book_id = self.get_book(book_id).id
        if member_id is not None:
            member_id = self.get_member(member_id).id
        return self._store.list_loans(book_id, member_id)

    def _add_copies(self, book_id, count):
        start = len(self._store.list_copies(book_id)) + 1
        copies = [Copy(f"{book_id}-C{number}", book_id)
                  for number in range(start, start + count)]
        for copy in copies:
            self._store.add_copy(copy)
        return copies

    def _choose_copy(self, book, copy_id):
        if copy_id is None:
            available = self._store.list_available_copies(book.id)
            if not available:
                raise UnavailableError(
                    f"No copies of '{book.title}' are available.")
            return available[0]
        copy = self._require_copy_of(book, copy_id)
        if self._store.active_loan_for_copy(copy.id):
            raise UnavailableError(f"Copy {copy.id} is already on loan.")
        return copy

    def _loan_of_copy(self, book, member, copy_id):
        loan = self._store.active_loan_for_copy(copy_id)
        if loan is None:
            raise NotOnLoanError(f"Copy {copy_id} is not on loan.")
        if loan.member_id != member.id:
            raise WrongMemberError(
                f"{member.name} cannot return copy {copy_id} of "
                f"'{book.title}': it is on loan to member {loan.member_id}.")
        return loan

    def _loan_of_book(self, book, member):
        loan = self._store.active_loan_for_member(book.id, member.id)
        if loan is not None:
            return loan
        if self._store.active_loans_for_book(book.id):
            raise WrongMemberError(
                f"{member.name} cannot return '{book.title}': "
                "it was borrowed by a different member.")
        raise NotOnLoanError(f"'{book.title}' is not on loan.")

    def _require_book(self, book_id):
        book = self._store.get_book(book_id)
        if book is None:
            raise NotFoundError(f"No book with ID '{book_id}'.")
        return book

    def _require_member(self, member_id):
        member = self._store.get_member(member_id)
        if member is None:
            raise NotFoundError(f"No member with ID '{member_id}'.")
        return member

    def _require_copy_of(self, book, copy_id):
        copy = self._store.get_copy(copy_id)
        if copy is None or copy.book_id != book.id:
            raise NotFoundError(
                f"'{book.title}' has no copy with ID '{copy_id}'.")
        return copy


def _normalize_id(value, pattern, kind):
    if not isinstance(value, str):
        raise ValidationError(f"Invalid {kind} ID: {value!r}.")
    normalized = value.strip().upper()
    if not pattern.fullmatch(normalized):
        raise ValidationError(f"Invalid {kind} ID: '{value}'.")
    return normalized


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} is required.")
    return value.strip()


def _check_copy_count(count):
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValidationError("Copies must be at least 1.")
    if count > MAX_COPIES_PER_REQUEST:
        raise ValidationError(
            f"Copies cannot exceed {MAX_COPIES_PER_REQUEST} at a time.")
