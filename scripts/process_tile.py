#!/usr/bin/env python3
"""Detect snowmelt runoff onset for one geographic tile.

This is the per-tile unit of work the concurrency probe fans out, and it is the
same detection the wider project runs: the seasonal minimum of Sentinel-1 C-band
VV backscatter, evaluated per pixel over one 1-degree cell.

Runoff onset detection
----------------------
Dry winter snow is nearly transparent at C band, so sigma0 over a snow-covered
slope sits close to the bare-ground value. As meltwater appears in the pack,
absorption rises and sigma0 falls, reaching a minimum around the point the pack
saturates and water begins to leave it. Once the pack drains and thins, sigma0
climbs back toward bare ground. The date of that seasonal minimum is taken as
runoff onset.

On the input data
-----------------
The backscatter series processed here is SYNTHETIC, generated deterministically
from the tile id rather than pulled from an archive. The probe needs hundreds of
identical, self-contained, network-free jobs, and fetching real granules would
make every job depend on an external service and its credentials. The detection
below is the real algorithm; only its input is stand-in data. Because the
generator knows the onset it injected, each tile also reports the error of its
own estimate, which is what the unit tests assert against.

On pacing
---------
The probe measures how many runners the account grants at once, which needs each
job to hold its runner for a known, controlled interval. The tile is therefore
processed in blocks on a fixed schedule spanning --hold-seconds rather than as
fast as the CPU allows. The work is deliberately small; the schedule, not the
arithmetic, is what sets how long a job lasts.
"""

import argparse
import json
import math
import os
import random
import statistics
import sys
import time

# Tile grid: 16x16 one-degree cells over 44-60N, 124-108W -- the western North
# American cordillera, which is seasonal-snow country and gives 256 tiles, the
# same number as GitHub's per-run matrix cap.
GRID = 16
TILE_DEG = 1.0
ORIGIN_LAT = 44.0
ORIGIN_LON = -124.0

PIXELS = 64          # pixels per tile side
REVISIT_DAYS = 6     # Sentinel-1 repeat interval
FIRST_DOY = 1
N_OBS = 61           # ~one year of acquisitions at a 6-day revisit
BLOCKS = 12          # pacing blocks the tile is processed in

DRY_SNOW_DB = -9.0   # dry-snow / bare-ground plateau
SPECKLE_DB = 0.55    # per-observation speckle, 1 sigma
MIN_DIP_DB = 2.0     # shallowest dip still read as a melt signal


def tile_bounds(tile_id):
    """Geographic bounds of a tile, from its 1-based id."""
    if not 1 <= tile_id <= GRID * GRID:
        raise ValueError(f"tile id must be in 1..{GRID * GRID}, got {tile_id}")
    row, col = divmod(tile_id - 1, GRID)
    lat = ORIGIN_LAT + row * TILE_DEG
    lon = ORIGIN_LON + col * TILE_DEG
    return {
        "lat_min": lat,
        "lat_max": lat + TILE_DEG,
        "lon_min": lon,
        "lon_max": lon + TILE_DEG,
    }


def tile_climatology(tile_id):
    """Onset baseline and within-tile spread for a whole tile.

    Runoff onset comes later at higher latitude, so the baseline tracks the
    tile's centre latitude. The spread is set by how much relief the tile
    contains: a flat tile melts out over a few days, a mountainous one over
    weeks. Both are drawn once per tile from a tile-seeded generator, so
    neighbouring tiles differ the way real ones do instead of returning the
    same answer 256 times.
    """
    b = tile_bounds(tile_id)
    lat = (b["lat_min"] + b["lat_max"]) / 2
    rng = random.Random(tile_id)
    base = 92.0 + 2.2 * (lat - ORIGIN_LAT) + rng.gauss(0.0, 4.0)
    return base, rng.uniform(15.0, 55.0)


def pixel_profile(rng, row, col, base_onset, relief):
    """Injected onset day and melt dip depth for one pixel.

    Onset climbs with elevation and the dip deepens with it, since a deeper pack
    stays wet longer. A smooth ramp across the tile stands in for hypsometry,
    with local relief on top.
    """
    ramp = (row + col) / (2 * (PIXELS - 1))
    z = min(max(ramp + rng.gauss(0.0, 0.04), 0.0), 1.0)
    return base_onset + relief * z, 4.0 + 3.0 * z


def acquisition_doys():
    return [FIRST_DOY + i * REVISIT_DAYS for i in range(N_OBS)]


