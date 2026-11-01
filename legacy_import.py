"""One-time import of the old library.json format into the SQLite store.

The whole file is validated before anything is written, and the import runs in
one transaction, so a bad file imports nothing. The JSON file is only read.
"""

import json
from datetime import date

from errors import CorruptedDataError, StorageError
from models import (
    BOOK_ID_PATTERN,
    MEMBER_ID_PATTERN,
    Book,
    Copy,
    Member,
)


def import_legacy_json(path, store):
    """Copy books, members and loans from path into an empty store."""
    if not store.is_empty():
        raise StorageError("Legacy data can only be imported into an empty store.")
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
        books, members, loans = _parse(data)
    except OSError as error:
        raise StorageError(f"Cannot read '{path}': {error}") from error
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        # json.JSONDecodeError and UnicodeDecodeError are both ValueErrors.
        raise CorruptedDataError(
            f"'{path}' is not valid library data: {error!r}. "
            "The file was left unchanged and nothing was imported.") from error

    with store.transaction():
        for book, copies in books:
            store.add_book(book)
            for copy in copies:
                store.add_copy(copy)
        for member in members:
            store.add_member(member)
        for loan in loans:
            store.add_loan(*loan)


def _parse(data):
    """Return books with their copies, members and loans, or raise ValueError."""
    books = {}
    for record in data["books"]:
        book = Book(_id(record["id"], BOOK_ID_PATTERN),
                    _text(record["title"]), _text(record["author"]))
        count = record["copies"]
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ValueError(f"book {book.id} has invalid copies: {count!r}")
        if book.id in books:
            raise ValueError(f"duplicate book {book.id}")
        books[book.id] = (book, [Copy(f"{book.id}-C{number}", book.id)
                                 for number in range(1, count + 1)])

    members = {}
    for record in data["members"]:
        member = Member(_id(record["id"], MEMBER_ID_PATTERN),
                        _text(record["name"]))
        if member.id in members:
            raise ValueError(f"duplicate member {member.id}")
        members[member.id] = member

    # The old format never named a copy. Each active loan is given the next
    # free copy; returned loans keep no copy rather than an invented one.
    free_copies = {book_id: list(copies) for book_id, (_, copies) in books.items()}
    active = set()
    loans = []
    for record in data["loans"]:
        book_id, member_id = record["book_id"], record["member_id"]
        if book_id not in books or member_id not in members:
            raise ValueError(f"loan refers to unknown IDs: {record!r}")
        issued_on = date.fromisoformat(record["issued_on"])
        returned_on = record.get("returned_on")
        if returned_on is not None:
            returned_on = date.fromisoformat(returned_on)
            if returned_on < issued_on:
                raise ValueError(f"loan returned before issue: {record!r}")
            loans.append((book_id, None, member_id, issued_on, returned_on))
            continue
        if (book_id, member_id) in active:
            raise ValueError(f"duplicate active loan: {record!r}")
        if not free_copies[book_id]:
            raise ValueError(f"more active loans than copies of {book_id}")
        active.add((book_id, member_id))
        copy = free_copies[book_id].pop(0)
        loans.append((book_id, copy.id, member_id, issued_on, None))

    return list(books.values()), list(members.values()), loans


def _id(value, pattern):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid ID: {value!r}")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"expected text, got {value!r}")
    return value.strip()
