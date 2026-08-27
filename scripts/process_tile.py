#!/usr/bin/env python3
"""Detect snowmelt runoff onset for one geographic tile.

This is the per-tile unit of work the concurrency probe fans out. The detection
itself is not reimplemented here: scripts/upstream_processing.py holds the real
pipeline's functions, copied verbatim from
egagli/global_snowmelt_runoff_onset at the commit its header names, and this
module's job is to build a Sentinel-1 RTC-shaped dataset, hand it to
`calculate_runoff_onset`, and aggregate across water years with
`median_and_mad_with_min_obs` -- the same two calls the real pipeline makes.

Runoff onset detection (what the upstream code does)
----------------------------------------------------
Dry winter snow is nearly transparent at C band, so sigma0 over a snow-covered
slope sits close to the bare-ground value. As meltwater appears in the pack,
absorption rises and sigma0 falls, reaching a minimum around the point the pack
saturates and water begins to leave it. Once the pack drains and thins, sigma0
climbs back toward bare ground. Upstream takes the date of that seasonal
minimum per relative orbit and polarization, then the median across them.

On the input data
-----------------
The backscatter is SYNTHETIC, generated deterministically from the tile id
rather than pulled from the Planetary Computer. The probe needs hundreds of
self-contained, network-free, credential-free jobs, and the real STAC search
plus RTC load would make every job depend on an external service -- which would
also make it useless as a measurement of runner allocation. What is real here is
the detection code and the shape of the data it is given: three relative orbits
interleaved in time at the 12-day repeat, dual polarization, three water years.

Because the generator knows the onset it injected, each tile reports the error
of upstream's estimate against it. That is what scripts/test_process_tile.py
asserts on, and it is a live check that the vendored copy still works.

On pacing
---------
The probe measures how many runners the account grants at once, which needs
each job to hold its runner for a known interval. The tile's twelve
(water year x latitude band) units are therefore processed on a schedule
spanning --hold-seconds rather than as fast as the CPU allows. The work is
small on purpose; the schedule, not the arithmetic, sets how long a job lasts.
"""

import argparse
import json
import math
import os
import statistics
import sys
import time

import numpy as np
import pandas as pd
import xarray as xr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from upstream_processing import calculate_runoff_onset, median_and_mad_with_min_obs

# Tile grid: 16x16 one-degree cells over 44-60N, 124-108W -- the western North
# American cordillera, which is seasonal-snow country and gives 256 tiles, the
# same number as GitHub's per-run matrix cap.
GRID = 16
TILE_DEG = 1.0
ORIGIN_LAT = 44.0
ORIGIN_LON = -124.0

PIXELS = 32                        # pixels per tile side
ORBITS = (13, 64, 115)             # sat:relative_orbit values
ORBIT_REPEAT_DAYS = 12             # Sentinel-1 repeat for a single orbit
ACQ_PER_ORBIT = 31                 # ~one year per orbit at that repeat
WATER_YEARS = (2020, 2021, 2022)
LAT_BANDS = 4                      # spatial chunks per water year
MIN_YEARS = 2                      # water years required for a median

DRY_SNOW_DB = -9.0                 # dry-snow / bare-ground plateau, VV
CROSS_POL_OFFSET_DB = -5.0         # VH sits below VV
SPECKLE_DB = 0.55                  # per-observation speckle, 1 sigma
MELT_WIDTH_DAYS = 18.0             # width of the wet-snow dip
TERRAIN_SEED = 1                   # terrain is fixed across water years


def tile_bounds(tile_id):
    """Geographic bounds of a tile, from its 1-based id."""
    if not 1 <= tile_id <= GRID * GRID:
        raise ValueError(f"tile id must be in 1..{GRID * GRID}, got {tile_id}")
    row, col = divmod(tile_id - 1, GRID)
    lat = ORIGIN_LAT + row * TILE_DEG
    lon = ORIGIN_LON + col * TILE_DEG
    return {"lat_min": lat, "lat_max": lat + TILE_DEG,
            "lon_min": lon, "lon_max": lon + TILE_DEG}