def synthesize_series(rng, onset_doy, dip_db):
    """A year of VV sigma0 (dB) for one pixel, dipping around onset_doy."""
    width = 18.0
    series = []
    for doy in acquisition_doys():
        d = (doy - onset_doy) / width
        series.append(DRY_SNOW_DB - dip_db * math.exp(-d * d) + rng.gauss(0.0, SPECKLE_DB))
    return series


def moving_median(values, window=3):
    """Despeckle the series without shifting the position of its minimum."""
    half = window // 2
    out = []
    for i in range(len(values)):
        lo = max(0, i - half)
        hi = min(len(values), i + half + 1)
        out.append(statistics.median(values[lo:hi]))
    return out


def detect_onset(doys, sigma0):
    """Runoff onset day of year, or None where no melt signal is present.

    Returns (onset_doy, dip_depth_db). A dip shallower than MIN_DIP_DB is a
    pixel with no resolvable seasonal snow -- open water, low elevation, or a
    series that is speckle all the way down -- and is reported as invalid rather
    than as an onset at whatever day the noise happened to bottom out.
    """
    smooth = moving_median(sigma0)
    floor = min(smooth)
    depth = statistics.median(smooth) - floor
    if depth < MIN_DIP_DB:
        return None, depth
    return doys[smooth.index(floor)], depth


def process_rows(tile_id, row_lo, row_hi):
    """Detect onset for every pixel in a band of rows."""
    doys = acquisition_doys()
    base, relief = tile_climatology(tile_id)
    found = []
    for row in range(row_lo, row_hi):
        for col in range(PIXELS):
            # Seeded per pixel, so a tile's result does not depend on how its
            # rows were split into blocks.
            rng = random.Random(tile_id * 1_000_000 + row * 1_000 + col)
            truth, dip = pixel_profile(rng, row, col, base, relief)
            estimate, _ = detect_onset(doys, synthesize_series(rng, truth, dip))
            if estimate is not None:
                found.append((estimate, truth))
    return found


def block_bounds():
    """Row ranges for the pacing blocks, split as evenly as PIXELS allows."""
    edges = [round(PIXELS * b / BLOCKS) for b in range(BLOCKS + 1)]
    return list(zip(edges, edges[1:]))


def summarize(tile_id, found):
    onsets = [e for e, _ in found]
    summary = {
        "tile_id": tile_id,
        "bounds": tile_bounds(tile_id),
        "pixels": PIXELS * PIXELS,
        "pixels_with_onset": len(found),
        "acquisitions": N_OBS,
        "revisit_days": REVISIT_DAYS,
    }
    if onsets:
        median = statistics.median(onsets)
        errors = [e - t for e, t in found]
        summary.update(
            {
                "onset_doy_median": round(median, 1),
                "onset_doy_p10": round(sorted(onsets)[len(onsets) // 10], 1),
                "onset_doy_p90": round(sorted(onsets)[min(len(onsets) * 9 // 10, len(onsets) - 1)], 1),
                "onset_doy_mad": round(statistics.median([abs(o - median) for o in onsets]), 1),
                "rmse_days": round(math.sqrt(sum(e * e for e in errors) / len(errors)), 2),
                "bias_days": round(sum(errors) / len(errors), 2),
            }
        )
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

    print(f"tile {args.tile_id}: {bounds['lat_min']:.0f}-{bounds['lat_max']:.0f}N "
          f"{abs(bounds['lon_max']):.0f}-{abs(bounds['lon_min']):.0f}W, "
          f"{PIXELS}x{PIXELS} px x {N_OBS} acquisitions")
    print(f"started {clock(now)}, processing in {BLOCKS} blocks over {hold:.0f}s")

    start = time.time()
    found = []
    for block, (lo, hi) in enumerate(block_bounds(), start=1):
        found += process_rows(args.tile_id, lo, hi)
        print(f"  block {block:2d}/{BLOCKS}  rows {lo:2d}-{hi:2d}  "
              f"{len(found):5d} px with onset  t+{time.time() - start:5.0f}s", flush=True)
        # Hold the runner to the block's slot in the schedule.
        remaining = start + hold * block / BLOCKS - time.time()
        if remaining > 0:
            time.sleep(remaining)

    end = time.time()
    summary = summarize(args.tile_id, found)
    write_window(args.out, args.tile_id, start, end, 1)
    write_tile(args.out, summary)

    if "onset_doy_median" in summary:
        print(f"tile {args.tile_id} finished {clock(end)}: median onset DOY "
              f"{summary['onset_doy_median']} (p10-p90 {summary['onset_doy_p10']}-"
              f"{summary['onset_doy_p90']}), RMSE {summary['rmse_days']} days over "
              f"{summary['pixels_with_onset']} px")
    else:
        print(f"tile {args.tile_id} finished {clock(end)}: no pixel showed a melt signal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
