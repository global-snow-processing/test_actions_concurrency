"""Runoff onset detection, copied verbatim from the pipeline this probe measures.

Source:  https://github.com/egagli/global_snowmelt_runoff_onset
Path:    global_snowmelt_runoff_onset/processing.py
Commit:  4f2589e390cd788848b0228d4fdf76721370c8d1
License: MIT, (c) 2026 Eric Gagliano -- see that repository's LICENSE

Copied rather than imported. The real package installs into a pixi-solved
environment (icechunk, odc-stac, planetary-computer, geopandas, rasterio, dask,
easysnowdata); standing that up inside each of 256 concurrent probe jobs would
dominate the job and add a pile of network failure modes to a measurement that
is supposed to be about runner allocation. The functions below need only numpy,
pandas and xarray.

Only the returned_dates_format="doy" path is exercised here. The "dowy" branch
of calculate_runoff_onset needs rioxarray (.rio.estimate_utm_crs) and
easysnowdata, neither of which the probe installs, and xr_datetime_to_DOWY
below will raise NameError on easysnowdata if that branch is ever reached. Both
are kept verbatim anyway so this file diffs cleanly against upstream.

DO NOT EDIT. To refresh, re-copy the same functions from the commit named above
and update that line.
"""

from typing import Tuple

import numpy as np
import pandas as pd
import xarray as xr


def xr_datetime_to_DOWY(
    date_da: xr.DataArray, 
    hemisphere: str = "northern"
) -> xr.DataArray:
    """
    Convert xarray DataArray of datetime objects to Day of Water Year (DOWY).
    
    Converts datetime coordinates to day-of-water-year values, which provide
    a consistent temporal reference for comparing dates across different
    calendar years within the same water year.
    
    Args:
        date_da: DataArray containing datetime objects with any dimensions
        hemisphere: 'northern' or 'southern' for water year definition
        
    Returns:
        DataArray with DOWY values (1-366), with NaT values converted to 0
        
        **Input dimensions:** Same as date_da (typically includes 'latitude', 'longitude')
        **Output dimensions:** Same as input
        **Data type:** uint16 with 0 as nodata value
        **Values:** 1-366 representing day within water year
    """
    if date_da.attrs.get("any_valid_date") is not None:
        any_valid_date = pd.to_datetime(date_da.attrs["any_valid_date"])
    else:
        any_valid_date = pd.to_datetime(date_da.isel(latitude=0, longitude=0).values)

    start_of_water_year = easysnowdata.utils.get_water_year_start(
        any_valid_date, hemisphere=hemisphere
    )
    
    start_of_water_year_np = np.datetime64(start_of_water_year)
    
    def vectorized_dowy_calc(x):
        """Vectorized function that works efficiently with dask chunks and handles NaT"""
        nodata_int16 = -9999
        x_days = x.astype('datetime64[D]')
        
        days_diff = (x_days - start_of_water_year_np).astype('timedelta64[D]').astype('int64') + 1
        
        result = days_diff.astype('int16')
        result[pd.isna(x)] = nodata_int16

        return result
    
    return xr.apply_ufunc(
        vectorized_dowy_calc,
        date_da,
        vectorize=False,
        dask="parallelized",
        output_dtypes=[np.int16],
        dask_gufunc_kwargs={"output_sizes": {}},
    )


def calculate_backscatter_min_per_orbit(s1_rtc_masked_filtered_ds: xr.Dataset) -> xr.Dataset:
    """
    Calculate timing of minimum backscatter per orbit and polarization.
    
    Finds the date of minimum backscatter for each pixel, orbit, and polarization.
    
    Args:
        s1_rtc_masked_filtered_ds: Quality-filtered Sentinel-1 dataset with dimensions 
                                  ('time', 'latitude', 'longitude') and sat:relative_orbit coordinate
        
    Returns:
        Dataset with datetime of minimum backscatter for each orbit and polarization
        
        **Input dimensions:** ('time', 'latitude', 'longitude')
        **Output dimensions:** ('sat:relative_orbit', 'latitude', 'longitude')
        **Values:** datetime64[ns] of minimum backscatter occurrence
        **Data variables:** One per band (e.g., 'vv', 'vh')
    """
    backscatter_min_timing_per_orbit_and_polarization_ds = s1_rtc_masked_filtered_ds.groupby(
        "sat:relative_orbit"
    ).map(lambda c: c.idxmin(dim="time"))
    return backscatter_min_timing_per_orbit_and_polarization_ds


def calculate_runoff_onset_from_constituent_runoff_onsets(
    constituent_runoff_onsets_da: xr.DataArray
) -> xr.DataArray:
    """
    Calculate final runoff onset from multiple orbit/polarization estimates.
    
    Combines runoff onset estimates from different satellite orbits and
    polarizations by taking the median value. This reduces the impact of
    outliers and provides a more robust estimate.
    
    Args:
        constituent_runoff_onsets_da: DataArray with onset dates by orbit and polarization,
                                     dimensions ('sat:relative_orbit', 'polarization', 'latitude', 'longitude')
        
    Returns:
        DataArray with median runoff onset date for each pixel
        
        **Input dimensions:** ('sat:relative_orbit', 'polarization', 'latitude', 'longitude')
        **Output dimensions:** ('latitude', 'longitude')
        **Values:** datetime64[ns] - median across orbits and polarizations
        **Processing:** Excludes zero values, computes median, converts back to datetime
    """
    # Normalize to ns first: newer xarray/pandas return datetime64[us] from
    # idxmin, and int64-of-microseconds reinterpreted as datetime64[ns] would
    # silently produce 1970-era garbage dates.
    runoff_onset_da = (
        constituent_runoff_onsets_da.astype("datetime64[ns]").astype("int64")
        .where(lambda x: x > 0)
        .median(dim=["sat:relative_orbit", "polarization"], skipna=True)
        .astype("datetime64[ns]")
    )
    return runoff_onset_da