def tile_climatology(tile_id):
    """Onset baseline and within-tile spread for a whole tile.

    Runoff onset comes later at higher latitude, so the baseline tracks the
    tile's centre latitude. The spread is set by how much relief the tile
    contains: a flat tile melts out over days, a mountainous one over weeks.
    Both are drawn once per tile, so neighbouring tiles differ the way real ones
    do instead of returning the same answer 256 times.
    """
    b = tile_bounds(tile_id)
    lat = (b["lat_min"] + b["lat_max"]) / 2
    rng = np.random.default_rng(tile_id)
    base = 92.0 + 2.2 * (lat - ORIGIN_LAT) + rng.normal(0.0, 4.0)
    return float(base), float(rng.uniform(15.0, 55.0))


def band_bounds():
    """Latitude-index ranges for the spatial chunks, split as evenly as possible."""
    edges = [round(PIXELS * b / LAT_BANDS) for b in range(LAT_BANDS + 1)]
    return list(zip(edges, edges[1:]))


def tile_coords(tile_id):
    b = tile_bounds(tile_id)
    lat = np.linspace(b["lat_min"], b["lat_max"], PIXELS, endpoint=False)
    lon = np.linspace(b["lon_min"], b["lon_max"], PIXELS, endpoint=False)
    return lat, lon


def year_anomaly(tile_id, water_year):
    """How early or late this water year's spring ran, in days.

    A whole tile shifts together: an early spring is an early spring everywhere
    in the cell. This is what gives the cross-year median something to average
    over and the MAD something to measure -- with an identical onset every year
    the MAD is zero by construction and upstream's aggregation is untested.
    """
    return float(np.random.default_rng([tile_id, water_year]).normal(0.0, 9.0))


def injected_onset(tile_id, water_year, row_lo=0, row_hi=PIXELS):
    """Onset day-of-year injected into each pixel, as a (lat, lon) array.

    Onset climbs with elevation, and a smooth ramp across the tile stands in for
    hypsometry with local relief on top, shifted by that water year's anomaly.

    The whole tile is always generated and then sliced, rather than generating
    the requested rows directly. Drawing only the slice would seed the terrain
    off the band boundaries, so the same pixel would get different terrain
    depending on how the tile happened to be chunked -- and the probe chunks it
    twelve ways purely to pace itself, which must not change the answer.
    """
    base, relief = tile_climatology(tile_id)
    rows = np.arange(PIXELS)[:, None]
    cols = np.arange(PIXELS)[None, :]
    rng = np.random.default_rng([tile_id, TERRAIN_SEED])
    ramp = (rows + cols) / (2 * (PIXELS - 1))
    z = np.clip(ramp + rng.normal(0.0, 0.04, size=(PIXELS, PIXELS)), 0.0, 1.0)
    onset = base + relief * z + year_anomaly(tile_id, water_year)
    return onset[row_lo:row_hi]


def acquisitions(water_year):
    """Interleaved acquisition times and their relative orbits, sorted in time.

    Each orbit revisits on the 12-day repeat, and the orbits are offset from one
    another, so the archive for a cell is several sparse series interleaved --
    which is why upstream reduces per orbit before combining.
    """
    times, orbits = [], []
    start = np.datetime64(f"{water_year - 1}-10-01")
    for k, orbit in enumerate(ORBITS):
        first = start + np.timedelta64(4 * k, "D")
        for i in range(ACQ_PER_ORBIT):
            times.append(first + np.timedelta64(ORBIT_REPEAT_DAYS * i, "D"))
            orbits.append(orbit)
    order = np.argsort(np.array(times))
    return np.array(times)[order], np.array(orbits)[order]


