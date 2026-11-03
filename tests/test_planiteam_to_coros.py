"""Unit tests for what the example-folder tests (test_examples.py) can't show:
behaviour at other VMAs, failing loudly on unknown layouts, rendering syntax,
and the recipients/push side."""

import unittest
from pathlib import Path
from unittest.mock import patch

import planiteam_to_coros as p
from intervals_icu import parse_recipients, push_to_recipients
from planiteam_to_coros import Segment, Step, Workout, parse_planiteam_pdf

HILL_SPRINTS_PDF = Path("example/2026-09-10/cotes-courtes-2x6x20-.pdf")
STAIRS_PDF = Path("example/2026-09-15/travail-escaliers-12-a-15x45-45-sur-boucle-vallonnee.pdf")
PYRAMID_PDF = Path("example/2026-09-17/seuil-pyramide-2-4-6-4-3-2-min.pdf")
VMA_SPLIT_PDF = Path("example/2026-09-22/vma-6x500m-6x300m-avec-relances.pdf")
VMA_1000_PDF = Path("example/2026-10-08/6-a-8x1000m-avec-relances.pdf")


def _page(blocks, zones=(), bands=(), columns=()):
    return p._Page(title="t", blocks=blocks, zones=list(zones), bands=list(bands), columns=list(columns))


class ParsingTest(unittest.TestCase):
    def test_hill_sprints_structure_matches_planiteam_total(self):
        workout = parse_planiteam_pdf(HILL_SPRINTS_PDF)
        warmup, gammes, main_set, cooldown = workout.segments

        self.assertIn("CÔTES COURTES", workout.title)
        self.assertEqual([s.role for s in workout.segments], ["warmup", None, None, "cooldown"])
        self.assertEqual(main_set.repeat, 2)
        self.assertEqual([step.duration_s for step in main_set.steps], [20, 40] * 5 + [20, 180])
        # Planiteam's own UI shows 56:40 for this session.
        total = sum(sum(step.duration_s for step in s.steps) * s.repeat for s in workout.segments)
        self.assertEqual(total, 56 * 60 + 40)

    def test_pace_table_columns_resolve_to_vma_percentages(self):
        # Every pace-table column is a constant %VMA - including split-time
        # columns, rescaled by the entry's own distance.
        pyramid = parse_planiteam_pdf(PYRAMID_PDF).segments[1].steps
        self.assertEqual([s.pace_pct for s in pyramid[:3]], [(90, 90), None, (85, 85)])
        self.assertEqual(parse_planiteam_pdf(VMA_SPLIT_PDF).segments[2].steps[0].pace_pct, (100, 100))
        self.assertEqual(parse_planiteam_pdf(VMA_1000_PDF).segments[1].steps[0].pace_pct, (88, 88))

    def test_paces_scale_with_vma(self):
        pyramid = parse_planiteam_pdf(PYRAMID_PDF)
        self.assertIn("- Effort 2m 3:20/km Pace", pyramid.to_intervals_icu_text(20))  # 90% of 20 km/h
        stairs = parse_planiteam_pdf(STAIRS_PDF)
        self.assertIn("- Effort 45s 3:00-3:09/km Pace", stairs.to_intervals_icu_text(20))  # 95-100%

    def test_effort_and_recovery_come_from_vma_percentage_not_position(self):
        # A block opening on a 55% column is a recovery, whatever its position.
        columns = [p._PaceColumn(100, [(20.0, 327, False)]), p._PaceColumn(200, [(20.0, 200, False)])]
        block = p._Block("SEUIL", 1, [p._Entry(100, duration_s=60), p._Entry(200, duration_s=120)])
        steps = p._build_segments(_page([block], columns=columns))[0].steps
        self.assertEqual([(s.cue, s.pace_pct) for s in steps], [("Récupération", None), ("Effort", (90, 90))])

    def test_cotes_get_zone_in_cue_and_no_pace(self):
        main_set = parse_planiteam_pdf(HILL_SPRINTS_PDF).segments[2]
        # The trailing 3:00 rest has no zone label of its own.
        self.assertEqual(
            [s.cue for s in main_set.steps],
            ["Effort Côtes (Z5)", "Récupération (Z1)"] * 5 + ["Effort Côtes (Z5)", "Récupération"],
        )
        self.assertTrue(all(s.pace_pct is None for s in main_set.steps))


