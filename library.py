"""Core logic for a small library management system."""

import json
import os
from dataclasses import asdict, dataclass
from datetime import date


class LibraryError(Exception):
    """A library rule was broken, e.g. issuing a book with no copies left."""


@dataclass
class Book:
    id: str
    title: str
    author: str
    copies: int


@dataclass
class Member:
    id: str
    name: str


@dataclass
class Loan:
    book_id: str
    member_id: str
    issued_on: str
    returned_on: str | None = None


class Library:
    """Holds books, members and loans, optionally saving them to a JSON file."""

    def __init__(self, path=None):
        self.path = path
        self.books = {}
        self.members = {}
        self.loans = []
        if path and os.path.exists(path):
            self._load()

    def add_book(self, title, author, copies=1):
        title, author = title.strip(), author.strip()
        if not title or not author:
            raise LibraryError("Title and author are required.")
        if copies < 1:
            raise LibraryError("Copies must be at least 1.")
        book = Book(f"B{len(self.books) + 1}", title, author, copies)
        self.books[book.id] = book
        self._save()
        return book

    def register_member(self, name):
        name = name.strip()
        if not name:
            raise LibraryError("Member name is required.")
        member = Member(f"M{len(self.members) + 1}", name)
        self.members[member.id] = member
        self._save()
        return member

    def issue_book(self, book_id, member_id):
        book = self._get_book(book_id)
        member = self._get_member(member_id)
        if self._open_loan(book.id, member.id):
            raise LibraryError(f"{member.name} already has '{book.title}'.")
        if self.available_copies(book.id) == 0:
            raise LibraryError(f"No copies of '{book.title}' are available.")
        loan = Loan(book.id, member.id, date.today().isoformat())
        self.loans.append(loan)
        self._save()
        return loan

    def return_book(self, book_id, member_id):
        book = self._get_book(book_id)
        member = self._get_member(member_id)
        loan = self._open_loan(book.id, member.id)
        if loan is None:
            raise LibraryError(f"{member.name} does not have '{book.title}'.")
        loan.returned_on = date.today().isoformat()
        self._save()
        return loan

    def available_copies(self, book_id):
        book = self._get_book(book_id)
        on_loan = sum(
            1 for loan in self.loans
            if loan.book_id == book.id and loan.returned_on is None
        )
        return book.copies - on_loan

    def active_loans(self):
        return [loan for loan in self.loans if loan.returned_on is None]

    def _get_book(self, book_id):
        book = self.books.get(book_id.strip().upper())
        if book is None:
            raise LibraryError(f"No book with ID '{book_id}'.")
        return book

    def _get_member(self, member_id):
        member = self.members.get(member_id.strip().upper())
        if member is None:
            raise LibraryError(f"No member with ID '{member_id}'.")
        return member

    def _open_loan(self, book_id, member_id):
        for loan in self.loans:
            if (loan.book_id, loan.member_id) == (book_id, member_id) \
                    and loan.returned_on is None:
                return loan
        return None

    def _load(self):
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.books = {b["id"]: Book(**b) for b in data["books"]}
        self.members = {m["id"]: Member(**m) for m in data["members"]}
        self.loans = [Loan(**loan) for loan in data["loans"]]

    def _save(self):
        if not self.path:
            return
        data = {
            "books": [asdict(b) for b in self.books.values()],
            "members": [asdict(m) for m in self.members.values()],
            "loans": [asdict(loan) for loan in self.loans],
        }
        # Write to a temp file first so a crash can't leave a half-written file.
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, self.path)
