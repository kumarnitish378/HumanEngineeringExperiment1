"""Interactive command-line menu for the library. Run with: python main.py"""

from library import Library, LibraryError

DATA_FILE = "library.json"

MENU = """
1. Add book
2. Register member
3. Issue book
4. Return book
5. List books
6. List members
7. List active loans
0. Exit"""


def add_book(library):
    title = input("Title: ")
    author = input("Author: ")
    copies = input("Copies [1]: ").strip() or "1"
    if not copies.isdecimal():
        raise LibraryError("Copies must be a whole number.")
    book = library.add_book(title, author, int(copies))
    print(f"Added {book.id}: {book.title} by {book.author} ({book.copies} copies)")


def register_member(library):
    member = library.register_member(input("Name: "))
    print(f"Registered {member.id}: {member.name}")


def issue_book(library):
    loan = library.issue_book(input("Book ID: "), input("Member ID: "))
    print(f"Issued {loan.book_id} to {loan.member_id} on {loan.issued_on}")


def return_book(library):
    loan = library.return_book(input("Book ID: "), input("Member ID: "))
    print(f"Returned {loan.book_id} from {loan.member_id} on {loan.returned_on}")


def list_books(library):
    if not library.books:
        print("No books yet.")
    for book in library.books.values():
        available = library.available_copies(book.id)
        print(f"{book.id}: {book.title} by {book.author} "
              f"({available}/{book.copies} available)")


def list_members(library):
    if not library.members:
        print("No members yet.")
    for member in library.members.values():
        print(f"{member.id}: {member.name}")


def list_loans(library):
    loans = library.active_loans()
    if not loans:
        print("No active loans.")
    for loan in loans:
        book = library.books[loan.book_id]
        member = library.members[loan.member_id]
        print(f"{book.id} '{book.title}' -> {member.id} {member.name} "
              f"(since {loan.issued_on})")


ACTIONS = {
    "1": add_book,
    "2": register_member,
    "3": issue_book,
    "4": return_book,
    "5": list_books,
    "6": list_members,
    "7": list_loans,
}


def main():
    library = Library(DATA_FILE)
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
    except (EOFError, KeyboardInterrupt):
        print()


if __name__ == "__main__":
    main()
