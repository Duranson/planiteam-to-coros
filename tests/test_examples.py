"""One test per ``example/<date>/`` folder - discoverable in VSCode's Testing
tab (and via ``python -m unittest``) as a separate, individually-runnable
test for every folder, with no code change needed when a new one is added.

Each case parses that folder's PDF, renders it to intervals.icu text, saves
that output next to the PDF as ``actual.workout.intervals.txt`` (always,
whether the test passes or fails - open it side by side with
``expected.workout.intervals.txt``, or just ``git diff``/the editor's diff
view, to see exactly what's wrong), and compares it against that folder's
hand-checked ``expected.workout.intervals.txt``. See CLAUDE.md for how those
expected files were themselves checked against the source PDFs - a mismatch
here is sometimes the parser being wrong, sometimes the expected file being
wrong.

Test methods are attached to the class dynamically at import time (one
``setattr`` per folder, below), which is the one requirement for a test
runner (VSCode's included) to discover them as distinct test cases - they
must exist by the time the class finishes loading, not be added lazily
inside a single test method.
"""

import re
import unittest
from pathlib import Path

from planiteam_to_coros import parse_planiteam_pdf

EXAMPLE_ROOT = Path(__file__).resolve().parent.parent / "example"
ACTUAL_FILENAME = "actual.workout.intervals.txt"


def _discover_example_cases():
    """Return ``(folder_name, pdf_path, expected_path)`` for every
    ``example/<folder>/`` that has both a PDF and an expected file.

    Folders missing either (e.g. a brand new one dropped in before its
    expected file has been hand-checked) are silently skipped rather than
    failing collection - a WIP folder shouldn't block every other test from
    being discoverable.
    """

    cases = []
    if not EXAMPLE_ROOT.is_dir():
        return cases
    for folder in sorted(p for p in EXAMPLE_ROOT.iterdir() if p.is_dir()):
        expected_path = folder / "expected.workout.intervals.txt"
        pdf_paths = sorted(folder.glob("*.pdf"))
        if not expected_path.is_file() or not pdf_paths:
            continue
        cases.append((folder.name, pdf_paths[0], expected_path))
    return cases


def _test_method_name(folder_name: str) -> str:
    # Folder names are dates ("2026-09-10"); a valid Python identifier can't
    # start with a digit or contain "-".
    return "test_" + re.sub(r"\W", "_", folder_name)


def _make_test(pdf_path: Path, expected_path: Path):
    def test(self):
        actual_path = expected_path.with_name(ACTUAL_FILENAME)
        workout = parse_planiteam_pdf(pdf_path)
        actual = workout.to_intervals_icu_text()
        # Written before the assertion, so it's on disk to inspect even
        # when the comparison below fails.
        actual_path.write_text(actual, encoding="utf-8")

        expected = expected_path.read_text(encoding="utf-8")
        self.assertEqual(
            actual,
            expected,
            f"{pdf_path.name} did not render to {expected_path.name} - see {actual_path} for the full output.",
        )

    test.__doc__ = f"{pdf_path.name} renders to {expected_path.name}"
    return test


class ExamplePdfConversionTest(unittest.TestCase):
    maxDiff = None


for _folder_name, _pdf_path, _expected_path in _discover_example_cases():
    setattr(ExamplePdfConversionTest, _test_method_name(_folder_name), _make_test(_pdf_path, _expected_path))


if __name__ == "__main__":
    unittest.main()