def calculate_runoff_onset(
    s1_rtc_masked_filtered_ds: xr.Dataset, 
    return_constituent_runoff_onsets: bool = False, 
    returned_dates_format: str = "dowy"
) -> xr.DataArray:
    """
    Calculate snowmelt runoff onset dates from filtered Sentinel-1 data.
    
    Main function for runoff onset detection. Identifies minimum backscatter
    timing per orbit/polarization, then optionally aggregates to a single
    estimate per pixel. Output format can be customized.
    
    Args:
        s1_rtc_masked_filtered_ds: Quality-filtered Sentinel-1 dataset with dimensions 
                                  ('time', 'latitude', 'longitude') and coordinates:
                                  water_year, sat:relative_orbit
        return_constituent_runoff_onsets: Whether to return individual orbit/polarization estimates
        returned_dates_format: Output format ('dowy', 'doy', or 'datetime64')
        
    Returns:
        DataArray with runoff onset dates in the specified format
        
        **If return_constituent_runoff_onsets=False:**
        - **Dimensions:** ('latitude', 'longitude')
        - **Values:** Aggregated onset estimate per pixel
        
        **If return_constituent_runoff_onsets=True:**
        - **Dimensions:** ('sat:relative_orbit', 'polarization', 'latitude', 'longitude')
        - **Values:** Individual onset estimates per orbit/polarization
        
        **Value types by format:**
        - 'dowy': uint16 (1-366, 0=nodata)
        - 'doy': int (1-366)
        - 'datetime64': datetime64[ns]
        
    Raises:
        ValueError: If returned_dates_format is not recognized
    """
    backscatter_min_timing_per_orbit_and_polarization_ds = calculate_backscatter_min_per_orbit(s1_rtc_masked_filtered_ds)
    constituent_runoff_onsets_da = backscatter_min_timing_per_orbit_and_polarization_ds.to_dataarray(dim="polarization")

    if return_constituent_runoff_onsets == False:
        runoff_onset_da = calculate_runoff_onset_from_constituent_runoff_onsets(constituent_runoff_onsets_da)
    else:
        runoff_onset_da = constituent_runoff_onsets_da

    if returned_dates_format == "dowy":
        hemisphere = (
            "northern"
            if s1_rtc_masked_filtered_ds.rio.estimate_utm_crs().to_epsg() < 32700
            else "southern"
        )
        month_start = 10 if hemisphere == "northern" else 4
        print(f"Area is in the {hemisphere} hemisphere. Water year starts in month {month_start}.")
        runoff_onset_da.attrs["any_valid_date"] = s1_rtc_masked_filtered_ds.time[0].values
        runoff_onset_da = xr_datetime_to_DOWY(runoff_onset_da, hemisphere=hemisphere)

    elif returned_dates_format == "doy":
        runoff_onset_da = runoff_onset_da.dt.dayofyear
    elif returned_dates_format == "datetime64":
        runoff_onset_da = runoff_onset_da
    else:
        raise ValueError('returned_dates_format must be either "doy", "dowy", or "datetime64".')

    return runoff_onset_da

def median_and_mad_with_min_obs(
    da: xr.DataArray, 
    dim: str, 
    min_count: int
) -> Tuple[xr.DataArray, xr.DataArray]:
    """
    Calculate median and median absolute deviation with minimum observation requirement.
    
    Computes robust statistics for runoff onset timing across multiple years.
    Only calculates statistics for pixels with sufficient observations to
    ensure statistical reliability.
    
    Args:
        da: DataArray with runoff onset dates, dimensions typically include the aggregation 
            dimension (e.g., 'water_year') plus spatial dimensions ('latitude', 'longitude')
            Zero values are excluded from calculations
        dim: Dimension along which to calculate statistics (typically 'water_year')
        min_count: Minimum number of valid observations required
        
    Returns:
        Tuple of (median, mad) DataArrays with statistics where sufficient data exists
        
        **Input dimensions:** (dim, 'latitude', 'longitude') - e.g., ('water_year', 'latitude', 'longitude')
        **Output dimensions:** ('latitude', 'longitude') - aggregation dimension removed
        **Data type:** Same as input for median, float for MAD
        **Values:** NaN where min_count not met, valid statistics elsewhere
    """
    da = da.where(lambda x: x > 0)  # Exclude zero values
    count_mask = da.notnull().sum(dim=dim) >= min_count
    median = da.where(count_mask).median(dim=dim)
    abs_dev = np.abs(da - median)
    mad = abs_dev.where(count_mask).median(dim=dim)
    return median, mad

