import json
import os
import tempfile
import unittest
from datetime import date

from errors import CorruptedDataError, StorageError
from legacy_import import import_legacy_json
from library import Library
from storage import SqliteStore


def legacy_data():
    return {
        "books": [{"id": "B1", "title": "Dune", "author": "Frank Herbert",
                   "copies": 2}],
        "members": [{"id": "M1", "name": "Ana"}, {"id": "M2", "name": "Ben"}],
        "loans": [
            {"book_id": "B1", "member_id": "M1", "issued_on": "2026-01-01",
             "returned_on": "2026-01-05"},
            {"book_id": "B1", "member_id": "M2", "issued_on": "2026-02-01",
             "returned_on": None},
        ],
    }


class LegacyImportTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, "library.json")
        self.store = SqliteStore()
        self.addCleanup(self.store.close)
        self.library = Library(self.store)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as file:
            file.write(text)

    def test_all_records_are_imported(self):
        self.write(json.dumps(legacy_data()))
        import_legacy_json(self.path, self.store)

        self.assertEqual(self.library.get_book("B1").title, "Dune")
        self.assertEqual(self.library.total_copies("B1"), 2)
        self.assertEqual(self.library.available_copies("B1"), 1)
        self.assertEqual(len(self.library.list_members()), 2)

        returned, active = self.library.loan_history()
        self.assertIsNone(returned.copy_id)
        self.assertEqual(returned.returned_on, date(2026, 1, 5))
        self.assertEqual(active.copy_id, "B1-C1")
        self.assertEqual(active.issued_on, date(2026, 2, 1))

    def test_imported_loan_can_be_returned(self):
        self.write(json.dumps(legacy_data()))
        import_legacy_json(self.path, self.store)
        self.library.return_book("B1", "M2")
        self.assertEqual(self.library.available_copies("B1"), 2)

    def test_source_file_is_left_unchanged(self):
        text = json.dumps(legacy_data())
        self.write(text)
        import_legacy_json(self.path, self.store)
        with open(self.path, encoding="utf-8") as file:
            self.assertEqual(file.read(), text)

    def test_malformed_files_import_nothing(self):
        def mutated(change):
            data = legacy_data()
            change(data)
            return json.dumps(data)

        bad_files = {
            "not json": "{ not json",
            "empty": "",
            "wrong shape": "[1, 2, 3]",
            "missing section": mutated(lambda d: d.pop("members")),
            "missing field": mutated(lambda d: d["books"][0].pop("title")),
            "bad id": mutated(lambda d: d["books"][0].update(id="X")),
            "bad copies": mutated(lambda d: d["books"][0].update(copies=0)),
            "bad date": mutated(lambda d: d["loans"][0].update(issued_on="soon")),
            "date type": mutated(lambda d: d["loans"][0].update(issued_on=5)),
            "return before issue": mutated(
                lambda d: d["loans"][0].update(returned_on="2025-01-01")),
            "unknown member": mutated(lambda d: d["loans"][0].update(member_id="M9")),
            "duplicate book": mutated(lambda d: d["books"].append(d["books"][0])),
            "too many active loans": mutated(
                lambda d: d["books"][0].update(copies=1) or d["loans"].append(
                    {"book_id": "B1", "member_id": "M1",
                     "issued_on": "2026-02-01", "returned_on": None})),
            "duplicate active loan": mutated(lambda d: d["loans"].append(d["loans"][1])),
        }
        for name, text in bad_files.items():
            with self.subTest(name):
                self.write(text)
                with self.assertRaises(CorruptedDataError):
                    import_legacy_json(self.path, self.store)
                self.assertTrue(self.store.is_empty())

    def test_write_failure_midway_imports_nothing(self):
        self.write(json.dumps(legacy_data()))
        self.store._run("CREATE TRIGGER no_loans BEFORE INSERT ON loans "
                        "BEGIN SELECT RAISE(ABORT, 'simulated'); END")
        with self.assertRaises(StorageError):
            import_legacy_json(self.path, self.store)
        self.assertTrue(self.store.is_empty())

    def test_never_imports_over_existing_data(self):
        self.library.register_member("Existing")
        self.write(json.dumps(legacy_data()))
        with self.assertRaises(StorageError):
            import_legacy_json(self.path, self.store)
        self.assertEqual(len(self.library.list_members()), 1)


if __name__ == "__main__":
    unittest.main()
