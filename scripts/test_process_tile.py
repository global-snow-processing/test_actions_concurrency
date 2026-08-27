#!/usr/bin/env python3
"""Tests for the probe's use of the real pipeline's runoff onset detection.

These do not test the detection itself -- that code is upstream's, copied
verbatim into upstream_processing.py. What they test is that the dataset this
repository builds is shaped the way upstream expects, that upstream's functions
recover an onset that was deliberately injected into it, and that the harness
around them (tile geometry, band splitting, deadline draining) behaves.

Run with: python3 -m unittest discover -s scripts -p 'test_*.py' -v
"""

import os
import re
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd
import xarray as xr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import process_tile as pt
import upstream_processing as up


class VendoredProvenance(unittest.TestCase):
    def test_header_pins_an_upstream_commit(self):
        path = os.path.join(os.path.dirname(pt.__file__), "upstream_processing.py")
        with open(path) as fh:
            header = fh.read(2000)
        self.assertIn("egagli/global_snowmelt_runoff_onset", header)
        self.assertRegex(header, r"Commit:\s+[0-9a-f]{40}")

    def test_the_functions_the_probe_calls_are_the_upstream_ones(self):
        self.assertIs(pt.calculate_runoff_onset, up.calculate_runoff_onset)
        self.assertIs(pt.median_and_mad_with_min_obs, up.median_and_mad_with_min_obs)


class DatasetShape(unittest.TestCase):
    """What upstream's detection requires of its input."""

    def setUp(self):
        self.ds, self.truth = pt.synthesize_rtc(7, pt.WATER_YEARS[0], 0, 8)

    def test_dims_and_variables_match_upstream_expectations(self):
        for var in ("vv", "vh"):
            self.assertEqual(self.ds[var].dims, ("time", "latitude", "longitude"))
        self.assertIn("sat:relative_orbit", self.ds.coords)
        self.assertEqual(self.ds["sat:relative_orbit"].dims, ("time",))
        self.assertIn("water_year", self.ds.coords)

    def test_every_orbit_is_present_and_interleaved_in_time(self):
        orbits = self.ds["sat:relative_orbit"].values
        self.assertEqual(set(orbits), set(pt.ORBITS))
        # Interleaved, not one orbit's series after another.
        self.assertGreater(len(set(orbits[:len(pt.ORBITS) * 2])), 1)

    def test_time_is_sorted(self):
        t = self.ds.time.values
        self.assertTrue((np.diff(t) >= np.timedelta64(0)).all())

    def test_cross_pol_sits_below_co_pol(self):
        self.assertLess(float(self.ds.vh.mean()), float(self.ds.vv.mean()))


class UpstreamDetection(unittest.TestCase):
    def test_recovers_the_injected_onset(self):
        ds, truth = pt.synthesize_rtc(128, 2021, 0, 8)
        estimate = up.calculate_runoff_onset(ds, returned_dates_format="doy")
        self.assertEqual(estimate.dims, ("latitude", "longitude"))
        errors = estimate.values - truth
        # Bounded by the 12-day per-orbit repeat, softened by the median over
        # three orbits and two polarizations.
        self.assertLess(np.sqrt(np.mean(errors ** 2)), pt.ORBIT_REPEAT_DAYS / 2)

    def test_constituent_estimates_cover_every_orbit_and_polarization(self):
        ds, _ = pt.synthesize_rtc(3, 2020, 0, 4)
        da = up.calculate_runoff_onset(
            ds, return_constituent_runoff_onsets=True, returned_dates_format="doy"
        )
        self.assertEqual(set(da["sat:relative_orbit"].values), set(pt.ORBITS))
        self.assertEqual(set(da["polarization"].values), {"vv", "vh"})

    def test_band_split_does_not_change_the_result(self):
        whole, _ = pt.process_unit(11, 2022, 0, 8)
        top, _ = pt.process_unit(11, 2022, 0, 3)
        bottom, _ = pt.process_unit(11, 2022, 3, 8)
        joined = xr.concat([top, bottom], dim="latitude")
        np.testing.assert_array_equal(whole.values, joined.values)


class CrossYearAggregation(unittest.TestCase):
    def _stack(self, values):
        return xr.concat(
            [xr.DataArray(float(v)) for v in values],
            dim=pd.Index(pt.WATER_YEARS, name="water_year"),
        )

    def test_median_across_water_years(self):
        median, mad = up.median_and_mad_with_min_obs(
            self._stack([100, 110, 120]), dim="water_year", min_count=2
        )
        self.assertAlmostEqual(float(median), 110.0)
        self.assertAlmostEqual(float(mad), 10.0)

    def test_min_count_is_enforced(self):
        # Two of the three years excluded as zero -> below min_count -> NaN.
        median, _ = up.median_and_mad_with_min_obs(
            self._stack([100, 0, 0]), dim="water_year", min_count=2
        )
        self.assertTrue(np.isnan(float(median)))

    def test_interannual_spread_is_not_degenerate(self):
        # A zero MAD everywhere would mean the years are identical and
        # upstream's aggregation is doing nothing.
        anomalies = {pt.year_anomaly(60, y) for y in pt.WATER_YEARS}
        self.assertGreater(len(anomalies), 1)


class TileGeometry(unittest.TestCase):
    def test_ids_cover_the_grid_exactly_once(self):
        corners = {
            (pt.tile_bounds(i)["lat_min"], pt.tile_bounds(i)["lon_min"])
            for i in range(1, pt.GRID * pt.GRID + 1)
        }
        self.assertEqual(len(corners), pt.GRID * pt.GRID)

    def test_ids_outside_the_grid_are_rejected(self):
        for bad in (0, -1, pt.GRID * pt.GRID + 1):
            with self.assertRaises(ValueError):
                pt.tile_bounds(bad)

    def test_onset_comes_later_at_higher_latitude(self):
        def row_mean(row):
            ids = range(row * pt.GRID + 1, (row + 1) * pt.GRID + 1)
            return float(np.mean([pt.tile_climatology(i)[0] for i in ids]))

        self.assertGreater(row_mean(pt.GRID - 1) - row_mean(0), 20.0)

    def test_neighbouring_tiles_differ(self):
        bases = [pt.tile_climatology(i)[0] for i in range(1, pt.GRID + 1)]
        self.assertGreater(len(set(bases)), pt.GRID // 2)

    def test_bands_partition_the_tile(self):
        bounds = pt.band_bounds()
        self.assertEqual(len(bounds), pt.LAT_BANDS)
        self.assertEqual((bounds[0][0], bounds[-1][1]), (0, pt.PIXELS))
        for (_, prev_hi), (lo, _) in zip(bounds, bounds[1:]):
            self.assertEqual(prev_hi, lo)


class TileRun(unittest.TestCase):
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

            import json
            with open(os.path.join(out, "tiles", "tile-9.json")) as fh:
                summary = json.load(fh)
            self.assertEqual(summary["pixels_with_onset"], pt.PIXELS * pt.PIXELS)
            self.assertLess(summary["rmse_days"], pt.ORBIT_REPEAT_DAYS / 2)
            self.assertGreater(summary["onset_doy_mad"], 0.0)
            self.assertEqual(summary["relative_orbits"], list(pt.ORBITS))


if __name__ == "__main__":
    unittest.main()
