"""End-to-end tests that run the real program as a separate process."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

MAIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")


class MenuTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name

    def run_menu(self, *lines):
        return subprocess.run(
            [sys.executable, MAIN],
            input="\n".join(lines) + "\n",
            capture_output=True, text=True, cwd=self.folder, timeout=30)

    def file_path(self, name):
        return os.path.join(self.folder, name)

    def test_full_workflow_across_restarts(self):
        first = self.run_menu(
            "1", "Dune", "Frank Herbert", "2",
            "2", "Ana",
            "2", "Ben",
            "3", "b1", "m1", "",
            "3", "B1", "M2", "b1-c1",
            "4", "B1", "M2", "",
            "0")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("Added B1", first.stdout)
        self.assertIn("Issued B1-C1 to M1", first.stdout)
        self.assertIn("Error: Copy B1-C1 is already on loan.", first.stdout)
        self.assertIn("Error: Ben cannot return 'Dune'", first.stdout)

        second = self.run_menu("5", "6", "7", "4", "B1", "M1", "", "9", "", "", "0")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("(1/2 available)", second.stdout)
        self.assertIn("M2: Ben", second.stdout)
        self.assertIn("B1-C1 'Dune' -> M1 Ana", second.stdout)
        self.assertIn("Returned B1-C1 from M1", second.stdout)
        self.assertRegex(second.stdout, r"B1-C1 -> M1: issued \S+, returned \d")

    def test_add_copies_and_bad_input(self):
        result = self.run_menu(
            "1", "Dune", "Frank Herbert", "abc",
            "1", "Dune", "Frank Herbert", "",
            "8", "B1", "2",
            "3", "nonsense", "M1", "",
            "42",
            "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Error: Copies must be a whole number.", result.stdout)
        self.assertIn("Added B1-C2, B1-C3", result.stdout)
        self.assertIn("Error: Invalid book ID: 'nonsense'.", result.stdout)
        self.assertIn("Invalid choice.", result.stdout)

    def test_end_of_input_exits_cleanly(self):
        self.assertEqual(self.run_menu("5").returncode, 0)

    def test_corrupted_database_stops_startup_and_is_preserved(self):
        with open(self.file_path("library.db"), "wb") as file:
            file.write(b"garbage" * 1000)
        result = self.run_menu("0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Cannot start", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        with open(self.file_path("library.db"), "rb") as file:
            self.assertEqual(file.read(), b"garbage" * 1000)

    def test_legacy_file_is_imported_once(self):
        data = {
            "books": [{"id": "B1", "title": "Dune", "author": "F", "copies": 1}],
            "members": [{"id": "M1", "name": "Ana"}],
            "loans": [],
        }
        with open(self.file_path("library.json"), "w", encoding="utf-8") as file:
            json.dump(data, file)

        first = self.run_menu("5", "0")
        self.assertIn("Imported existing data", first.stdout)
        self.assertIn("B1: Dune", first.stdout)

        second = self.run_menu("0")
        self.assertNotIn("Imported", second.stdout)

    def test_malformed_legacy_file_stops_startup_and_retries_next_time(self):
        with open(self.file_path("library.json"), "w", encoding="utf-8") as file:
            file.write("{ broken")
        result = self.run_menu("0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("library.json", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        with open(self.file_path("library.json"), "w", encoding="utf-8") as file:
            json.dump({"books": [], "members": [{"id": "M1", "name": "Ana"}],
                       "loans": []}, file)
        fixed = self.run_menu("6", "0")
        self.assertEqual(fixed.returncode, 0, fixed.stderr)
        self.assertIn("M1: Ana", fixed.stdout)


if __name__ == "__main__":
    unittest.main()
