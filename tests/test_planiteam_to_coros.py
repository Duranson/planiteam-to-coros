import unittest
from pathlib import Path

from planiteam_to_coros import Workout, parse_planiteam_pdf

SAMPLE_PDF = Path("example/cotes-courtes-2x6x20-.pdf")
SAMPLE_VMA = 17.5


class PlaniteamToCorosTest(unittest.TestCase):
    def test_parse_planiteam_sample_pdf(self):
        workout = parse_planiteam_pdf(SAMPLE_PDF, vma=SAMPLE_VMA)

        self.assertIsInstance(workout, Workout)
        self.assertIn("CÔTES COURTES", workout.title)
        self.assertEqual(
            [segment.name for segment in workout.segments],
            ["ÉCHAUFFEMENT", "GAMMES", '2X6X20" CÔTES', "RETOUR AU CALME"],
        )

        intervals_text = workout.to_intervals_icu_text()
        expected_text = Path("example/expected.workout.intervals.txt").read_text(encoding="utf-8")
        self.assertEqual(intervals_text, expected_text)

    def test_warmup_and_cooldown_use_vma_derived_pace(self):
        workout = parse_planiteam_pdf(SAMPLE_PDF, vma=SAMPLE_VMA)

        warmup, gammes, main_set, cooldown = workout.segments

        self.assertEqual(warmup.repeat, 1)
        self.assertEqual(warmup.steps[0].duration_s, 20 * 60)
        self.assertEqual(warmup.steps[0].target, "5:43/km")

        self.assertEqual(cooldown.steps[0].duration_s, 10 * 60)
        self.assertEqual(cooldown.steps[0].target, "6:14/km")

        # No pace data is printed for the drills block, so no target is invented.
        self.assertEqual(gammes.steps[0].duration_s, 10 * 60)
        self.assertIsNone(gammes.steps[0].target)

    def test_main_set_is_two_reps_of_six_efforts_with_final_long_recovery(self):
        workout = parse_planiteam_pdf(SAMPLE_PDF, vma=SAMPLE_VMA)
        main_set = workout.segments[2]

        self.assertEqual(main_set.repeat, 2)
        durations_and_targets = [(step.duration_s, step.target) for step in main_set.steps]
        self.assertEqual(
            durations_and_targets,
            [
                (20, "Z5"), (40, "Z1"),
                (20, "Z5"), (40, "Z1"),
                (20, "Z5"), (40, "Z1"),
                (20, "Z5"), (40, "Z1"),
                (20, "Z5"), (40, "Z1"),
                (20, "Z5"), (180, "Z1"),
            ],
        )

    def test_total_duration_matches_planiteam_summary(self):
        # The Planiteam UI reports a total duration of 56:40 for this session.
        workout = parse_planiteam_pdf(SAMPLE_PDF, vma=SAMPLE_VMA)

        total_seconds = 0
        for segment in workout.segments:
            segment_seconds = sum(step.duration_s for step in segment.steps)
            total_seconds += segment_seconds * segment.repeat

        self.assertEqual(total_seconds, 56 * 60 + 40)

    def test_vma_outside_reference_table_clamps_to_nearest_row(self):
        workout = parse_planiteam_pdf(SAMPLE_PDF, vma=99.0)
        warmup = workout.segments[0]

        # VMA=99 is far above the table's highest row (23): the pace should
        # clamp to that row rather than extrapolate or crash.
        self.assertEqual(warmup.steps[0].target, "4:21/km")


if __name__ == "__main__":
    unittest.main()
