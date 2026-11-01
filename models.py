"""Records stored by the library."""

import re
from dataclasses import dataclass
from datetime import date

BOOK_ID_PATTERN = re.compile(r"B[1-9]\d*")
MEMBER_ID_PATTERN = re.compile(r"M[1-9]\d*")
COPY_ID_PATTERN = re.compile(r"B[1-9]\d*-C[1-9]\d*")


@dataclass(frozen=True)
class Book:
    """A title. Physical items are represented by Copy."""

    id: str
    title: str
    author: str


@dataclass(frozen=True)
class Copy:
    """One physical item of a book, e.g. B1-C2 is the second copy of B1."""

    id: str
    book_id: str


@dataclass(frozen=True)
class Member:
    id: str
    name: str


@dataclass(frozen=True)
class Loan:
    """One borrowing. A return sets returned_on; the record is never deleted.

    copy_id is None only for returned loans imported from the old JSON format,
    which did not record which physical copy was lent.
    """

    id: int
    book_id: str
    copy_id: str | None
    member_id: str
    issued_on: date
    returned_on: date | None = None

    @property
    def is_active(self):
        return self.returned_on is None
