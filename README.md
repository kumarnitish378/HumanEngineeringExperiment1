# Library Management System

A command-line library system in Python. It manages books, physical copies and
members, issues and returns books, and keeps a permanent borrowing history.

## Setup

You need Python 3.11 or newer. There is nothing to install: the project uses
only the standard library, including `sqlite3`.

```
git clone https://github.com/kumarnitish378/HumanEngineeringExperiment1
cd HumanEngineeringExperiment1
```

## Running

```
python main.py
```

The menu covers adding books and copies, registering members, issuing and
returning books, and listing books, members, active loans and loan history.

IDs are generated for you, and upper or lower case both work:

| Record | Example | Meaning |
| --- | --- | --- |
| Book | `B1` | A title |
| Copy | `B1-C2` | The second physical copy of `B1` |
| Member | `M1` | A registered member |

When issuing, leave the copy ID blank to get any available copy.

## Testing

```
python -m unittest
```

| File | Covers |
| --- | --- |
| `test_library.py` | Original behaviour, kept as regression tests |
| `test_copies_and_loans.py` | Copies, concurrent borrowing, member checks, ID validation, loan history |
| `test_storage.py` | Restarts, corrupted data, write failures, process crashes, schema rules |
| `test_legacy_import.py` | Importing the old JSON format |
| `test_main.py` | The real program, run as a separate process |
| `test_reliability.py` | Thread and process races, failure injection at every step, crash recovery, corrupted records, schema upgrades |

`test_reliability.py` starts real processes, so it takes longer than the others.
Races are coordinated with barriers and events, not timed sleeps. Thread and
process races can still interleave differently from run to run, but the expected
result is the same for every interleaving.

## Storage

Data is stored in `library.db`, a SQLite database created in the directory you
run the program from. It is excluded from git.

- **Back up:** close the program and copy `library.db`.
- **Reset:** close the program and delete `library.db`. This erases all records.
- **Corrupted database:** the program checks the file at startup. If it is
  damaged, the program prints the reason, exits with status 1 and leaves the file
  untouched. Restore a backup, or move the file aside to start empty.
- **Upgrading from `library.json`:** if `library.db` is empty and a `library.json`
  from the earlier version is present, it is imported automatically at startup.
  If the JSON file is invalid, nothing is imported and the program stops so you
  can fix it. The JSON file is never modified, and it is ignored once the
  database has data.
- **Upgrading the database:** an older `library.db` is upgraded automatically at
  startup, in one transaction. A database from a newer version of the program is
  refused and left untouched.
- **After a crash:** just start the program again. SQLite undoes any unfinished
  transaction using `library.db-journal`, so never delete that file by hand.
  Back it up along with `library.db` if it is present after a crash.

## Project structure

| File | Responsibility |
| --- | --- |
| `main.py` | Menu and console output only |
| `library.py` | Business rules and input validation |
| `storage.py` | SQLite schema, transactions and corruption checks |
| `legacy_import.py` | One-time import of old JSON data |
| `models.py` | `Book`, `Copy`, `Member` and `Loan` records, and ID formats |
| `errors.py` | Exception types |

Dependencies point one way: `main` → `library` → `storage`.

## Design decisions

**SQLite instead of JSON.** Rewriting a JSON file on every change cannot protect
against two running instances overwriting each other's data, and failure-safe
writes would have to be built by hand. SQLite provides atomic commits, crash
recovery, locking between processes and corruption checks, and it ships with
Python.

**No state held in memory.** `Library` reads from the store on each call, and
every change runs in one transaction. A failed write rolls back, so memory and
disk cannot disagree, and a change made by another running instance is seen
straight away.

**Rules are enforced in two layers.** `Library` validates input and gives clear
errors. The schema independently refuses invalid records: a copy with two active
loans, a member holding two copies of a title, a copy that belongs to a different
book, invalid dates, or deleting loan history. A code bug therefore cannot store
an invalid loan.

**Writes take the lock first.** Transactions use `BEGIN IMMEDIATE`, so a check
such as "is this copy free?" stays true until the commit, even when two instances
act at the same moment. ID generation runs inside the same lock, so concurrent
creation cannot produce duplicate IDs.

**Busy databases are retried as whole operations.** Every `Library` change is one
function passed to `SqliteStore.atomic()`. SQLite waits up to 5 seconds for a lock
on each attempt. If it is still locked, the transaction rolls back and the whole
function runs again, up to 5 attempts with random backoff, so every check is
repeated against current data. After that, `DatabaseBusyError` reaches the caller.
Other failures are not retried, because they would not be expected to clear.

**History can only move forward.** Triggers allow a loan one change: closing it,
once. Copies cannot be deleted or changed, so a copy ID always means one physical
item.

**Rollback journal rather than WAL.** WAL would let reads continue during a write,
but it adds extra files that must be backed up together. This workload is small
and writes are short, so retries handle the contention.

**Rule violations and system failures are separate.** `LibraryError` subclasses
are mistakes the user can correct. `StorageError` means the data could not be read
or written. The menu reports both and keeps running. Any other exception is a bug
and is deliberately left to crash with a traceback, after its transaction has
rolled back.

**Corrupted data is never repaired automatically.** Refusing to start is safer
than guessing or overwriting a file that might still be recoverable.

**A member can hold one copy of a title at a time.** This rule comes from the
original version and is kept. Because of it, a book ID and member ID identify a
loan exactly, so `return_book` needs a copy ID only if you want to name the copy.

**Returned loans imported from JSON have no copy ID.** The old format never
recorded a copy, so these records show none rather than an invented one. Active
loans are assigned real copies because the system must track them.

**`Library` depends on `SqliteStore` directly.** It is passed in through the
constructor, but there is no abstract storage interface, because there is only one
implementation. One can be extracted if a second is ever needed.

## Known limitations

- Books, copies and members cannot be edited or deleted. Lost or damaged copies
  cannot be retired.
- There are no due dates, fines, reservations or borrowing limits.
- Adding the same title twice creates two separate books. Use "Add copies of a
  book" for more copies of an existing title.
- There are no user accounts, so anyone at the menu can act as any member. Member
  verification checks the ID entered, not a person's identity.
- There are no automatic backups. A corrupted database can only be restored from a
  copy you made yourself.
- An empty `library.db` (0 bytes) is treated as a new database, because SQLite
  cannot tell it apart from a new file.
- Integrity checks run in full at startup, which will slow down on very large
  databases. They do not run again while the program is open.
- If the database stays locked through every retry, the operation fails with
  `DatabaseBusyError`, changes nothing and can be tried again. All writes run one
  at a time, so throughput is limited by disk commit speed.
- A disk failure reported during a commit is treated as a failed write. Whether
  committed data really reached the disk depends on the disk and operating system
  honouring flush requests.
- Rules in the schema protect against code bugs and ordinary SQL. Someone editing
  the file with SQL tools can still remove the triggers. Such damage is detected
  at startup only if it breaks a constraint.
- Integrity checks of the whole file run only at startup. Bad data written by
  another tool while the program runs is reported when a read reaches it.
- SQLite locking is unreliable on network drives, so keep `library.db` on a local
  disk.
- Dates come from the local system clock. If the clock moves backwards, a return
  dated before its issue date is refused.
