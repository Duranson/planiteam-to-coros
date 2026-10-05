import unittest
from pathlib import Path
from unittest.mock import patch

from planiteam_to_coros import (
    Segment,
    Step,
    Workout,
    parse_planiteam_pdf,
    parse_recipients,
    push_to_recipients,
)

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

# A second round of real PDFs (see CLAUDE.md "A third round of real PDFs
# broke the flat-count/sequential-index approach"). Their hand-typed
# expected.workout.intervals.txt files have a handful of small, independently
# confirmed errors (wrong duration copied from a different session, "mt"
# instead of the real "mtr" distance suffix, a single pace value instead of
# the API-verified range, ...) - see CLAUDE.md's "Hand-typed expected.*.txt
# files had several more small errors" for the full list. Rather than assert
# full-text equality against those files, the tests below assert the
# specific corrected values directly.
VMA_SPLIT_PDF = Path("example/2026-09-22/vma-6x500m-6x300m-avec-relances.pdf")
COTES_TEMPO_PDF = Path("example/2026-09-24/cotes-6-a-8x500m-tempo-sur-du-plat.pdf")
COTES_TWO_SERIES_PDF = Path("example/2026-09-29/cotes-2x6x40-.pdf")
SEUIL_PYRAMID_WITH_GAMMES_PDF = Path("example/2026-10-01/seuil-12-8-6-3-.pdf")
COTES_LONGUES_PDF = Path("example/2026-10-06/cotes-longues-3x-2-1-30-1-.pdf")

