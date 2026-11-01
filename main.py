"""Interactive command-line menu for the library. Run with: python main.py"""

import os
import sys

from errors import (
    AlreadyPopulatedError,
    DatabaseBusyError,
    LibraryError,
    StorageError,
)
from legacy_import import import_legacy_json
from library import Library
from storage import SqliteStore

DATA_FILE = "library.db"
LEGACY_DATA_FILE = "library.json"

MENU = """
1. Add book
2. Register member
3. Issue book
4. Return book
5. List books
6. List members
7. List active loans
8. Add copies of a book
9. Show loan history
0. Exit"""


def add_book(library):
    title = input("Title: ")
    author = input("Author: ")
    copies = read_count("Copies [1]: ")
    book = library.add_book(title, author, copies)
    print(f"Added {book.id}: {book.title} by {book.author} ({copies} copies)")


def register_member(library):
    member = library.register_member(input("Name: "))
    print(f"Registered {member.id}: {member.name}")


def issue_book(library):
    book_id = input("Book ID: ")
    member_id = input("Member ID: ")
    copy_id = input("Copy ID [any available]: ").strip() or None
    loan = library.issue_book(book_id, member_id, copy_id)
    print(f"Issued {loan.copy_id} to {loan.member_id} on {loan.issued_on}")


def return_book(library):
    book_id = input("Book ID: ")
    member_id = input("Member ID: ")
    copy_id = input("Copy ID [optional]: ").strip() or None
    loan = library.return_book(book_id, member_id, copy_id)
    print(f"Returned {loan.copy_id} from {loan.member_id} on {loan.returned_on}")


def list_books(library):
    books = library.list_books()
    if not books:
        print("No books yet.")
    for book in books:
        print(f"{book.id}: {book.title} by {book.author} "
              f"({library.available_copies(book.id)}/"
              f"{library.total_copies(book.id)} available)")


def list_members(library):
    members = library.list_members()
    if not members:
        print("No members yet.")
    for member in members:
        print(f"{member.id}: {member.name}")


def list_loans(library):
    loans = library.active_loans()
    if not loans:
        print("No active loans.")
    for loan in loans:
        book = library.get_book(loan.book_id)
        member = library.get_member(loan.member_id)
        print(f"{loan.copy_id} '{book.title}' -> {member.id} {member.name} "
              f"(since {loan.issued_on})")


def add_copies(library):
    book_id = input("Book ID: ")
    copies = library.add_copies(book_id, read_count("Copies to add [1]: "))
    print("Added " + ", ".join(copy.id for copy in copies))


def show_history(library):
    book_id = input("Book ID [all]: ").strip() or None
    member_id = input("Member ID [all]: ").strip() or None
    loans = library.loan_history(book_id, member_id)
    if not loans:
        print("No loans found.")
    for loan in loans:
        returned = loan.returned_on if loan.returned_on else "not returned"
        copy = loan.copy_id if loan.copy_id else loan.book_id
        print(f"{copy} -> {loan.member_id}: "
              f"issued {loan.issued_on}, returned {returned}")


def read_count(prompt):
    count = input(prompt).strip() or "1"
    if not count.isdecimal():
        raise LibraryError("Copies must be a whole number.")
    return int(count)


ACTIONS = {
    "1": add_book,
    "2": register_member,
    "3": issue_book,
    "4": return_book,
    "5": list_books,
    "6": list_members,
    "7": list_loans,
    "8": add_copies,
    "9": show_history,
}


def import_legacy_data(store):
    if store.is_empty() and os.path.exists(LEGACY_DATA_FILE):
        try:
            import_legacy_json(LEGACY_DATA_FILE, store)
        except AlreadyPopulatedError:
            return  # another running instance imported it first
        print(f"Imported existing data from {LEGACY_DATA_FILE}.")


def run_menu(library):
    try:
        while True:
            print(MENU)
            choice = input("Choose: ").strip()
            if choice == "0":
                break
            action = ACTIONS.get(choice)
            if action is None:
                print("Invalid choice.")
                continue
            try:
                action(library)
            except LibraryError as error:
                print(f"Error: {error}")
            except DatabaseBusyError:
                print("The library is busy, nothing was changed. Try again.")
            except StorageError as error:
                print(f"Storage error, nothing was changed: {error}")
    except (EOFError, KeyboardInterrupt):
        print()


def main():
    try:
        with SqliteStore(DATA_FILE) as store:
            import_legacy_data(store)
            run_menu(Library(store))
    except StorageError as error:
        print(f"Cannot start: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
