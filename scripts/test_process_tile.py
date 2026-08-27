#!/usr/bin/env python3
"""Tests for the per-tile runoff onset detection.

Run with: python3 -m unittest discover -s scripts -p 'test_*.py' -v
"""

import os
import random
import statistics
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import process_tile as pt


class TileGeometry(unittest.TestCase):
    def test_ids_cover_the_grid_exactly_once(self):
        corners = {
            (pt.tile_bounds(i)["lat_min"], pt.tile_bounds(i)["lon_min"])
            for i in range(1, pt.GRID * pt.GRID + 1)
        }
        self.assertEqual(len(corners), pt.GRID * pt.GRID)

    def test_first_tile_sits_at_the_origin(self):
        b = pt.tile_bounds(1)
        self.assertEqual((b["lat_min"], b["lon_min"]), (pt.ORIGIN_LAT, pt.ORIGIN_LON))
        self.assertEqual(b["lat_max"] - b["lat_min"], pt.TILE_DEG)

    def test_ids_outside_the_grid_are_rejected(self):
        for bad in (0, -1, pt.GRID * pt.GRID + 1):
            with self.assertRaises(ValueError):
                pt.tile_bounds(bad)


class TileClimatology(unittest.TestCase):
    def test_onset_comes_later_at_higher_latitude(self):
        # Averaged over the longitude row, since each tile also carries its own
        # draw on top of the latitude trend.
        def row_mean(row):
            ids = range(row * pt.GRID + 1, (row + 1) * pt.GRID + 1)
            return statistics.mean(pt.tile_climatology(i)[0] for i in ids)

        self.assertLess(row_mean(0), row_mean(pt.GRID - 1))
        self.assertGreater(row_mean(pt.GRID - 1) - row_mean(0), 20.0)

    def test_neighbouring_tiles_differ(self):
        bases = [pt.tile_climatology(i)[0] for i in range(1, pt.GRID + 1)]
        self.assertGreater(len(set(bases)), pt.GRID // 2)

    def test_climatology_is_stable_across_calls(self):
        self.assertEqual(pt.tile_climatology(77), pt.tile_climatology(77))


class OnsetDetection(unittest.TestCase):
    def test_recovers_an_injected_onset(self):
        doys = pt.acquisition_doys()
        for injected in (100.0, 130.0, 160.0):
            rng = random.Random(injected)
            series = pt.synthesize_series(rng, injected, dip_db=6.0)
            estimate, depth = pt.detect_onset(doys, series)
            self.assertIsNotNone(estimate)
            # One revisit interval either side is the best a 6-day series can do.
            self.assertLessEqual(abs(estimate - injected), pt.REVISIT_DAYS)
            self.assertGreater(depth, pt.MIN_DIP_DB)

    def test_a_flat_series_reports_no_onset(self):
        doys = pt.acquisition_doys()
        rng = random.Random(0)
        flat = pt.synthesize_series(rng, onset_doy=150.0, dip_db=0.0)
        estimate, depth = pt.detect_onset(doys, flat)
        self.assertIsNone(estimate)
        self.assertLess(depth, pt.MIN_DIP_DB)

    def test_smoothing_does_not_move_the_minimum(self):
        # A single speckle spike must not be mistaken for the seasonal minimum.
        doys = pt.acquisition_doys()
        rng = random.Random(7)
        series = pt.synthesize_series(rng, onset_doy=140.0, dip_db=6.0)
        spike_at = 3
        series[spike_at] -= 12.0
        estimate, _ = pt.detect_onset(doys, series)
        self.assertNotEqual(estimate, doys[spike_at])
        self.assertLessEqual(abs(estimate - 140.0), pt.REVISIT_DAYS)


class Blocking(unittest.TestCase):
    def test_blocks_partition_the_tile(self):
        bounds = pt.block_bounds()
        self.assertEqual(len(bounds), pt.BLOCKS)
        self.assertEqual(bounds[0][0], 0)
        self.assertEqual(bounds[-1][1], pt.PIXELS)
        for (_, prev_hi), (lo, _) in zip(bounds, bounds[1:]):
            self.assertEqual(prev_hi, lo)

    def test_results_do_not_depend_on_the_block_split(self):
        whole = pt.process_rows(3, 0, 8)
        split = pt.process_rows(3, 0, 5) + pt.process_rows(3, 5, 8)
        self.assertEqual(whole, split)


class TileRun(unittest.TestCase):
    def test_a_tile_detects_onset_and_reports_its_own_error(self):
        found = pt.process_rows(42, 0, 4)
        self.assertGreater(len(found), 0)
        summary = pt.summarize(42, found)
        self.assertIn("onset_doy_median", summary)
        # The detector is bounded by the 6-day revisit, so a few days of RMSE is
        # expected; anything much larger means it is not tracking the dip.
        self.assertLess(summary["rmse_days"], pt.REVISIT_DAYS)
        # Onset must land inside the acquisition year, well clear of its edges,
        # or the dip is being clipped rather than detected.
        doys = pt.acquisition_doys()
        self.assertTrue(doys[0] + 30 < summary["onset_doy_median"] < doys[-1] - 30)

    def test_tiles_do_not_all_return_the_same_answer(self):
        medians = {
            pt.summarize(i, pt.process_rows(i, 0, 2))["onset_doy_median"]
            for i in (1, 40, 90, 150, 210, 256)
        }
        self.assertGreater(len(medians), 3)

    def test_draining_past_the_deadline_holds_no_runner(self):
        with tempfile.TemporaryDirectory() as out:
            rc = pt.main(["--tile-id", "5", "--hold-seconds", "600",
                          "--deadline", "1", "--out", out])
            self.assertEqual(rc, 0)
            with open(os.path.join(out, "windows", "job-5.txt")) as fh:
                jid, start, end, held = fh.read().split()
            self.assertEqual((jid, held), ("5", "0"))
            self.assertEqual(start, end)
            self.assertFalse(os.path.exists(os.path.join(out, "tiles")))

    def test_a_short_hold_writes_both_outputs(self):
        with tempfile.TemporaryDirectory() as out:
            rc = pt.main(["--tile-id", "9", "--hold-seconds", "1", "--out", out])
            self.assertEqual(rc, 0)
            with open(os.path.join(out, "windows", "job-9.txt")) as fh:
                jid, start, end, held = fh.read().split()
            self.assertEqual((jid, held), ("9", "1"))
            self.assertGreaterEqual(int(end), int(start))
            self.assertTrue(os.path.exists(os.path.join(out, "tiles", "tile-9.json")))


if __name__ == "__main__":
    unittest.main()
