"""Add potential global solar radiation (``f_rg_pot``) to a WISP forcing file.

``f_rg_pot`` is the SINDBAD counterpart of ONEFlux ``SW_IN_POT``: the incoming
short-wave radiation a horizontal surface would receive from solar geometry
alone, i.e. extraterrestrial radiation. It is a pure function of time,
latitude and longitude, so it can be reconstructed from the coordinates that
already exist in the WISP cube.

The default output matches the units of ``f_rg`` in the WISP physical entity
(MJ m-2 h-1) and uses the same accumulation convention as ERA5 ``ssrd``: the
value stamped at time ``t`` is the energy accumulated over ``(t - 1h, t]``.

Usage
-----
    python add_rg_pot.py INPUT.nc [-o OUTPUT.nc]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import xarray as xr

SOLAR_CONSTANT = 1361.0  # W m-2


def _solar_position(times_utc: np.ndarray, lat: np.ndarray, lon: np.ndarray):
    """Cosine of the solar zenith angle (NOAA algorithm), clipped at zero.

    Parameters
    ----------
    times_utc : datetime64[ns] array of shape (T,), assumed UTC.
    lat, lon : degrees, shapes (nlat,) and (nlon,).

    Returns
    -------
    array of shape (T, nlat, nlon) with cos(zenith), zero when the sun is
    below the horizon.
    """
    times = np.asarray(times_utc, dtype="datetime64[ns]")

    # Fractional day of year and UTC hour.
    year_start = times.astype("datetime64[Y]").astype("datetime64[ns]")
    day_start = times.astype("datetime64[D]").astype("datetime64[ns]")
    sec_per_day = 86400.0
    doy = (day_start - year_start) / np.timedelta64(1, "D") + 1.0
    utc_hour = (times - day_start) / np.timedelta64(1, "s") / 3600.0

    # Fractional year (radians), NOAA solar calculator.
    days_in_year = np.where(
        _is_leap(times.astype("datetime64[Y]").astype(int) + 1970), 366.0, 365.0
    )
    gamma = 2.0 * np.pi / days_in_year * (doy - 1.0 + (utc_hour - 12.0) / 24.0)

    # Equation of time (minutes) and solar declination (radians).
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * np.cos(gamma)
        - 0.032077 * np.sin(gamma)
        - 0.014615 * np.cos(2.0 * gamma)
        - 0.040849 * np.sin(2.0 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * np.cos(gamma)
        + 0.070257 * np.sin(gamma)
        - 0.006758 * np.cos(2.0 * gamma)
        + 0.000907 * np.sin(2.0 * gamma)
        - 0.002697 * np.cos(3.0 * gamma)
        + 0.001480 * np.sin(3.0 * gamma)
    )

    # Broadcast to (T, nlat, nlon).
    lat_r = np.deg2rad(np.asarray(lat, dtype=float))[None, :, None]
    lon_b = np.asarray(lon, dtype=float)[None, None, :]
    eqtime_b = eqtime[:, None, None]
    decl_b = decl[:, None, None]
    utc_min = (utc_hour * 60.0)[:, None, None]

    # True solar time (minutes) -> hour angle (radians).
    tst = np.mod(utc_min + eqtime_b + 4.0 * lon_b, 1440.0)
    hour_angle = np.deg2rad(tst / 4.0 - 180.0)

    cos_zenith = np.sin(lat_r) * np.sin(decl_b) + np.cos(lat_r) * np.cos(decl_b) * np.cos(
        hour_angle
    )
    return np.clip(cos_zenith, 0.0, None)


def _is_leap(year: np.ndarray) -> np.ndarray:
    return ((year % 4 == 0) & (year % 100 != 0)) | (year % 400 == 0)


def _earth_sun_factor(times_utc: np.ndarray) -> np.ndarray:
    """Inverse-square Earth-Sun distance correction (dimensionless, ~0.97-1.03)."""
    times = np.asarray(times_utc, dtype="datetime64[ns]")
    year_start = times.astype("datetime64[Y]").astype("datetime64[ns]")
    doy = (times - year_start) / np.timedelta64(1, "D") + 1.0
    return 1.0 + 0.033 * np.cos(2.0 * np.pi * doy / 365.25)


def compute_rg_pot(
    time: xr.DataArray,
    lat: xr.DataArray,
    lon: xr.DataArray,
    *,
    accumulation_hours: float = 1.0,
    label: str = "end",
    n_substeps: int = 60,
    transmissivity: float = 1.0,
) -> xr.DataArray:
    """Potential (extraterrestrial) global radiation in MJ m-2 per accumulation period.

    The solar zenith angle varies strongly within an hour near sunrise and
    sunset, so the period is integrated with ``n_substeps`` sub-samples rather
    than evaluated once at the timestamp.

    Parameters
    ----------
    accumulation_hours : length of the accumulation period, in hours.
    label : ``"end"`` if the timestamp marks the end of the period (ERA5
        ``ssrd`` convention), ``"begin"`` or ``"center"`` otherwise.
    transmissivity : optional bulk clear-sky transmissivity. Leave at 1.0 to
        reproduce ONEFlux ``SW_IN_POT``, which is purely geometric; ~0.75 gives
        a rough clear-sky surface irradiance instead.
    """
    if label == "end":
        offsets_h = -accumulation_hours, 0.0
    elif label == "begin":
        offsets_h = 0.0, accumulation_hours
    elif label == "center":
        offsets_h = -accumulation_hours / 2.0, accumulation_hours / 2.0
    else:
        raise ValueError(f"label must be 'end', 'begin' or 'center', got {label!r}")

    t_values = time.values.astype("datetime64[ns]")
    # Midpoints of n_substeps equal sub-intervals covering the period.
    edges = np.linspace(offsets_h[0], offsets_h[1], n_substeps + 1)
    midpoints_h = 0.5 * (edges[:-1] + edges[1:])

    mean_cos_zenith = np.zeros((t_values.size, lat.size, lon.size), dtype=np.float64)
    mean_dist = np.zeros(t_values.size, dtype=np.float64)
    for offset_h in midpoints_h:
        shifted = t_values + np.timedelta64(int(round(offset_h * 3.6e12)), "ns")
        mean_cos_zenith += _solar_position(shifted, lat.values, lon.values)
        mean_dist += _earth_sun_factor(shifted)
    mean_cos_zenith /= n_substeps
    mean_dist /= n_substeps

    # W m-2 averaged over the period -> MJ m-2 over the period.
    irradiance = SOLAR_CONSTANT * transmissivity * mean_dist[:, None, None] * mean_cos_zenith
    energy = irradiance * accumulation_hours * 3600.0 * 1e-6

    da = xr.DataArray(
        energy.astype(np.float32),
        coords={"time": time, "lat": lat, "lon": lon},
        dims=("time", "lat", "lon"),
        name="f_rg_pot",
    )
    da.attrs = {
        "long_name": "SINDBAD potential global-radiation energy per hour",
        "standard_name": "Potential Global Radiation",
        "units": f"MJ m-2 {'h' if accumulation_hours == 1.0 else f'{accumulation_hours}h'}-1",
        "source_product": "computed from solar geometry (time, lat, lon)",
        "source_variable": "time,lat,lon",
        "conversion_formula": (
            f"f_rg_pot = {SOLAR_CONSTANT} [W m-2] * {transmissivity} * E0(doy) * "
            "mean(cos(solar_zenith)) over the accumulation period * 3600 * 1e-6"
        ),
        "solar_position_algorithm": "NOAA solar calculator (equation of time + declination)",
        "solar_constant_W_m-2": SOLAR_CONSTANT,
        "clear_sky_transmissivity": transmissivity,
        "accumulation_hours": accumulation_hours,
        "timestamp_convention": f"value at t covers the period labelled '{label}'",
        "sub_sampling_steps_per_period": n_substeps,
        "time_zone_assumption": "input time coordinate is UTC",
        "space_time_type": "spatiotemporal",
        "temporal_support": "hourly" if accumulation_hours == 1.0 else "sub-daily",
        "normalization_applied": "none",
        "sindbad_partial_forcing": "true",
        "derivation_is_approximation": "true",
        "data_quality_note": (
            "Extraterrestrial (top-of-atmosphere) radiation on a horizontal surface, "
            "the ONEFlux SW_IN_POT convention. No atmospheric attenuation is applied "
            "unless clear_sky_transmissivity < 1."
        ),
    }
    return da


def add_rg_pot(
    input_path: str | Path,
    output_path: str | Path | None = None,
    **kwargs,
) -> Path:
    """Compute ``f_rg_pot`` and write a copy of the dataset that includes it."""
    input_path = Path(input_path)
    if output_path is None:
        output_path = input_path.with_name(f"{input_path.stem}_rgpot{input_path.suffix}")
    output_path = Path(output_path)

    with xr.open_dataset(input_path) as ds:
        ds = ds.load()

    ds["f_rg_pot"] = compute_rg_pot(ds["time"], ds["lat"], ds["lon"], **kwargs)

    available = ds.attrs.get("available_sindbad_forcing", "")
    names = [n for n in available.split(",") if n]
    if "f_rg_pot" not in names:
        names.append("f_rg_pot")
    ds.attrs["available_sindbad_forcing"] = ",".join(names)
    ds.attrs["history"] = (
        ds.attrs.get("history", "") + "; f_rg_pot added by add_rg_pot.py"
    ).lstrip("; ")

    encoding = {
        var: {"zlib": True, "complevel": 4}
        for var in ds.data_vars
        if ds[var].ndim >= 2
    }
    ds.to_netcdf(output_path, encoding=encoding)
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="input NetCDF forcing file")
    parser.add_argument("-o", "--output", default=None, help="output NetCDF file")
    parser.add_argument("--accumulation-hours", type=float, default=1.0)
    parser.add_argument("--label", default="end", choices=["end", "begin", "center"])
    parser.add_argument("--substeps", type=int, default=60)
    parser.add_argument(
        "--transmissivity",
        type=float,
        default=1.0,
        help="bulk clear-sky transmissivity; 1.0 reproduces ONEFlux SW_IN_POT",
    )
    args = parser.parse_args()

    out = add_rg_pot(
        args.input,
        args.output,
        accumulation_hours=args.accumulation_hours,
        label=args.label,
        n_substeps=args.substeps,
        transmissivity=args.transmissivity,
    )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