def synthesize_rtc(tile_id, water_year, row_lo, row_hi):
    """A Sentinel-1 RTC-shaped Dataset for part of a tile, for one water year.

    Matches what upstream's detection expects: data variables per polarization
    over ('time', 'latitude', 'longitude'), with sat:relative_orbit and
    water_year as coordinates along time.
    """
    onset_doy = injected_onset(tile_id, water_year, row_lo, row_hi)
    lat, lon = tile_coords(tile_id)
    lat = lat[row_lo:row_hi]
    times, orbits = acquisitions(water_year)

    # Depth of the dip grows with the injected onset day: a pack that melts out
    # later held more water to begin with.
    base, relief = tile_climatology(tile_id)
    elevation = (onset_doy - base - year_anomaly(tile_id, water_year)) / max(relief, 1e-6)
    dip_db = 4.0 + 3.0 * np.clip(elevation, 0.0, 1.0)

    onset_dt = (np.datetime64(f"{water_year}-01-01")
                + (onset_doy - 1).astype("timedelta64[D]").astype("timedelta64[ns]"))
    offset_days = ((times[:, None, None].astype("datetime64[ns]") - onset_dt[None, :, :])
                   / np.timedelta64(1, "D"))
    wet = dip_db[None, :, :] * np.exp(-((offset_days / MELT_WIDTH_DAYS) ** 2))

    # Speckle is drawn for the whole tile and then sliced, for the same reason
    # the terrain is: the band split is a pacing detail, not part of the science.
    rng = np.random.default_rng([tile_id, water_year])
    full = (len(times), PIXELS, PIXELS)
    band = slice(row_lo, row_hi)
    vv = DRY_SNOW_DB - wet + rng.normal(0.0, SPECKLE_DB, size=full)[:, band, :]
    vh = (DRY_SNOW_DB + CROSS_POL_OFFSET_DB - wet
          + rng.normal(0.0, SPECKLE_DB, size=full)[:, band, :])

    ds = xr.Dataset(
        {"vv": (("time", "latitude", "longitude"), vv.astype("float32")),
         "vh": (("time", "latitude", "longitude"), vh.astype("float32"))},
        coords={"time": times.astype("datetime64[ns]"), "latitude": lat, "longitude": lon},
    )
    ds = ds.assign_coords({
        "sat:relative_orbit": ("time", orbits),
        "water_year": ("time", np.full(len(times), water_year)),
    })
    return ds, onset_doy


def process_unit(tile_id, water_year, row_lo, row_hi):
    """Run the real detection over one (water year x latitude band) unit."""
    ds, truth = synthesize_rtc(tile_id, water_year, row_lo, row_hi)
    estimate = calculate_runoff_onset(ds, returned_dates_format="doy")
    return estimate, truth


def summarize(tile_id, per_year_doy, truth_by_year):
    """Aggregate across water years with upstream's median/MAD, then report."""
    stacked = xr.concat(
        [per_year_doy[y] for y in WATER_YEARS],
        dim=pd.Index(WATER_YEARS, name="water_year"),
    )
    median, mad = median_and_mad_with_min_obs(stacked, dim="water_year", min_count=MIN_YEARS)

    # Upstream's median is across water years, so compare it against the median
    # of what was injected across those same years, not against any one year.
    truth = np.median(np.stack([truth_by_year[y] for y in WATER_YEARS]), axis=0)
    valid = np.isfinite(median.values)
    summary = {
        "tile_id": tile_id,
        "bounds": tile_bounds(tile_id),
        "pixels": PIXELS * PIXELS,
        "pixels_with_onset": int(valid.sum()),
        "water_years": list(WATER_YEARS),
        "relative_orbits": list(ORBITS),
        "acquisitions_per_water_year": len(ORBITS) * ACQ_PER_ORBIT,
        "min_water_years_for_median": MIN_YEARS,
    }
    if valid.any():
        vals = median.values[valid]
        errors = vals - truth[valid]
        summary.update({
            "onset_doy_median": round(float(np.median(vals)), 1),
            "onset_doy_p10": round(float(np.percentile(vals, 10)), 1),
            "onset_doy_p90": round(float(np.percentile(vals, 90)), 1),
            "onset_doy_mad": round(float(np.nanmedian(mad.values[valid])), 1),
            "rmse_days": round(float(np.sqrt(np.mean(errors ** 2))), 2),
            "bias_days": round(float(np.mean(errors)), 2),
        })
    return summary


