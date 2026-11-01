"""Exceptions raised by the library.

LibraryError means a request broke a business rule and the user can correct it.
StorageError means the data could not be read or written; it is kept separate
so that a rule violation is never mistaken for a failure of the system.
"""


class LibraryError(Exception):
    """A library rule was broken, e.g. issuing a book with no copies left."""


class ValidationError(LibraryError):
    """Input is malformed, e.g. a blank title or a badly formed ID."""


class NotFoundError(LibraryError):
    """A well-formed ID does not refer to an existing record."""


class UnavailableError(LibraryError):
    """The book or copy cannot be issued right now."""


class AlreadyBorrowedError(LibraryError):
    """The member already holds a copy of this book."""


class NotOnLoanError(LibraryError):
    """The book or copy being returned is not on loan."""


class WrongMemberError(LibraryError):
    """The member returning a book is not the member who borrowed it."""


class StorageError(Exception):
    """The data store could not complete an operation. Nothing was saved."""


class CorruptedDataError(StorageError):
    """Stored data is damaged or unreadable. The file is left untouched."""
