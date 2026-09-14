import unittest
from pathlib import Path
from unittest.mock import patch

from planiteam_to_coros import Segment, Step, Workout, parse_planiteam_pdf

SAMPLE_VMA = 17.5

# Three real Planiteam PDFs, each in its own folder named after the session
# date (from the notification email, not the PDF - it has no date field).
# They cover the two structurally different pace-table layouts this parser
# has to dispatch between - see CLAUDE.md.
HILL_SPRINTS_PDF = Path("example/2026-09-10/cotes-courtes-2x6x20-.pdf")
HILL_SPRINTS_EXPECTED = Path("example/2026-09-10/expected.workout.intervals.txt")
STAIRS_PDF = Path("example/2026-09-15/travail-escaliers-12-a-15x45-45-sur-boucle-vallonnee.pdf")
STAIRS_EXPECTED = Path("example/2026-09-15/expected.workout.intervals.txt")
PYRAMID_PDF = Path("example/2026-09-17/seuil-pyramide-2-4-6-4-3-2-min.pdf")
PYRAMID_EXPECTED = Path("example/2026-09-17/expected.workout.intervals.txt")


class PlaniteamToCorosTest(unittest.TestCase):
    def test_parse_planiteam_sample_pdf(self):
        workout = parse_planiteam_pdf(HILL_SPRINTS_PDF, vma=SAMPLE_VMA)

        self.assertIsInstance(workout, Workout)
        self.assertIn("CÔTES COURTES", workout.title)
        self.assertEqual(
            [segment.name for segment in workout.segments],
            ["ÉCHAUFFEMENT", "GAMMES", '2X6X20" CÔTES', "RETOUR AU CALME"],
        )

        intervals_text = workout.to_intervals_icu_text()
        expected_text = HILL_SPRINTS_EXPECTED.read_text(encoding="utf-8")
        self.assertEqual(intervals_text, expected_text)

    def test_warmup_and_cooldown_use_vma_derived_pace(self):
        workout = parse_planiteam_pdf(HILL_SPRINTS_PDF, vma=SAMPLE_VMA)

        warmup, gammes, main_set, cooldown = workout.segments

        self.assertEqual(warmup.repeat, 1)
        self.assertEqual(warmup.steps[0].duration_s, 20 * 60)
        self.assertEqual(warmup.steps[0].target, "5:43/km")

        self.assertEqual(cooldown.steps[0].duration_s, 10 * 60)
        self.assertEqual(cooldown.steps[0].target, "6:14/km")

        # No pace data is printed for the drills block, so no target is invented.
        self.assertEqual(gammes.steps[0].duration_s, 10 * 60)
        self.assertIsNone(gammes.steps[0].target)

    def test_warmup_and_cooldown_roles_are_tagged_for_device_step_type(self):
        # Confirmed live against intervals.icu: a bare "Warmup"/"Cooldown"
        # header line is what makes it tag the step for device sync (see
        # CLAUDE.md) - Segment.role drives that header, so it must be set
        # correctly on exactly these two segments.
        workout = parse_planiteam_pdf(HILL_SPRINTS_PDF, vma=SAMPLE_VMA)
        warmup, gammes, main_set, cooldown = workout.segments

        self.assertEqual(warmup.role, "warmup")
        self.assertEqual(cooldown.role, "cooldown")
        self.assertIsNone(gammes.role)
        self.assertIsNone(main_set.role)

        text = workout.to_intervals_icu_text()
        self.assertTrue(text.startswith("Warmup\n"))
        self.assertIn("\n\nCooldown\n", text)

    def test_blocks_get_generic_cue_text_for_device_display(self):
        # Confirmed live: a leading cue word on a step's line becomes that
        # step's block name on the COROS device (see CLAUDE.md). Warm-up and
        # cool-down are deliberately left without one since the warmup/
        # cooldown flag alone already gives them a correctly-localised name.
        workout = parse_planiteam_pdf(HILL_SPRINTS_PDF, vma=SAMPLE_VMA)
        warmup, gammes, main_set, cooldown = workout.segments

        self.assertIsNone(warmup.steps[0].cue)
        self.assertIsNone(cooldown.steps[0].cue)
        self.assertEqual(gammes.steps[0].cue, "Gammes")

        cues = [step.cue for step in main_set.steps]
        self.assertEqual(
            cues,
            ["Effort", "Récupération"] * 5 + ["Effort", "Récupération"],
        )

    def test_main_set_is_two_reps_of_six_efforts_with_final_long_recovery(self):
        workout = parse_planiteam_pdf(HILL_SPRINTS_PDF, vma=SAMPLE_VMA)
        main_set = workout.segments[2]

        self.assertEqual(main_set.repeat, 2)
        durations_and_targets = [(step.duration_s, step.target) for step in main_set.steps]
        effort, recovery = "3:26-3:37/km", "5:16-8:34/km"
        self.assertEqual(
            durations_and_targets,
            [
                (20, effort), (40, recovery),
                (20, effort), (40, recovery),
                (20, effort), (40, recovery),
                (20, effort), (40, recovery),
                (20, effort), (40, recovery),
                (20, effort), (180, recovery),
            ],
        )

    def test_main_set_targets_are_vma_derived_paces_not_intervals_icu_zones(self):
        # intervals.icu always pre-resolves a "Z5 Pace"-style zone target
        # into an absolute pace using its own athlete-side zone config before
        # a workout ever reaches a device (verified at the FIT byte level,
        # see CLAUDE.md), so the main set targets a pace computed directly
        # from the PDF's own %VMA bands and the given VMA instead - this
        # should hold regardless of the athlete's intervals.icu zone setup.
        workout = parse_planiteam_pdf(HILL_SPRINTS_PDF, vma=SAMPLE_VMA)
        main_set = workout.segments[2]

        for step in main_set.steps:
            self.assertNotIn("Z", step.target)
            self.assertIn("/km", step.target)

    def test_total_duration_matches_planiteam_summary(self):
        # The Planiteam UI reports a total duration of 56:40 for this session.
        workout = parse_planiteam_pdf(HILL_SPRINTS_PDF, vma=SAMPLE_VMA)

        total_seconds = 0
        for segment in workout.segments:
            segment_seconds = sum(step.duration_s for step in segment.steps)
            total_seconds += segment_seconds * segment.repeat

        self.assertEqual(total_seconds, 56 * 60 + 40)

    def test_vma_outside_reference_table_clamps_to_nearest_row(self):
        workout = parse_planiteam_pdf(HILL_SPRINTS_PDF, vma=99.0)
        warmup = workout.segments[0]

        # VMA=99 is far above the table's highest row (23): the pace should
        # clamp to that row rather than extrapolate or crash.
        self.assertEqual(warmup.steps[0].target, "4:21/km")

    def test_parse_raises_instead_of_silently_returning_an_empty_workout(self):
        # A real production incident: a differently-laid-out PDF (its
        # section headers didn't wrap onto a second line, shifting the
        # interval grid's coordinates) made the old fixed-coordinate parser
        # find zero blocks, which silently produced an empty Workout - and
        # from there, an empty session pushed to the athlete's watch with no
        # error anywhere in the pipeline. Forcing that same "nothing found"
        # condition here (rather than relying on some future PDF still
        # triggering it) to make sure it now fails loudly instead.
        with patch("planiteam_to_coros._parse_main_row", return_value=[]):
            with self.assertRaises(ValueError):
                parse_planiteam_pdf(HILL_SPRINTS_PDF, vma=SAMPLE_VMA)

    def test_parse_stairs_pdf_with_shifted_grid_coordinates(self):
        # This PDF's section headers don't wrap onto a second line, so its
        # interval grid and pace table sit ~9pt higher on the page than the
        # first sample's - exactly the layout that broke the original
        # fixed-coordinate row detection (see the test above).
        workout = parse_planiteam_pdf(STAIRS_PDF, vma=SAMPLE_VMA)

        intervals_text = workout.to_intervals_icu_text()
        expected_text = STAIRS_EXPECTED.read_text(encoding="utf-8")
        self.assertEqual(intervals_text, expected_text)

        warmup, drills, main_set, cooldown = workout.segments
        self.assertEqual(warmup.role, "warmup")
        self.assertEqual(cooldown.role, "cooldown")
        # Different pace for warm-up vs cool-down, unlike the other two
        # samples where the athlete assumed they'd match - confirmed correct
        # by their alignment with the pace table's own column positions
        # (left column under the warm-up block, right column under cool-down).
        self.assertEqual(warmup.steps[0].target, "5:16/km")
        self.assertEqual(cooldown.steps[0].target, "5:43/km")
        self.assertEqual(main_set.repeat, 15)

    def test_parse_pyramid_pdf_with_per_step_pace_table(self):
        # No Z-zone/%VMA overlay at all in this PDF - instead one pace-table
        # column per session phase (warm-up, each of the 11 main-set
        # entries, cool-down), a structurally different layout from the
        # other two samples.
        workout = parse_planiteam_pdf(PYRAMID_PDF, vma=SAMPLE_VMA)

        intervals_text = workout.to_intervals_icu_text()
        expected_text = PYRAMID_EXPECTED.read_text(encoding="utf-8")
        self.assertEqual(intervals_text, expected_text)

        warmup, main_set, cooldown = workout.segments
        self.assertEqual(warmup.role, "warmup")
        self.assertEqual(cooldown.role, "cooldown")

        # The PDF's own repeat marker for the pyramid is "1x" (a single
        # pass) - not repeated, unlike the hill-sprints main set.
        self.assertEqual(main_set.repeat, 1)
        self.assertEqual(len(main_set.steps), 11)

        # Alternates Effort/Récupération purely by position, since there's
        # no zone data here to classify entries by.
        cues = [step.cue for step in main_set.steps]
        self.assertEqual(cues, ["Effort", "Récupération"] * 5 + ["Effort"])

        # Durations that aren't a whole number of minutes render as combined
        # "1m15s"-style tokens, not raw seconds.
        durations = [step.duration_s for step in main_set.steps]
        self.assertIn(75, durations)  # 1:15 recovery
        self.assertIn("1m15s", intervals_text)

    def test_multi_step_segment_gets_repeat_header_even_at_1x(self):
        # The athlete's own preference (confirmed live: verified a "1x"
        # header parses correctly, workout_doc shows {"reps": 1, ...}): a
        # multi-step "core" block reads as a repeat of one, even with no
        # warm-up/cool-down around it - not a bare list of steps. Verified
        # against the pyramid PDF's real main set, whose own repeat marker
        # is "1x". A single-step segment (warm-up, cool-down, a plain drills
        # block) never gets a header regardless of its repeat count -
        # there's nothing to group.
        multi_step = Segment(name="Core", repeat=1, steps=[Step(duration_s=30), Step(duration_s=30)])
        single_step = Segment(name="Drills", repeat=1, steps=[Step(duration_s=600)])
        text = Workout(title="t", segments=[multi_step, single_step]).to_intervals_icu_text()

        self.assertTrue(text.startswith("1x\n"))
        self.assertNotIn("1x\n- 10m", text)  # the single-step segment stays unwrapped


if __name__ == "__main__":
    unittest.main()