def write_window(out_dir, tile_id, start, end, held):
    """Timing record the report job sweeps to compute in-job concurrency."""
    path = os.path.join(out_dir, "windows")
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, f"job-{tile_id}.txt"), "w") as fh:
        fh.write(f"{tile_id} {int(start)} {int(end)} {held}\n")


def write_tile(out_dir, summary):
    path = os.path.join(out_dir, "tiles")
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, f"tile-{summary['tile_id']}.json"), "w") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
        fh.write("\n")


def clock(ts):
    return time.strftime("%H:%M:%SZ", time.gmtime(ts))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tile-id", type=int, required=True)
    ap.add_argument("--hold-seconds", type=float, required=True)
    ap.add_argument("--deadline", type=float, default=0.0,
                    help="Epoch seconds after which a job drains instead of holding a runner.")
    ap.add_argument("--out", default="results")
    args = ap.parse_args(argv)

    bounds = tile_bounds(args.tile_id)
    now = time.time()

    # Past the census window the fan-out is only draining its queue, so exit
    # without holding a runner rather than adding hold_seconds to the tail.
    if args.deadline and now >= args.deadline:
        print(f"tile {args.tile_id} got a runner at {clock(now)}, after the census "
              f"window closed -- draining without processing.")
        write_window(args.out, args.tile_id, now, now, 0)
        return 0

    hold = args.hold_seconds
    if args.deadline:
        hold = min(hold, args.deadline - now)

    units = [(y, lo, hi) for y in WATER_YEARS for lo, hi in band_bounds()]
    print(f"tile {args.tile_id}: {bounds['lat_min']:.0f}-{bounds['lat_max']:.0f}N "
          f"{abs(bounds['lon_max']):.0f}-{abs(bounds['lon_min']):.0f}W, "
          f"{PIXELS}x{PIXELS} px, {len(ORBITS)} orbits x {ACQ_PER_ORBIT} acquisitions, "
          f"water years {WATER_YEARS[0]}-{WATER_YEARS[-1]}")
    print(f"started {clock(now)}, {len(units)} units over {hold:.0f}s")

    start = time.time()
    bands = {y: {} for y in WATER_YEARS}
    truth_bands = {y: {} for y in WATER_YEARS}
    for n, (year, lo, hi) in enumerate(units, start=1):
        estimate, truth = process_unit(args.tile_id, year, lo, hi)
        bands[year][lo] = estimate
        truth_bands[year][lo] = truth
        print(f"  unit {n:2d}/{len(units)}  WY{year} rows {lo:2d}-{hi:2d}  "
              f"t+{time.time() - start:5.0f}s", flush=True)
        # Hold the runner to this unit's slot in the schedule.
        remaining = start + hold * n / len(units) - time.time()
        if remaining > 0:
            time.sleep(remaining)

    per_year = {y: xr.concat([bands[y][lo] for lo, _ in band_bounds()], dim="latitude")
                for y in WATER_YEARS}
    truth = {y: np.concatenate([truth_bands[y][lo] for lo, _ in band_bounds()], axis=0)
             for y in WATER_YEARS}

    end = time.time()
    summary = summarize(args.tile_id, per_year, truth)
    write_window(args.out, args.tile_id, start, end, 1)
    write_tile(args.out, summary)

    if "onset_doy_median" in summary:
        print(f"tile {args.tile_id} finished {clock(end)}: median onset DOY "
              f"{summary['onset_doy_median']} (p10-p90 {summary['onset_doy_p10']}-"
              f"{summary['onset_doy_p90']}), RMSE {summary['rmse_days']} days over "
              f"{summary['pixels_with_onset']} px")
    else:
        print(f"tile {args.tile_id} finished {clock(end)}: no pixel met the "
              f"{MIN_YEARS}-water-year minimum")
    return 0


if __name__ == "__main__":
    sys.exit(main())