class FailsLoudlyTest(unittest.TestCase):
    def test_interval_step_with_no_intensity_source_raises(self):
        block = p._Block("SÉRIE", 2, [p._Entry(100, duration_s=60), p._Entry(200, duration_s=60)])
        with self.assertRaises(ValueError):
            p._build_segments(_page([block]))

    def test_header_and_block_count_mismatch_raises(self):
        real = p._parse_section_headers
        with patch("planiteam_to_coros._parse_section_headers", side_effect=lambda words: real(words) + ["EXTRA"]):
            with self.assertRaises(ValueError):
                parse_planiteam_pdf(HILL_SPRINTS_PDF)

    def test_unknown_duration_token_raises(self):
        with self.assertRaises(ValueError):
            p._parse_entry({"text": "Fartlek", "x0": 0})

    def test_zero_segments_raises(self):
        # A production incident once pushed an empty session to the watch.
        with patch("planiteam_to_coros._build_segments", return_value=[]):
            with self.assertRaises(ValueError):
                parse_planiteam_pdf(HILL_SPRINTS_PDF)


class RenderingTest(unittest.TestCase):
    def test_multi_step_segment_gets_repeat_header_even_at_1x(self):
        workout = Workout(
            title="t",
            segments=[
                Segment(name="Core", steps=[Step(duration_s=30), Step(duration_s=30)]),
                Segment(name="Drills", steps=[Step(duration_s=600)]),
            ],
        )
        self.assertEqual(workout.to_intervals_icu_text(17.5), "1x\n- 30s\n- 30s\n\n- 10m")

    def test_role_header_lap_and_distance_syntax(self):
        workout = Workout(
            title="t",
            segments=[
                Segment(name="w", steps=[Step(duration_s=600, cue="Gammes", until_lap_press=True)], role="warmup"),
                Segment(name="m", steps=[Step(distance_m=1000), Step(distance_m=300), Step(duration_s=75)]),
            ],
        )
        self.assertEqual(
            workout.to_intervals_icu_text(17.5),
            "Warmup\n- Gammes Press lap 10m\n\n1x\n- 1km\n- 300mtr\n- 1m15s",
        )

    def test_open_ended_band_cannot_render_as_a_range(self):
        with self.assertRaises(ValueError):
            Step(duration_s=60, pace_pct=(0, 65)).render(17.5)


class RecipientsTest(unittest.TestCase):
    def test_parse_recipients_reads_valid_list(self):
        recipients = parse_recipients(
            '[{"name": "Fabien", "vma": 17.5, "api_key": "k1", "athlete_id": "a1"},'
            ' {"name": "Alex", "vma": 15.0, "api_key": "k2", "athlete_id": "a2", "enabled": false}]'
        )
        self.assertEqual([r.name for r in recipients], ["Fabien", "Alex"])
        self.assertTrue(recipients[0].enabled)
        self.assertFalse(recipients[1].enabled)

    def test_parse_recipients_rejects_malformed_input(self):
        for raw in ("not json", '[{"name": "Fabien", "vma": 17.5, "api_key": "k1"}]'):
            with self.assertRaises(ValueError):
                parse_recipients(raw)

    def test_push_uses_each_vma_skips_disabled_and_isolates_failures(self):
        recipients = parse_recipients(
            '[{"name": "Works", "vma": 17.5, "api_key": "k1", "athlete_id": "a1"},'
            ' {"name": "Broken", "vma": 15.0, "api_key": "k2", "athlete_id": "a2"},'
            ' {"name": "Paused", "vma": 16.0, "api_key": "k3", "athlete_id": "a3", "enabled": false}]'
        )
        pushed_vmas = []

        def fake_push_event(workout, *, vma, date, athlete_id, api_key, base_url=None):
            pushed_vmas.append(vma)
            if athlete_id == "a2":
                raise RuntimeError("401 Unauthorized")
            return {"id": 42}

        workout = parse_planiteam_pdf(HILL_SPRINTS_PDF)
        with patch("intervals_icu.push_event", side_effect=fake_push_event):
            results = push_to_recipients(workout, date="2026-09-10", recipients=recipients)

        self.assertEqual(pushed_vmas, [17.5, 15.0])
        self.assertEqual([(r.name, r.ok, r.event_id) for r in results], [("Works", True, 42), ("Broken", False, None)])
        self.assertIn("401", results[1].error)


if __name__ == "__main__":
    unittest.main()