# This one PDF's own expected file is fully correct - exercises a shifted
# VMA-label table column and a plain distance-based main set end to end.
VMA_1000_PDF = Path("example/2026-10-08/6-a-8x1000m-avec-relances.pdf")
VMA_1000_EXPECTED = Path("example/2026-10-08/expected.workout.intervals.txt")


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

        # "Cotes" is appended to the effort cue because this block's own
        # section name mentions hill repeats ("CÔTES") - see CLAUDE.md.
        cues = [step.cue for step in main_set.steps]
        self.assertEqual(
            cues,
            ["Effort Cotes", "Récupération"] * 5 + ["Effort Cotes", "Récupération"],
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

    def test_parse_vma_split_pdf_with_distance_based_entries_and_raw_pace_columns(self):
        # No Z-zone overlay at all here - the main-set entries are distances
        # ("500m"/"300m"), targeted from a raw (non-"/km") pace-table column
        # that has to be rescaled by the entry's own distance - see CLAUDE.md
        # "The pace table can have a third column kind".
        workout = parse_planiteam_pdf(VMA_SPLIT_PDF, vma=SAMPLE_VMA)
        warmup, gammes, main_500, inter_block, main_300, cooldown = workout.segments

        self.assertEqual(main_500.steps[0].distance_m, 500)
        self.assertIsNone(main_500.steps[0].duration_s)
        self.assertEqual(main_500.steps[0].cue, "Effort")
        self.assertEqual(main_500.steps[0].target, "3:26/km")
        # The per-rep jog has no pace-table column of its own - left untargeted.
        self.assertEqual(main_500.steps[1].cue, "Récupération")
        self.assertIsNone(main_500.steps[1].target)

        self.assertEqual(main_300.steps[0].distance_m, 300)
        self.assertEqual(main_300.steps[0].target, "3:27/km")

        # The inter-block rest is a plain, untargeted block - not folded into
        # either 6x set.
        self.assertIsNone(inter_block.role)
        self.assertEqual(len(inter_block.steps), 1)
        self.assertIsNone(inter_block.steps[0].target)

        self.assertEqual(cooldown.role, "cooldown")
        self.assertEqual(cooldown.steps[0].duration_s, 10 * 60)

        text = workout.to_intervals_icu_text()
        self.assertIn("500mtr 3:26/km Pace", text)
        self.assertNotIn("500mt ", text)  # the real intervals.icu unit is "mtr", not "mt"

    def test_parse_cotes_tempo_pdf_with_single_zone_block_and_final_tempo_block(self):
        # A single Z-zone token (no paired recovery zone) covers the whole
        # "8x 500m en côtes" block - its attached jog recovery has nothing to
        # borrow a pace from, so it must stay untargeted (see CLAUDE.md "A
        # trailing rest only gets a pace if the block actually uses a second
        # zone"). The session ends on a bare tempo block, not a cool-down.
        workout = parse_planiteam_pdf(COTES_TEMPO_PDF, vma=SAMPLE_VMA)
        names = [s.name for s in workout.segments]
        self.assertNotIn("cooldown", [s.role for s in workout.segments])

        main_set = next(s for s in workout.segments if s.steps and s.steps[0].cue == "Effort Cotes")
        self.assertEqual(main_set.steps[0].distance_m, 500)
        self.assertIsNone(main_set.steps[1].target)  # the 2:00 jog recovery

        tempo = workout.segments[-1]
        self.assertIsNone(tempo.role)
        self.assertEqual(len(tempo.steps), 1)
        self.assertEqual(tempo.steps[0].duration_s, 15 * 60)
        self.assertIsNotNone(tempo.steps[0].target)

    def test_parse_cotes_pdf_with_two_independently_zoned_blocks(self):
        # Two separate "6x40 côtes" series, each its own repeat block with
        # its own single Z5/95-100% token pair - the flat whole-page zone
        # list must not be shared/mismatched between them (see CLAUDE.md "A
        # block can have its own zone/percent overlay independent of other
        # blocks").
        workout = parse_planiteam_pdf(COTES_TWO_SERIES_PDF, vma=SAMPLE_VMA)
        series_blocks = [s for s in workout.segments if s.steps and s.steps[0].cue == "Effort Cotes"]
        self.assertEqual(len(series_blocks), 2)
        for series in series_blocks:
            self.assertEqual(series.steps[0].duration_s, 40)
            self.assertEqual(series.steps[0].target, "3:26-3:37/km")
            self.assertEqual(series.steps[1].cue, "Récupération")
            self.assertIsNone(series.steps[1].target)

    def test_parse_pdf_with_combined_warmup_and_drills_header(self):
        # "ÉCHAUFFEMENT + GAMMES" is one centred header over two distinct
        # duration-row entries sharing a single "1x" marker - must split into
        # a real Warmup segment and a separate, untargeted Gammes segment,
        # not one segment with the warm-up's pace wrongly applied to both
        # entries. See CLAUDE.md "A '+'-joined header can span two different
        # entries".
        workout = parse_planiteam_pdf(SEUIL_PYRAMID_WITH_GAMMES_PDF, vma=SAMPLE_VMA)
        warmup, gammes, main_set, cooldown = workout.segments

        self.assertEqual(warmup.role, "warmup")
        self.assertEqual(len(warmup.steps), 1)
        self.assertEqual(warmup.steps[0].duration_s, 20 * 60)
        self.assertIsNotNone(warmup.steps[0].target)

        self.assertIsNone(gammes.role)
        self.assertEqual(gammes.steps[0].duration_s, 5 * 60)
        self.assertIsNone(gammes.steps[0].target)

        self.assertEqual(len(main_set.steps), 7)
        cues = [step.cue for step in main_set.steps]
        self.assertEqual(cues, ["Effort", "Récupération"] * 3 + ["Effort"])

    def test_parse_cotes_longues_pdf_with_per_entry_rest_in_one_zoned_block(self):
        # Three distinct durations in one "3x" block all share the *same*
        # single zone (Z5/95-100%) - each entry's own jog recovery must stay
        # untargeted (no second zone anywhere in this block to borrow from),
        # even though every entry individually has a rest attached (unlike
        # the original sample, where only the trailing entry does).
        workout = parse_planiteam_pdf(COTES_LONGUES_PDF, vma=SAMPLE_VMA)
        main_set = next(s for s in workout.segments if s.role is None and len(s.steps) > 1)

        self.assertEqual([step.duration_s for step in main_set.steps], [120, 75, 90, 60, 60, 120])
        self.assertEqual([step.cue for step in main_set.steps], ["Effort Cotes", "Récupération"] * 3)
        for step in main_set.steps:
            if step.cue == "Effort Cotes":
                self.assertEqual(step.target, "3:26-3:37/km")
            else:
                self.assertIsNone(step.target)

    def test_parse_vma_1000_pdf_with_shifted_pace_table_labels(self):
        # This PDF's longer title shifts the whole VMA reference table to
        # the right (labels at x0~123 instead of the usual ~37-65) - an
        # earlier hardcoded "x0 < 90" check found zero rows here, silently
        # dropping pace from the entire workout. See CLAUDE.md "VMA
        # reference table label column position is not fixed either". This
        # file's own expected output is fully correct, unlike its neighbours.
        workout = parse_planiteam_pdf(VMA_1000_PDF, vma=SAMPLE_VMA)

        intervals_text = workout.to_intervals_icu_text()
        expected_text = VMA_1000_EXPECTED.read_text(encoding="utf-8")
        self.assertEqual(intervals_text, expected_text)

        warmup, main_set, cooldown = workout.segments
        self.assertEqual(main_set.steps[0].distance_m, 1000)
        self.assertEqual(main_set.steps[0].target, "3:54/km")

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

    def test_parse_recipients_reads_valid_list(self):
        raw = """
        [
            {"name": "Fabien", "vma": 17.5, "api_key": "k1", "athlete_id": "a1"},
            {"name": "Alex", "vma": 15.0, "api_key": "k2", "athlete_id": "a2", "enabled": false}
        ]
        """
        recipients = parse_recipients(raw)

        self.assertEqual([r.name for r in recipients], ["Fabien", "Alex"])
        self.assertEqual(recipients[0].vma, 17.5)
        self.assertTrue(recipients[0].enabled)  # defaults to True when omitted
        self.assertFalse(recipients[1].enabled)

    def test_parse_recipients_rejects_malformed_json(self):
        with self.assertRaises(ValueError):
            parse_recipients("not json")

    def test_parse_recipients_rejects_entry_missing_a_field(self):
        # A typo'd/incomplete entry should fail loudly rather than silently
        # dropping someone from the club-wide push.
        with self.assertRaises(ValueError):
            parse_recipients('[{"name": "Fabien", "vma": 17.5, "api_key": "k1"}]')

    def test_push_to_recipients_skips_disabled_and_isolates_failures(self):
        # One recipient's push failing (bad key, network hiccup) must not
        # stop the others from getting pushed - see CLAUDE.md/DEPLOYMENT.md
        # on why per-recipient failures are isolated rather than aborting
        # the whole club-wide run.
        recipients = parse_recipients(
            """
            [
                {"name": "Works", "vma": 17.5, "api_key": "k1", "athlete_id": "a1"},
                {"name": "Broken", "vma": 15.0, "api_key": "k2", "athlete_id": "a2"},
                {"name": "Paused", "vma": 16.0, "api_key": "k3", "athlete_id": "a3", "enabled": false}
            ]
            """
        )

        def fake_push_event(workout, *, date, athlete_id, api_key, sport="Run", base_url=None):
            if athlete_id == "a2":
                raise RuntimeError("401 Unauthorized")
            return {"id": 42}

        with patch("planiteam_to_coros.push_event", side_effect=fake_push_event):
            results = push_to_recipients(HILL_SPRINTS_PDF, date="2026-09-10", recipients=recipients)

        self.assertEqual([r.name for r in results], ["Works", "Broken"])  # Paused is skipped entirely
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].event_id, 42)
        self.assertFalse(results[1].ok)
        self.assertIn("401", results[1].error)


if __name__ == "__main__":
    unittest.main()
