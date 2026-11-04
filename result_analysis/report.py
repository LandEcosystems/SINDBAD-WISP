"""Offline English reports from compact, completed SINDBAD analyses.

This module never opens raw simulation files or modifies a source result.  Its
only inputs are the auditable compact tables produced by the analysis stages.
Matplotlib/pandas imports are lazy so inventory-only reporting and schema tests
work without a plotting environment.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import re
import textwrap
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCENARIOS = ("No_Fire", "VIIRS_Fire")
COLORS = {"No_Fire": "#24647E", "VIIRS_Fire": "#BD602B"}
GROUP_FAMILIES = ("overall", "pyrome", "vegetation", "igbp", "model_pft")
PYROME_ORDER = ("unknown", "FIL", "ICS", "RCS", "RIL", "FCS")
SEMANTIC_WARNINGS = (
    "This is an interim analysis of completed regions, not an unbiased total for the Mediterranean.",
    "Scenario differences compare two model experiments; they do not validate either experiment against observations.",
    "NEE excludes direct fire efflux. Mortality and internal transfer must not be added again to atmospheric emissions.",
    "The saved restart is from EtaScaleH before age-dependent regrowth, not the first forward carbon inventory.",
    "2014 is incomplete (12 January onward). Complete-year comparisons exclude that partial year.",
    "Water cells can contain finite zeros. Source run and ownership masks, not NaN presence alone, define support.",
    "LAI is unavailable and cannot be uniquely reconstructed from these outputs. No simulated LAI is fabricated.",
    "Model area conventions remain an explicit audit item; no blanket vegetation-fraction correction is applied.",
)


def slug(value: Any) -> str:
    """Filesystem-safe, stable display identity (never used as data grouping)."""
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
    return s or "unknown"


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def completed_regions(root: Path, snapshot: Any = None) -> list[Path]:
    """Discover only complete analyses; optional immutable snapshot limits IDs."""
    allowed = None
    if snapshot is not None:
        payload = read_json(Path(snapshot)) if isinstance(snapshot, (str, Path)) else snapshot
        items = payload.get("regions", payload) if isinstance(payload, dict) else payload
        allowed = {str(x.get("region_id", x.get("id"))) if isinstance(x, dict) else str(x) for x in items}
    parent = root / "regions"
    return sorted(p for p in parent.iterdir()
                  if p.is_dir() and (p / "completion.json").is_file()
                  and (allowed is None or p.name in allowed)) if parent.is_dir() else []


def qa_summary(region_dirs: list[Path]) -> tuple[list[dict], dict[str, int]]:
    """Keep check evidence, rather than deriving a pass from a completion marker."""
    rows = []
    for directory in region_dirs:
        payload = read_json(directory / "qa.json", {})
        checks = payload.get("checks", []) if isinstance(payload, dict) else payload
        for item in checks:
            row = dict(item)
            row["region_id"] = directory.name
            row.setdefault("status", "not_checked")
            row.setdefault("check", row.get("name", "unspecified"))
            rows.append(row)
        if not checks:
            rows.append({"region_id": directory.name, "check": "qa_manifest", "status": "not_checked"})
    return rows, dict(Counter(str(row["status"]) for row in rows))


def basic_metrics(x: Any, y: Any) -> dict[str, Any]:
    """Unweighted descriptive metrics at the supplied, explicitly stated grain."""
    import numpy as np
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    valid = np.isfinite(x) & np.isfinite(y)
    x, y = x[valid], y[valid]
    if not x.size:
        return {"n": 0, "bias": None, "mae": None, "rmse": None, "r": None, "r2": None, "nse": None}
    d = y - x
    variable = x.size >= 2 and np.ptp(x) > 0 and np.ptp(y) > 0
    r = float(np.corrcoef(x, y)[0, 1]) if variable else None
    denom = float(np.sum((x - x.mean()) ** 2))
    return {"n": int(x.size), "bias": float(d.mean()), "mae": float(np.abs(d).mean()),
            "rmse": float(np.sqrt(np.mean(d * d))), "r": r, "r2": r * r if r is not None else None,
            "nse": float(1 - np.sum(d * d) / denom) if denom > 0 else None}


def reference_status(root: Path) -> dict[str, Any]:
    for candidate in (root / "references" / "status.json", root / "reference_status.json"):
        if candidate.is_file():
            return {"source": str(candidate), "products": read_json(candidate)}
    regional = sorted(set(root.glob("references/**/statuses.json")) | set(root.glob("regions/*/references/statuses.json")))
    if regional:
        return {"status": "regional_manifests_available", "manifests": [str(p) for p in regional],
                "products": [read_json(path) for path in regional]}
    return {"products": [], "status": "not_available", "reason": "Reference harmonization has not produced a status manifest."}


def reference_region_coverage(root: Path, primary_ids: set[str]) -> list[dict]:
    """An attempted product comparison does not imply complete reference coverage."""
    rows = []
    for rid in sorted(primary_ids):
        directories = (root / "references" / rid, root / "regions" / rid / "references")
        manifests = [p / "statuses.json" for p in directories if (p / "statuses.json").is_file()]
        pairs = [p / "reference_pairs.csv.gz" for p in directories if (p / "reference_pairs.csv.gz").is_file()]
        rows.append(dict(region_id=rid, attempted=bool(manifests or pairs), pair_file_available=bool(pairs),
                         status_manifests=[str(p) for p in manifests],
                         status="attempted_check_product_manifests" if manifests or pairs else "not_attempted_or_no_saved_evidence"))
    return rows


def _owned_recovery_counts(frame, spatial_path: Path, expected_days: int):
    """Correct count support from compact masks, without changing upstream data."""
    import numpy as np
    result = frame.copy()
    result["n_runnable_pixels"] = result["n_pixels"]
    required = ("first_fire_day", "fire_day_count", "run_valid_mask", "owned_area_m2")
    with np.load(spatial_path, allow_pickle=False) as data:
        if not all(k in data for k in required):
            raise ValueError("Recovery ownership count requires compact fire-time and area masks")
        first, fire_count, runmask, area = [np.asarray(data[k]) for k in required]
    if len({x.shape for x in (first, fire_count, runmask, area)}) != 1 or expected_days <= 0:
        raise ValueError("Invalid recovery masks or analysis duration")
    base = np.isfinite(first) & (first >= 0) & runmask.astype(bool) & np.isfinite(area) & (area > 0)
    counts = {}
    for fire_class, lag in result[["fire_class", "lag_days"]].drop_duplicates().itertuples(index=False, name=None):
        if fire_class == "all":
            selected = fire_count >= 1
        elif fire_class == "repeated_positive_days":
            selected = fire_count > 1
        elif fire_class == "single_positive_day":
            selected = fire_count == 1
        else:
            raise ValueError("Unknown recovery fire class: " + str(fire_class))
        target = first + int(lag)
        counts[(fire_class, lag)] = int(np.sum(base & selected & (target >= 0) & (target < expected_days)))
    result["n_pixels"] = [counts[(state, lag)] for state, lag in result[["fire_class", "lag_days"]].itertuples(index=False, name=None)]
    result["pixel_count_support"] = "runnable, positive unique-owned area, valid first-fire lag"
    return result


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fields or ["status"])
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v for k, v in row.items()})


def _load_annual(region_dirs: list[Path]):
    import pandas as pd
    frames = []
    mandatory = {"region_id", "scenario", "year", "variable", "component", "mean", "group_type", "group"}
    for directory in region_dirs:
        path = directory / "annual.csv"
        if not path.is_file():
            continue
        frame = pd.read_csv(path, dtype={"region_id": str, "scenario": str, "variable": str,
                                       "component": str, "group_type": str, "group": str})
        missing = mandatory - set(frame.columns)
        if missing:
            raise ValueError(f"{path}: missing annual fields {sorted(missing)}")
        if not (frame["region_id"] == directory.name).all():
            raise ValueError(f"Region identity mismatch in {path}")
        for field, default in (("units", "unknown"), ("valid_area_m2", float("nan")),
                               ("aggregation", "unknown"), ("n_days", float("nan"))):
            if field not in frame:
                frame[field] = default
        frame["complete_period"] = (frame["complete_period"].astype(str).str.lower().isin(("true", "1", "1.0"))
                                    if "complete_period" in frame else frame["year"] != 2014)
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=sorted(mandatory | {"units", "valid_area_m2", "aggregation", "complete_period"}))
    result = pd.concat(frames, ignore_index=True)
    for column in ("scenario", "variable", "component", "group_type", "group", "units", "aggregation"):
        result[column] = result[column].astype("category")
    identity = ["region_id", "scenario", "year", "variable", "component", "group_type", "group"]
    if result.duplicated(identity).any():
        raise ValueError("Duplicate annual region/scenario/component/group/year rows: cannot report without resolving source identity.")
    return result


def _monthly_compact(region_dirs: list[Path]):
    """One streaming pass over potentially multi-GB monthly tables."""
    import numpy as np
    import pandas as pd
    keys = ["scenario", "variable", "component", "group_type", "group", "year", "month"]
    sums: dict[tuple, list[float]] = {}
    for directory in region_dirs:
        path = directory / "monthly.csv"
        if not path.is_file():
            continue
        for frame in pd.read_csv(path, chunksize=100_000):
            if not set(keys + ["mean", "valid_area_m2"]).issubset(frame.columns):
                raise ValueError(f"Monthly schema mismatch: {path}")
            frame = frame[frame["group_type"].isin(("overall", "pyrome", "vegetation"))].copy()
            if "complete_period" in frame:
                frame = frame[frame["complete_period"].astype(str).str.lower().isin(("true", "1", "1.0"))].copy()
            if frame.empty:
                continue
            # Monthly flux integrals become daily rates before combining unequal months.
            value = frame["mean"].to_numpy(dtype=float)
            if "aggregation" in frame and "n_days" in frame:
                summed = frame["aggregation"].astype(str).eq("sum").to_numpy()
                days = frame["n_days"].to_numpy(dtype=float)
                value = np.where(summed, np.divide(value, days, out=np.full_like(value, np.nan), where=days > 0), value)
            area = frame["valid_area_m2"].to_numpy(dtype=float)
            good = np.isfinite(value) & np.isfinite(area) & (area > 0)
            frame = frame.loc[good, keys].copy()
            frame["numerator"] = value[good] * area[good]
            frame["denominator"] = area[good]
            frame["count"] = 1
            grouped = frame.groupby(keys, observed=True, dropna=False)[["numerator", "denominator", "count"]].sum()
            for key, numerator, denominator, count in grouped.itertuples(index=True, name=None):
                aggregate = sums.setdefault(tuple(key), [0.0, 0.0, 0.0])
                aggregate[0] += float(numerator)
                aggregate[1] += float(denominator)
                aggregate[2] += float(count)
    rows = [dict(zip(keys, key), numerator=value[0], denominator=value[1], n_regions=int(value[2]),
                 mean=value[0] / value[1]) for key, value in sums.items() if value[1] > 0]
    return pd.DataFrame(rows, columns=keys + ["numerator", "denominator", "n_regions", "mean"])


def _paired(frame):
    import pandas as pd
    import numpy as np
    if frame.empty:
        return pd.DataFrame()
    frame = frame[frame["complete_period"] & frame["scenario"].isin(SCENARIOS)].copy()
    if frame.empty:
        return pd.DataFrame()
    keys = ["region_id", "year", "group_type", "group"]
    paired = frame.pivot(index=keys, columns="scenario", values="mean")
    if not set(SCENARIOS).issubset(paired.columns):
        return pd.DataFrame()
    paired = paired.reset_index()
    paired = paired[np.isfinite(paired["No_Fire"]) & np.isfinite(paired["VIIRS_Fire"])].copy()
    paired["difference"] = paired["VIIRS_Fire"] - paired["No_Fire"]
    areas = frame.pivot(index=keys, columns="scenario", values="valid_area_m2").reset_index()
    if set(SCENARIOS).issubset(areas.columns):
        areas["area_m2"] = areas[list(SCENARIOS)].min(axis=1)
        areas["same_area"] = np.isclose(areas["No_Fire"], areas["VIIRS_Fire"], rtol=1e-9, atol=.01)
        paired = paired.merge(areas[keys + ["area_m2", "same_area"]], on=keys, validate="one_to_one")
    return paired


def _set_style():
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.titlesize": 11, "axes.labelsize": 10,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "axes.edgecolor": "#5A6168", "axes.labelcolor": "#26313A",
                         "text.color": "#26313A", "axes.titleweight": "semibold",
                         "xtick.color": "#52606B", "ytick.color": "#52606B",
                         "figure.facecolor": "white", "axes.facecolor": "white",
                         "savefig.facecolor": "white", "pdf.fonttype": 42, "ps.fonttype": 42})
    return plt


def _plot_scatter(ax, paired, unit, title="Scenario agreement"):
    import numpy as np
    ax.set_title(title)
    if paired.empty:
        ax.text(.5, .5, "No complete, paired values", transform=ax.transAxes, ha="center")
        return
    x = paired["No_Fire"].to_numpy(dtype=float)
    y = paired["VIIRS_Fire"].to_numpy(dtype=float)
    if len(x) > 1500:
        ax.hexbin(x, y, gridsize=35, mincnt=1, cmap="cividis", linewidths=0, bins="log", rasterized=True)
    else:
        ax.scatter(x, y, s=13, color=COLORS["No_Fire"], alpha=.5, linewidths=0, rasterized=True)
    low, high = float(min(x.min(), y.min())), float(max(x.max(), y.max()))
    if low == high:
        low, high = low - max(1.0, abs(low) * .05), high + max(1.0, abs(high) * .05)
    else:
        pad = (high - low) * .04
        low, high = low - pad, high + pad
    ax.plot([low, high], [low, high], "--", color="#5F666C", lw=.9)
    ax.set(xlim=(low, high), ylim=(low, high), xlabel=f"No fire ({unit})", ylabel=f"VIIRS fire ({unit})")
    metrics = basic_metrics(x, y)
    rval = "undefined" if metrics["r"] is None else f"{metrics['r']:.3f}"
    ax.text(.03, .97, f"n = {metrics['n']:,} region-years\nr = {rval}; RMSE = {metrics['rmse']:.3g}",
            transform=ax.transAxes, va="top", fontsize=8.5,
            bbox={"facecolor": "white", "alpha": .82, "edgecolor": "none", "pad": 2})
    ax.set_aspect("equal", adjustable="box")


def _plot_distribution(ax, paired, unit, groups=None):
    import numpy as np
    ax.set_title("Fire − no-fire distribution")
    if paired.empty:
        ax.text(.5, .5, "No paired region-years", transform=ax.transAxes, ha="center")
        return
    groups = groups or list(dict.fromkeys(paired["group"].astype(str)))
    for index, group in enumerate(groups):
        values = paired.loc[paired["group"].astype(str).eq(str(group)), "difference"].to_numpy(float)
        values = values[np.isfinite(values)]
        if not values.size:
            continue
        # KDE fails or invents spread for constant/zero vectors; show their point mass.
        if len(values) >= 3 and np.ptp(values) > np.finfo(float).eps * max(1, float(np.max(np.abs(values)))):
            violin = ax.violinplot(values, positions=[index], vert=False, widths=.75,
                                   showextrema=False, showmedians=False)
            for body in violin["bodies"]:
                body.set_facecolor(COLORS["VIIRS_Fire"])
                body.set_alpha(.24)
                body.set_edgecolor("none")
        ax.boxplot(values, positions=[index], vert=False, widths=.20, showfliers=False,
                   manage_ticks=False, medianprops={"color": "#26313A", "linewidth": 1.2},
                   boxprops={"color": "#26313A", "linewidth": .8},
                   whiskerprops={"color": "#26313A", "linewidth": .8},
                   capprops={"color": "#26313A", "linewidth": .8})
        # Points are region means across complete years, not independent pixel-days.
        points = paired.loc[paired["group"].astype(str).eq(str(group))].groupby("region_id")["difference"].mean()
        rng = np.random.default_rng(int(hashlib.sha256(str(group).encode()).hexdigest()[:8], 16))
        ax.scatter(points, index + rng.uniform(-.12, .12, len(points)), s=10,
                   alpha=.55, color=COLORS["VIIRS_Fire"], linewidths=0, rasterized=True)
        zero = float(np.mean(values == 0))
        ax.annotate(f"n={len(values)}; zero={zero:.0%}", (1, index),
                    xycoords=("axes fraction", "data"), xytext=(-2, 6), textcoords="offset points",
                    ha="right", fontsize=7.5, color="#5F666C")
    ax.axvline(0, color="#747B81", lw=.8, ls="--")
    ax.set(yticks=list(range(len(groups))), yticklabels=[str(g) for g in groups],
           xlabel=f"Difference ({unit})", ylim=(-.6, len(groups) - .4))
    ax.grid(axis="x", color="#E4E8EB", lw=.5)


def _plot_annual(ax, frame, unit):
    import numpy as np
    ax.set_title("Area-weighted annual values")
    for scenario in SCENARIOS:
        rows = frame[frame["scenario"].eq(scenario)]
        points = []
        for year, sub in rows.groupby("year", observed=True):
            x, w = sub["mean"].to_numpy(float), sub["valid_area_m2"].to_numpy(float)
            valid = np.isfinite(x) & np.isfinite(w) & (w > 0)
            if valid.any():
                points.append((int(year), float(np.average(x[valid], weights=w[valid]))))
        if points:
            xx, yy = zip(*points)
            ax.plot(xx, yy, marker="o" if scenario == "No_Fire" else "s", ms=3,
                    color=COLORS[scenario], lw=1.3, label=scenario.replace("_", " "))
            if 2014 in xx:
                ax.axvspan(2013.8, 2014.2, color="#E4E8EB", alpha=.5)
    ax.set(xlabel="Year (2014 is partial)", ylabel=unit)
    ax.grid(axis="y", color="#E4E8EB", lw=.5)
    if ax.lines:
        ax.legend(frameon=False, fontsize=8)


def _plot_seasonal(ax, monthly, unit, is_flux=False):
    import numpy as np
    ax.set_title("Seasonal cycle · complete months")
    for scenario in SCENARIOS:
        rows = monthly[monthly["scenario"].eq(scenario)] if not monthly.empty else monthly
        points = []
        for month, sub in rows.groupby("month", observed=True) if not rows.empty else []:
            numerator, denominator = float(sub["numerator"].sum()), float(sub["denominator"].sum())
            if denominator > 0:
                points.append((int(month), numerator / denominator))
        if points:
            x, y = zip(*points)
            ax.plot(x, y, marker="o" if scenario == "No_Fire" else "s", ms=3,
                    color=COLORS[scenario], lw=1.3, label=scenario.replace("_", " "))
    if not ax.lines:
        ax.text(.5, .5, "Monthly summaries not available", ha="center", transform=ax.transAxes)
    daily_unit = unit + " day-1" if is_flux and "day" not in unit else unit
    ax.set(xticks=np.arange(1, 13), xticklabels=list("JFMAMJJASOND"), xlabel="Calendar month",
           ylabel=f"{'Daily rate' if is_flux else 'State'} ({daily_unit})")
    ax.grid(axis="y", color="#E4E8EB", lw=.5)


def _component_figure(frame, monthly, label, group_type, group=None):
    plt = _set_style()
    data = frame[frame["group_type"].eq(group_type)]
    cycle = monthly[monthly["group_type"].eq(group_type)] if not monthly.empty else monthly
    if group is not None:
        data = data[data["group"].astype(str).eq(str(group))]
        cycle = cycle[cycle["group"].astype(str).eq(str(group))] if not cycle.empty else cycle
    if data.empty:
        return None
    unit = str(data["units"].iloc[0])
    aggregation = ", ".join(sorted(set(data["aggregation"].astype(str))))
    paired = _paired(data)
    fig, axes = plt.subplots(2, 2, figsize=(11.7, 8.3), layout="constrained")
    group_label = f"{group_type}: {group}" if group is not None else group_type
    fig.suptitle(f"{label} | {group_label}\n{data['region_id'].nunique()} regions · annual aggregation: {aggregation}", fontsize=14)
    _plot_scatter(axes[0, 0], paired, unit)
    _plot_distribution(axes[0, 1], paired, unit)
    # Mixed categorical groups are not recombined in curves: keep separate support.
    _plot_annual(axes[1, 0], data, unit)
    _plot_seasonal(axes[1, 1], cycle, unit, aggregation == "sum")
    fig.supxlabel("Interim analysed support; no design-based whole-Mediterranean estimate. "
                   "Violin/box: region-years; dots: region means. No confidence intervals implied.", fontsize=8)
    return fig


def _combined_figure(frame, label):
    plt = _set_style()
    fig, axes = plt.subplots(1, 3, figsize=(14, 5.3), layout="constrained")
    unit = str(frame["units"].iloc[0])
    for ax, family in zip(axes, ("overall", "pyrome", "vegetation")):
        paired = _paired(frame[frame["group_type"].eq(family)])
        _plot_distribution(ax, paired, unit)
        ax.set_title(family.replace("_", " ").title())
    fig.suptitle(f"{label}: paired annual responses across groups", fontsize=14)
    fig.supxlabel("Fire − no-fire. Group supports are analysed separately; mixed regions may contribute to several groups. "
                   "Region-year distributions are descriptive, not confidence intervals.", fontsize=8)
    return fig


def _anomaly_figure(frame, label, out, token):
    import numpy as np
    import pandas as pd
    plt = _set_style()
    rows = []
    source = frame[frame["complete_period"] & frame["group_type"].isin(("overall", "pyrome", "vegetation"))]
    for key, sub in source.groupby(["scenario", "group_type", "group", "year"], observed=True):
        value, area = sub["mean"].to_numpy(float), sub["valid_area_m2"].to_numpy(float)
        good = np.isfinite(value) & np.isfinite(area) & (area > 0)
        if good.any():
            rows.append(dict(zip(("scenario", "group_type", "group", "year"), key),
                             mean=float(np.average(value[good], weights=area[good])), n_regions=int(good.sum())))
    if not rows:
        return None
    data = pd.DataFrame(rows)
    data["group_label"] = data["group_type"].astype(str) + ": " + data["group"].astype(str)
    data["anomaly"] = data["mean"] - data.groupby(["scenario", "group_label"])["mean"].transform("mean")
    data.to_csv(out / f"annual_anomaly_{token}.csv", index=False)
    groups, years = sorted(data["group_label"].unique()), sorted(data["year"].unique())
    scale = max(float(np.max(np.abs(data["anomaly"]))), 1e-12)
    fig, axes = plt.subplots(1, 2, figsize=(12, max(4.5, len(groups) * .28 + 1.7)), layout="constrained")
    for ax, scenario in zip(axes, SCENARIOS):
        table = data[data["scenario"].eq(scenario)].pivot(index="group_label", columns="year", values="anomaly").reindex(index=groups, columns=years)
        artist = ax.imshow(table.to_numpy(float), cmap="RdBu_r", vmin=-scale, vmax=scale, aspect="auto")
        ax.set(xticks=np.arange(len(years)), xticklabels=years, yticks=np.arange(len(groups)),
               yticklabels=groups, title=scenario.replace("_", " "), xlabel="Complete calendar year")
        ax.tick_params(axis="y", labelsize=8.5)
    fig.colorbar(artist, ax=axes, shrink=.7, label=str(frame["units"].iloc[0]))
    fig.suptitle(f"{label}: annual anomalies", fontsize=14)
    fig.supxlabel(f"Each group/scenario is centred on its own mean over available complete years {min(years)}–{max(years)}. "
                   "Shared colour limits; anomalies are descriptive and do not establish a trend.", fontsize=8)
    return fig


def _pixel_figure(region_dirs, variable, component, label, unit, out, token):
    """Area-weighted moments for all owned pixels; bounded uniform display."""
    import numpy as np
    import pandas as pd
    plt = _set_style()
    rng = np.random.default_rng(42)
    sample = pd.DataFrame()
    totals = dict(n=0, w=0., wx=0., wy=0., wxx=0., wyy=0., wxy=0., wd=0., wdd=0.)
    keybase = f"{variable}__{component}"
    for directory in region_dirs:
        path = directory / "spatial.npz"
        if not path.is_file():
            continue
        with np.load(path, allow_pickle=False) as ds:
            keys = [keybase + "__" + s for s in SCENARIOS]
            if not all(k in ds for k in keys):
                continue
            x, y = (np.asarray(ds[k], float) for k in keys)
            area = np.asarray(ds["owned_area_m2"], float)
            mask = np.asarray(ds["run_valid_mask"], bool) & (area > 0) & np.isfinite(x) & np.isfinite(y)
            iy, ix = np.where(mask)
            x, y, area = x[mask], y[mask], area[mask]
            if not len(x):
                continue
            delta = y - x
            totals["n"] += len(x)
            for field, values in (("w", area), ("wx", area*x), ("wy", area*y), ("wxx", area*x*x),
                                  ("wyy", area*y*y), ("wxy", area*x*y), ("wd", area*delta), ("wdd", area*delta*delta)):
                totals[field] += float(values.sum())
            incoming = pd.DataFrame(dict(region_id=directory.name, row=iy, col=ix,
                                         lat=np.asarray(ds["lat"])[iy], lon=np.asarray(ds["lon"])[ix],
                                         no_fire=x, viirs_fire=y, owned_area_m2=area, display_priority=rng.random(len(x))))
            sample = pd.concat([sample, incoming], ignore_index=True)
            if len(sample) > 20_000:
                sample = sample.nsmallest(20_000, "display_priority")
    if not totals["n"]:
        return None
    sample.to_csv(out / f"pixel_pairs_{token}.csv.gz", index=False, compression="gzip")
    w = totals["w"]
    vx, vy = max(0., totals["wxx"] - totals["wx"]**2 / w), max(0., totals["wyy"] - totals["wy"]**2 / w)
    corr = (totals["wxy"] - totals["wx"]*totals["wy"] / w) / math.sqrt(vx*vy) if vx > 0 and vy > 0 else None
    metric = dict(n_pixels=totals["n"], n_display=len(sample), area_m2=w,
                  bias=totals["wd"] / w, rmse=math.sqrt(totals["wdd"] / w), r=corr,
                  grain="paired owned pixels, ten-year time means", display_sampling="uniform priority sample; seed42; maximum20000")
    atomic_json(out / f"pixel_metrics_{token}.json", metric)
    fig, axes = plt.subplots(1, 2, figsize=(11.7, 5.2), layout="constrained")
    x, y = sample["no_fire"].to_numpy(), sample["viirs_fire"].to_numpy()
    if len(sample) > 1500:
        art = axes[0].hexbin(x, y, gridsize=40, mincnt=1, cmap="cividis", bins="log", linewidths=0, rasterized=True)
        fig.colorbar(art, ax=axes[0], label="Displayed pixel count (log scale)")
    else:
        axes[0].scatter(x, y, s=9, alpha=.5, color=COLORS["No_Fire"], linewidths=0)
    lo, hi = float(min(x.min(), y.min())), float(max(x.max(), y.max()))
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    axes[0].plot([lo, hi], [lo, hi], color="#747B81", ls="--", lw=.8)
    axes[0].set(xlabel=f"No fire ({unit})", ylabel=f"VIIRS fire ({unit})", title="Paired pixel time means")
    axes[0].set_aspect("equal", adjustable="box")
    delta = y - x
    if len(delta) > 2 and np.ptp(delta) > 0:
        violin = axes[1].violinplot(delta, vert=False, showmedians=True)
        for body in violin["bodies"]:
            body.set_facecolor(COLORS["VIIRS_Fire"]); body.set_alpha(.3)
    else:
        axes[1].scatter(delta, np.ones(len(delta)), color=COLORS["VIIRS_Fire"], s=14)
    axes[1].boxplot(delta, vert=False, showfliers=False, widths=.15)
    axes[1].set(xlabel=f"Fire − no fire ({unit})", yticks=[], title="Pixel difference distribution")
    axes[1].axvline(0, color="#747B81", ls="--", lw=.8)
    fig.suptitle(f"{label}: pixel-wise scenario comparison\n{totals['n']:,} owned pixels · area-weighted RMSE {metric['rmse']:.3g}", fontsize=14)
    fig.supxlabel(f"Uniform display sample {len(sample):,}; exact area-weighted metrics use all paired owned pixels. "
                   "The displayed distribution counts pixels and is not a confidence interval or independent-pixel validation.", fontsize=8)
    return fig


def _pilot_pixel_maps(region_dirs, variable, component, label, unit):
    import numpy as np
    plt = _set_style()
    records = []
    for directory in region_dirs[:3]:
        path = directory / "spatial.npz"
        if not path.is_file():
            continue
        with np.load(path, allow_pickle=False) as data:
            keys = [f"{variable}__{component}__{s}" for s in SCENARIOS]
            if not all(k in data for k in keys):
                continue
            mask = np.asarray(data["run_valid_mask"], bool) & (np.asarray(data["owned_area_m2"]) > 0)
            arrays = [np.where(mask, np.asarray(data[k], float), np.nan) for k in keys]
            records.append((directory.name, np.asarray(data["lon"]), np.asarray(data["lat"]), arrays))
    if not records:
        return None
    finite = np.concatenate([a[np.isfinite(a)] for _, _, _, arrays in records for a in arrays])
    if not finite.size:
        return None
    low, high = float(finite.min()), float(finite.max())
    if low == high:
        high = low + max(1., abs(low) * .01)
    differences = [arrays[1] - arrays[0] for _, _, _, arrays in records]
    dscale = max([float(np.nanmax(np.abs(d))) for d in differences if np.isfinite(d).any()] + [1e-12])
    fig, axes = plt.subplots(len(records), 3, figsize=(13, 4 * len(records) + 1), squeeze=False, layout="constrained")
    for index, (rid, lon, lat, arrays) in enumerate(records):
        for col, field in enumerate((*arrays, differences[index])):
            kw = dict(cmap="cividis", vmin=low, vmax=high) if col < 2 else dict(cmap="RdBu_r", vmin=-dscale, vmax=dscale)
            artist = axes[index, col].pcolormesh(lon, lat, field, shading="auto", rasterized=True, **kw)
            axes[index, col].set(xlabel="Longitude (°E)", ylabel="Latitude (°N)",
                                 title=(SCENARIOS[col].replace("_", " ") if col < 2 else "Fire − no fire") + f" | region {index+1}")
            axes[index, col].set_aspect(1. / max(.1, math.cos(math.radians(float(lat.mean())))))
            fig.colorbar(artist, ax=axes[index, col], shrink=.7, label=unit)
    fig.suptitle(f"{label}: actual model-pixel maps for three stable-ID examples", fontsize=14)
    fig.supxlabel(textwrap.fill("Time means; common run and positive ownership masks. Regions: " + "; ".join(r[0] for r in records), 155), fontsize=7)
    return fig


def _region_maps(region_dirs, variable, component):
    """Read one component at a time; never retain all 50 maps × 293 regions."""
    import numpy as np
    rows = []
    keybase = f"{variable}__{component}"
    for directory in region_dirs:
        path = directory / "spatial.npz"
        if not path.is_file():
            continue
        with np.load(path, allow_pickle=False) as ds:
            keys = [keybase + "__" + scenario for scenario in SCENARIOS]
            if not all(k in ds for k in keys) or "owned_area_m2" not in ds:
                continue
            area = np.asarray(ds["owned_area_m2"], dtype=float)
            mask = np.asarray(ds["run_valid_mask"], dtype=bool) if "run_valid_mask" in ds else area > 0
            x, y = (np.asarray(ds[key], dtype=float) for key in keys)
            valid = mask & (area > 0) & np.isfinite(x) & np.isfinite(y)
            if not valid.any():
                continue
            rows.append({"region_id": directory.name, "lon": float(np.mean(ds["lon"])),
                         "lat": float(np.mean(ds["lat"])), "No_Fire": float(np.average(x[valid], weights=area[valid])),
                         "VIIRS_Fire": float(np.average(y[valid], weights=area[valid])),
                         "difference": float(np.average(y[valid] - x[valid], weights=area[valid])),
                         "owned_area_m2": float(area[valid].sum())})
    return rows


def _map_figure(rows, label, unit):
    import numpy as np
    plt = _set_style()
    if not rows:
        return None
    # Coordinates are actual longitude/latitude, with aspect corrected at mean latitude.
    # This is explicitly a region-centre display, not a false continuous raster.
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.8), layout="constrained")
    lon, lat = np.array([r["lon"] for r in rows]), np.array([r["lat"] for r in rows])
    xy = np.array([[r[s] for r in rows] for s in SCENARIOS])
    low, high = float(xy.min()), float(xy.max())
    if low == high:
        high = low + max(1.0, abs(low) * .01)
    for i, key in enumerate((*SCENARIOS, "difference")):
        values = np.array([r[key] for r in rows])
        kwargs = {"cmap": "cividis", "vmin": low, "vmax": high}
        if key == "difference":
            scale = max(float(np.max(np.abs(values))), 1e-12)
            kwargs = {"cmap": "RdBu_r", "vmin": -scale, "vmax": scale}
        artist = axes[i].scatter(lon, lat, c=values, s=25, edgecolor="white", linewidth=.25, **kwargs)
        axes[i].set(xlabel="Longitude (°E)", ylabel="Latitude (°N)", title=key.replace("_", " "))
        axes[i].set_aspect(1.0 / max(.1, math.cos(math.radians(float(lat.mean())))))
        axes[i].grid(color="#E4E8EB", lw=.5)
        fig.colorbar(artist, ax=axes[i], shrink=.7, label=unit)
    fig.suptitle(f"{label} · time-mean values at region centres", fontsize=14)
    fig.supxlabel("Each marker is one region's paired, owned-area mean at its true geographic centre. "
                   "No interpolation; marker area is not cube coverage.", fontsize=8)
    return fig


def block_year_interval(values, areas, blocks, years, repeats=2000, seed=42):
    """Two-way bootstrap of area-weighted effects on completed support.

    Resampling units are geographic blocks and years.  Every record in a drawn
    block/year receives their multiplicity product, so neighbouring region-years
    are not independently resampled.  This is not product/parameter uncertainty.
    """
    import numpy as np
    values, areas = np.asarray(values, float), np.asarray(areas, float)
    blocks, years = np.asarray(blocks).astype(str), np.asarray(years).astype(str)
    valid = np.isfinite(values) & np.isfinite(areas) & (areas > 0)
    values, areas, blocks, years = values[valid], areas[valid], blocks[valid], years[valid]
    result = {"estimate": None, "lower": None, "upper": None, "n_blocks": 0, "n_years": 0,
              "repeats": int(repeats), "seed": int(seed), "status": "insufficient_data",
              "p_exploratory": None, "p_status": "insufficient_blocks_or_years",
              "p_method": "Approximate two-sided centered two-way spatial-block/year bootstrap"}
    if not len(values):
        return result
    ub, bi = np.unique(blocks, return_inverse=True)
    uy, yi = np.unique(years, return_inverse=True)
    result.update(estimate=float(np.average(values, weights=areas)), n_blocks=len(ub), n_years=len(uy))
    if len(ub) < 2 or len(uy) < 2:
        return result
    numerator, denominator = np.zeros((len(ub), len(uy))), np.zeros((len(ub), len(uy)))
    np.add.at(numerator, (bi, yi), values * areas)
    np.add.at(denominator, (bi, yi), areas)
    rng = np.random.default_rng(seed)
    bw = rng.multinomial(len(ub), np.full(len(ub), 1 / len(ub)), size=repeats)
    yw = rng.multinomial(len(uy), np.full(len(uy), 1 / len(uy)), size=repeats)
    nn = np.einsum("rb,by,ry->r", bw, numerator, yw, optimize=True)
    dd = np.einsum("rb,by,ry->r", bw, denominator, yw, optimize=True)
    draws = nn[dd > 0] / dd[dd > 0]
    if len(draws):
        lo, hi = np.quantile(draws, [.025, .975])
        result.update(lower=float(lo), upper=float(hi), status="estimated", finite_repeats=len(draws))
        if len(ub) >= 5 and len(uy) >= 3:
            resolution = max(1e-14, np.finfo(float).eps * max(1., float(np.max(np.abs(draws)))) * 100)
            if np.ptp(draws) <= resolution:
                result["p_status"] = "undefined_constant_resampling_distribution"
            else:
                centered = np.abs(draws - result["estimate"])
                result.update(p_exploratory=float((1 + np.count_nonzero(centered >= abs(result["estimate"]))) / (len(draws) + 1)),
                              p_status="exploratory_approximation")
    return result


def benjamini_hochberg(p_values):
    """BH-adjust valid p-values; undefined tests remain undefined and visible."""
    import numpy as np
    values = np.asarray([np.nan if p is None else p for p in p_values], float)
    valid = np.isfinite(values)
    if ((values[valid] < 0) | (values[valid] > 1)).any():
        raise ValueError("p-values must lie in [0,1]")
    indices = np.flatnonzero(valid)
    order = indices[np.argsort(values[indices], kind="stable")]
    result = [None] * len(values)
    if len(order):
        adjusted = values[order] * len(order) / np.arange(1, len(order) + 1)
        adjusted = np.minimum(1., np.minimum.accumulate(adjusted[::-1])[::-1])
        for index, q in zip(order, adjusted):
            result[int(index)] = float(q)
    return result


def _positions(region_dirs):
    import numpy as np
    result = {}
    for directory in region_dirs:
        path = directory / "spatial.npz"
        if path.is_file():
            with np.load(path, allow_pickle=False) as data:
                if "lat" in data and "lon" in data:
                    result[directory.name] = (float(np.mean(data["lon"])), float(np.mean(data["lat"])))
    return result


def _science_figures(annual, region_dirs, out, pdf, figures):
    import numpy as np
    import pandas as pd
    plt = _set_style()
    coords = _positions(region_dirs)
    primary = (("gpp", "gpp"), ("nee", "nee"), ("eco_respiration", "eco_respiration"),
               ("evapotranspiration", "evapotranspiration"), ("cEco", "cVeg"),
               ("cEco", "cEcosystem"), ("c_fire_efflux", "fire_carbon_emission"))
    effects = []
    for variable, component in primary:
        frame = annual[annual["variable"].eq(variable) & annual["component"].eq(component)]
        pairs = _paired(frame)
        if pairs.empty:
            continue
        pairs = pairs[pairs["region_id"].isin(coords)]
        if "same_area" in pairs:
            pairs = pairs[pairs["same_area"]]
        pairs = pairs[pairs["group_type"].isin(("overall", "pyrome", "vegetation"))].copy()
        pairs["spatial_block"] = [f"{math.floor(coords[r][0] / 2)}_{math.floor(coords[r][1] / 2)}" for r in pairs["region_id"]]
        unit = str(frame["units"].iloc[0])
        for (family, group), rows in pairs.groupby(["group_type", "group"], observed=True):
            result = block_year_interval(rows["difference"], rows["area_m2"], rows["spatial_block"], rows["year"])
            effects.append(dict(variable=variable, component=component, group_type=str(family), group=str(group),
                                units=unit, n_regions=int(rows["region_id"].nunique()),
                                block_definition="2 degree longitude/latitude cells; sensitivity not yet assessed", **result))
        selected = [r for r in effects if r["variable"] == variable and r["component"] == component]
        if not selected:
            continue
        fig, ax = plt.subplots(figsize=(10, max(4.2, .38 * len(selected) + 1.5)), layout="constrained")
        for index, record in enumerate(selected):
            if record["estimate"] is None:
                continue
            ax.scatter([record["estimate"]], [index], color=COLORS["VIIRS_Fire"], s=25)
            if record["lower"] is not None:
                ax.plot([record["lower"], record["upper"]], [index, index], color=COLORS["VIIRS_Fire"], lw=1.5)
        ax.set(yticks=np.arange(len(selected)), yticklabels=[r["group_type"] + ": " + r["group"] for r in selected],
               xlabel=f"VIIRS fire − no fire ({unit})", title=f"{variable} / {component}: modelled annual response")
        ax.axvline(0, color="#747B81", ls="--", lw=.8)
        ax.grid(axis="x", color="#E4E8EB", lw=.5)
        fig.supxlabel("95% percentile intervals: 2,000 two-way resamples of 2° spatial blocks and complete years, seed 42. "
                       "Exploratory completed-subset variability; not reference or parameter uncertainty. No interval when support is insufficient.", fontsize=8)
        _save_figure(fig, out / "figures" / "science" / f"effects_{slug(variable+'__'+component)}", pdf, figures,
                     {"kind": "block_year_effects", "variable": variable, "component": component, "source": "effects_block_year.csv"})
    q_values = benjamini_hochberg([row["p_exploratory"] for row in effects])
    n_tests = sum(q is not None for q in q_values)
    for row, q in zip(effects, q_values):
        row.update(q_bh_exploratory=q, q_family="Seven predefined responses × overall/pyrome/vegetation valid tests",
                   q_family_n_tests=n_tests, q_below_0_05=None if q is None else q < .05,
                   inference_scope="Exploratory modelled completed-subset response; not observational causal significance")
    _write_rows(out / "effects_block_year.csv", effects)

    # Flux response decomposition. Annual stock means are intentionally not used
    # as final-stock losses; that requires saved initial/end states.
    desired = {(v, c): frame for (v, c), frame in annual[annual["group_type"].eq("overall")].groupby(["variable", "component"], observed=True)}
    flux_parts = []
    for variable, component, sign, title in (("gpp", "gpp", -1, "Reduced carbon uptake (−ΔGPP)"),
                                           ("eco_respiration", "eco_respiration", 1, "Respiration change (ΔReco)"),
                                           ("c_fire_efflux", "fire_carbon_emission", 1, "Direct fire efflux")):
        frame = desired.get((variable, component))
        if frame is None:
            continue
        paired = _paired(frame)
        if paired.empty:
            continue
        paired = paired[paired["same_area"]]
        for year, rows in paired.groupby("year"):
            valid = np.isfinite(rows["area_m2"]) & (rows["area_m2"] > 0)
            if valid.any():
                flux_parts.append({"year": int(year), "component": title,
                                   "mean_gC_m2": float(np.average(sign * rows.loc[valid, "difference"], weights=rows.loc[valid, "area_m2"])),
                                   "n_regions": int(rows.loc[valid, "region_id"].nunique())})
    _write_rows(out / "carbon_flux_response.csv", flux_parts)
    if len({row["component"] for row in flux_parts}) == 3:
        fp = pd.DataFrame(flux_parts)
        fig, ax = plt.subplots(figsize=(10.5, 5), layout="constrained")
        for (name, rows), color, marker in zip(fp.groupby("component", sort=False), ("#24647E", "#88783B", "#BD602B"), ("o", "s", "^")):
            ax.plot(rows["year"], rows["mean_gC_m2"], marker=marker, color=color, lw=1.4, label=name)
        ax.set(xlabel="Complete calendar year", ylabel="Annual carbon exchange response (g C m−2)", title="Direct fire loss and subsequent ecosystem flux responses")
        ax.axhline(0, color="#747B81", ls="--", lw=.8)
        ax.legend(frameon=False, fontsize=9)
        fig.supxlabel("Components follow Δ(NEE + fire efflux) = −ΔGPP + ΔReco + fire efflux. "
                       "Mortality and internal carbon transfer are not counted again. Final stock loss is assessed separately.", fontsize=8)
        _save_figure(fig, out / "figures" / "science" / "carbon_flux_response", pdf, figures,
                     {"kind": "carbon_flux_decomposition", "source": "carbon_flux_response.csv"})

    recovery = []
    for directory in region_dirs:
        path = directory / "recovery.csv"
        if path.is_file() and path.stat().st_size > 1:
            try:
                frame = pd.read_csv(path)
            except pd.errors.EmptyDataError:
                continue
            if {"variable", "lag_days", "fire_class", "mean_delta", "area_m2"}.issubset(frame.columns):
                calendar = annual[(annual["region_id"] == directory.name) & annual["group_type"].eq("overall")]
                durations = calendar.groupby("year", observed=True)["n_days"].agg(["min", "max"])
                if not (durations["min"] == durations["max"]).all():
                    raise ValueError("Inconsistent component durations for recovery: " + directory.name)
                recovery.append(_owned_recovery_counts(frame, directory / "spatial.npz", int(durations["max"].sum())))
    if recovery:
        frame = pd.concat(recovery, ignore_index=True)
        frame.to_csv(out / "recovery_region_values.csv", index=False)
        rows = []
        for (variable, state, lag), sub in frame.groupby(["variable", "fire_class", "lag_days"], observed=True):
            good = np.isfinite(sub["mean_delta"]) & np.isfinite(sub["area_m2"]) & (sub["area_m2"] > 0)
            if good.any():
                rows.append(dict(variable=variable, fire_class=state, lag_days=lag,
                                 mean_delta=float(np.average(sub.loc[good, "mean_delta"], weights=sub.loc[good, "area_m2"])),
                                 n_regions=int(sub.loc[good, "region_id"].nunique()), n_pixels=int(sub.loc[good, "n_pixels"].sum()),
                                 n_runnable_pixels=int(sub.loc[good, "n_runnable_pixels"].sum()),
                                 area_m2=float(sub.loc[good, "area_m2"].sum())))
        _write_rows(out / "recovery_summary.csv", rows)
        rec = pd.DataFrame(rows)
        for variable, subset in rec.groupby("variable") if not rec.empty else []:
            fig, axes = plt.subplots(2, 1, figsize=(10.5, 7), sharex=True, layout="constrained")
            recovery_colors = {"all": "#52606B", "repeated_positive_days": "#BD602B", "single_positive_day": "#24647E"}
            for state, data in subset.groupby("fire_class", sort=True):
                color = recovery_colors[state]
                data = data.sort_values("lag_days")
                axes[0].plot(data["lag_days"], data["mean_delta"], label=state.replace("_", " "), color=color)
                axes[1].plot(data["lag_days"], data["n_pixels"], label=state.replace("_", " "), color=color)
            axes[0].axhline(0, color="#747B81", ls="--", lw=.8)
            axes[0].set(ylabel="Fire − no fire (native daily/state units)", title=f"{variable}: response aligned to first detected fire")
            axes[1].set(xlabel="Days from first positive fire-forcing day", ylabel="Available uniquely owned pixels")
            axes[0].legend(frameon=False, fontsize=9)
            fig.supxlabel("Area-weighted modelled responses. Repeated positive detections are not necessarily independent fire events. "
                           "Changing lag support and number of contributing regions are retained in source tables.", fontsize=8)
            _save_figure(fig, out / "figures" / "science" / f"recovery_{slug(variable)}", pdf, figures,
                         {"kind": "fire_aligned_recovery", "variable": str(variable), "source": "recovery_summary.csv"})

    stock_rows = []
    for directory in region_dirs:
        path = directory / "spatial.npz"
        if not path.is_file():
            continue
        with np.load(path, allow_pickle=False) as data:
            if "owned_area_m2" not in data:
                continue
            for component in ("cVeg", "cEcosystem", "cLitter", "cSoil"):
                keys = [f"cEco__{component}__{when}__{s}" for when in ("initial_saved", "final_saved") for s in SCENARIOS]
                if not all(k in data for k in keys):
                    continue
                arrays = [np.asarray(data[k], float) for k in keys]
                area = np.asarray(data["owned_area_m2"], float)
                valid = (area > 0) & np.logical_and.reduce([np.isfinite(x) for x in arrays])
                if "run_valid_mask" in data:
                    valid &= np.asarray(data["run_valid_mask"], bool)
                if not valid.any():
                    continue
                values = [float(np.average(x[valid], weights=area[valid])) for x in arrays]
                stock_rows.append(dict(region_id=directory.name, component=component,
                                       initial_No_Fire=values[0], initial_VIIRS_Fire=values[1],
                                       final_No_Fire=values[2], final_VIIRS_Fire=values[3],
                                       final_difference=values[3] - values[2],
                                       difference_of_changes=(values[3] - values[1]) - (values[2] - values[0]),
                                       area_m2=float(area[valid].sum())))
    _write_rows(out / "stock_endpoints.csv", stock_rows)
    if stock_rows:
        frame = pd.DataFrame(stock_rows)
        fig, axes = plt.subplots(1, 2, figsize=(11.7, 5), layout="constrained")
        for index, (component, data) in enumerate(frame.groupby("component", sort=False)):
            axes[0].scatter(np.full(len(data), index), data["final_difference"], color=COLORS["VIIRS_Fire"], s=12, alpha=.5)
            axes[1].scatter(np.full(len(data), index), data["difference_of_changes"], color=COLORS["No_Fire"], s=12, alpha=.5)
        labels = list(frame["component"].unique())
        for ax, title in zip(axes, ("Final saved stock: fire − no fire", "Difference in saved-period stock changes")):
            ax.axhline(0, color="#747B81", ls="--", lw=.8)
            ax.set(xticks=np.arange(len(labels)), xticklabels=labels, ylabel="Carbon (g C m−2)", title=title)
        fig.supxlabel("Each point is a region's common owned-area mean. Start is the first saved forward state, not restart.nc. "
                       "No stock is integrated over time or relabelled as emissions.", fontsize=8)
        _save_figure(fig, out / "figures" / "science" / "stock_endpoints", pdf, figures,
                     {"kind": "stock_endpoints", "source": "stock_endpoints.csv"})

    environment = []
    for directory in region_dirs:
        path = directory / "environment.csv"
        if path.is_file():
            env = pd.read_csv(path)
            if {"region_id", "year", "variable", "group_type", "group", "mean", "units"}.issubset(env):
                environment.append(env)
    if environment:
        env = pd.concat(environment, ignore_index=True)
        env = env[env["group_type"].eq("overall")].copy()
        initial = {row["region_id"]: row["initial_No_Fire"] for row in stock_rows if row["component"] == "cVeg"}
        for variable, component in (("gpp", "gpp"), ("evapotranspiration", "evapotranspiration"), ("cEco", "cVeg")):
            src = annual[annual["variable"].eq(variable) & annual["component"].eq(component) & annual["group_type"].eq("overall")]
            paired = _paired(src)
            if paired.empty:
                continue
            fig, axes = plt.subplots(1, 3, figsize=(14, 4.9), layout="constrained")
            source_rows = []
            for ax, predictor in zip(axes, ("f_VPD_day", "f_rain", "initial_cVeg")):
                if predictor == "initial_cVeg":
                    joined = paired.copy()
                    joined["predictor"] = joined["region_id"].map(initial)
                    xunit = "g C m-2"
                else:
                    temp = env[env["variable"].eq(predictor)][["region_id", "year", "mean", "units"]].rename(columns={"mean": "predictor"})
                    joined = paired.merge(temp, on=["region_id", "year"], validate="one_to_one")
                    xunit = str(temp["units"].iloc[0]) if not temp.empty else "unknown"
                good = np.isfinite(joined["predictor"]) & np.isfinite(joined["difference"])
                joined = joined[good].copy()
                if not joined.empty:
                    ax.scatter(joined["predictor"], joined["difference"], c=joined["year"], cmap="cividis", s=13, alpha=.65, linewidths=0)
                    joined["predictor_name"] = predictor
                    source_rows.extend(joined.to_dict("records"))
                else:
                    ax.text(.5, .5, "No matching context", transform=ax.transAxes, ha="center")
                ax.axhline(0, color="#747B81", ls="--", lw=.8)
                ax.set(xlabel=f"{predictor} ({xunit})", ylabel=f"Fire − no fire ({str(src['units'].iloc[0])})")
            fig.suptitle(f"{variable} / {component}: environmental dependence of modelled response", fontsize=14)
            fig.supxlabel("Points are complete region-years; colour denotes year. No regression or causal effect is asserted. "
                           "Fire exposure, region composition and repeated years can explain associations.", fontsize=8)
            name = f"environment_response_{slug(variable+'__'+component)}"
            _write_rows(out / f"{name}.csv", source_rows)
            _save_figure(fig, out / "figures" / "science" / name, pdf, figures,
                         {"kind": "environment_response", "variable": variable, "component": component, "source": name + ".csv"})


def _deduplicated_reference_chunks(paths, out, accepted_region_ids=None):
    """One best-covered pair per native reference cell/period, with audit trail.

    A local SQLite cache bounds memory and records every rejected overlapping
    cube pair.  Source simulations and the original pair tables remain read-only.
    """
    import numpy as np
    import pandas as pd
    import sqlite3
    database = out / "reference_pair_dedup.sqlite"
    temporary = database.with_suffix(".sqlite.tmp")
    if temporary.exists():
        temporary.unlink()  # This exact report-owned incomplete staging file only.
    connection = sqlite3.connect(temporary)
    connection.execute("CREATE TABLE best (pair_key TEXT PRIMARY KEY, row_id INTEGER, region_id TEXT, coverage REAL, payload TEXT)")
    connection.execute("CREATE TABLE candidates (row_id INTEGER PRIMARY KEY, pair_key TEXT, region_id TEXT, coverage REAL, source TEXT)")
    row_id = 0
    required = {"region_id", "scenario", "variable", "model", "reference", "accepted_90", "product",
                "reference_cell_id", "period_start", "period_end", "coverage"}
    try:
        for path in paths:
            for frame in pd.read_csv(path, chunksize=50_000):
                if not required.issubset(frame):
                    raise ValueError(f"Reference pair schema mismatch: {path}")
                good = frame["accepted_90"].astype(str).str.lower().isin(("true", "1", "1.0"))
                good &= np.isfinite(frame["model"]) & np.isfinite(frame["reference"])
                if accepted_region_ids is not None:
                    good &= frame["region_id"].isin(accepted_region_ids)
                frame = frame[good].copy()
                if frame.empty:
                    continue
                if "carbon_fraction" not in frame:
                    frame["carbon_fraction"] = "not_applicable"
                frame["carbon_fraction"] = frame["carbon_fraction"].fillna("not_applicable").astype(str)
                pending_best, pending_candidates = [], []
                # JSON handles pandas/NumPy values and nulls without custom adapters.
                records = json.loads(frame.to_json(orient="records", double_precision=15))
                for row in records:
                    row_id += 1
                    pair_key = json.dumps([row[k] for k in ("product", "variable", "scenario", "reference_cell_id",
                                                           "period_start", "period_end", "carbon_fraction")], separators=(",", ":"))
                    region, coverage = str(row["region_id"]), float(row["coverage"])
                    pending_best.append((pair_key, row_id, region, coverage, json.dumps(row, separators=(",", ":"))))
                    pending_candidates.append((row_id, pair_key, region, coverage, str(path)))
                connection.executemany("INSERT INTO candidates VALUES (?,?,?,?,?)", pending_candidates)
                connection.executemany("""INSERT INTO best VALUES (?,?,?,?,?)
                    ON CONFLICT(pair_key) DO UPDATE SET row_id=excluded.row_id, region_id=excluded.region_id,
                      coverage=excluded.coverage, payload=excluded.payload
                    WHERE excluded.coverage > best.coverage
                       OR (excluded.coverage = best.coverage AND excluded.region_id < best.region_id)""", pending_best)
                connection.commit()
        audit = out / "reference_overlap_omissions.csv"
        with audit.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["pair_key", "omitted_region", "omitted_coverage", "retained_region", "retained_coverage", "source", "reason"])
            for row in connection.execute("""SELECT c.pair_key,c.region_id,c.coverage,b.region_id,b.coverage,c.source
                        FROM candidates c JOIN best b ON c.pair_key=b.pair_key WHERE c.row_id != b.row_id ORDER BY c.row_id"""):
                writer.writerow([*row, "Same native reference cell/period; retain highest coverage, then stable region ID"])
        connection.commit()
        cursor = connection.execute("SELECT payload FROM best ORDER BY pair_key")
        while True:
            batch = cursor.fetchmany(50_000)
            if not batch:
                break
            yield pd.DataFrame([json.loads(row[0]) for row in batch])
    finally:
        connection.close()
    temporary.replace(database)


def _reference_figures(root, out, pdf, figures, accepted_region_ids=None):
    """Streaming exact pair moments and bounded reproducible display samples."""
    import numpy as np
    import pandas as pd
    plt = _set_style()
    paths = sorted(set(root.glob("references/**/reference_pairs.csv.gz")) |
                   set(root.glob("regions/*/references/reference_pairs.csv.gz")))
    if not paths:
        return []
    rng = np.random.default_rng(42)
    samples, moments = {}, {}
    max_display = 100_000
    for frame in _deduplicated_reference_chunks(paths, out, accepted_region_ids):
            frame = frame.copy()
            if frame.empty:
                continue
            if "carbon_fraction" not in frame:
                frame["carbon_fraction"] = "not_applicable"
            frame["carbon_fraction"] = frame["carbon_fraction"].fillna("not_applicable").astype(str)
            for key, sub in frame.groupby(["product", "variable", "scenario", "carbon_fraction"], dropna=False):
                x, y = sub["reference"].to_numpy(float), sub["model"].to_numpy(float)
                d = y - x
                m = moments.setdefault(key, dict(n=0, sx=0., sy=0., sxx=0., syy=0., sxy=0.,
                                                sd=0., sad=0., sdd=0., units=str(sub["units"].iloc[0]), sources=set()))
                m["n"] += len(x)
                for field, value in (("sx", x.sum()), ("sy", y.sum()), ("sxx", (x*x).sum()), ("syy", (y*y).sum()),
                                     ("sxy", (x*y).sum()), ("sd", d.sum()), ("sad", np.abs(d).sum()), ("sdd", (d*d).sum())):
                    m[field] += float(value)
                m["sources"].add("reference_pair_dedup.sqlite")
                sub = sub.copy()
                sub["display_priority"] = rng.random(len(sub))
                candidates = pd.concat([samples[key], sub], ignore_index=True) if key in samples else sub
                samples[key] = candidates.nsmallest(max_display, "display_priority") if len(candidates) > max_display else candidates
    rows = []
    for key, m in moments.items():
        product, variable, scenario, carbon_fraction = map(str, key)
        n = m["n"]
        vx, vy = max(0., m["sxx"] - m["sx"] ** 2 / n), max(0., m["syy"] - m["sy"] ** 2 / n)
        r = (m["sxy"] - m["sx"] * m["sy"] / n) / math.sqrt(vx * vy) if vx > 0 and vy > 0 else None
        if r is not None:
            r = min(1., max(-1., r))
        row = dict(reference_product=product, variable=variable, scenario=scenario, carbon_fraction=carbon_fraction,
                   n=n, bias=m["sd"] / n, mae=m["sad"] / n, rmse=math.sqrt(m["sdd"] / n),
                   r=r, r2=r*r if r is not None else None, nse=1 - m["sdd"] / vx if vx > 0 else None,
                   units=m["units"], coverage_threshold=.9,
                   grain="unique native reference cell / matched period / scenario; highest coverage retained", n_display=len(samples[key]))
        rows.append(row)
        data = samples[key]
        token = slug("__".join(map(str, key)))
        data.to_csv(out / f"reference_plot_values_{token}.csv.gz", index=False, compression="gzip")
        fig, axes = plt.subplots(1, 2, figsize=(11.7, 5.2), layout="constrained")
        x, y = data["reference"].to_numpy(float), data["model"].to_numpy(float)
        if len(data) > 1500:
            artist = axes[0].hexbin(x, y, gridsize=45, cmap="cividis", bins="log", mincnt=1, linewidths=0, rasterized=True)
            fig.colorbar(artist, ax=axes[0], label="Display pair count (log scale)")
        else:
            axes[0].scatter(x, y, color=COLORS[scenario], s=10, alpha=.4, linewidths=0, rasterized=True)
        lo, hi = min(x.min(), y.min()), max(x.max(), y.max())
        if lo == hi:
            lo, hi = lo - 1, hi + 1
        axes[0].plot([lo, hi], [lo, hi], "--", color="#5F666C", lw=.9)
        axes[0].set(xlabel=f"{product} ({m['units']})", ylabel=f"{scenario} ({m['units']})", title="Native reference-scale pairs")
        axes[0].set_aspect("equal", adjustable="box")
        delta = y - x
        if len(delta) > 2 and np.ptp(delta) > 0:
            violin = axes[1].violinplot(delta, vert=False, showmedians=True)
            for body in violin["bodies"]:
                body.set_facecolor(COLORS[scenario]); body.set_alpha(.3)
        else:
            axes[1].scatter(delta, np.ones(len(delta)), color=COLORS[scenario], s=15)
        axes[1].boxplot(delta, vert=False, positions=[1], widths=.15, showfliers=False)
        axes[1].set(xlabel=f"Model − reference ({m['units']})", yticks=[], title="Paired difference distribution")
        axes[1].axvline(0, color="#747B81", ls="--", lw=.8)
        rval = "undefined" if r is None else f"{r:.3f}"
        fig.suptitle(f"{variable} | {product} | {scenario}\n{n:,} accepted pairs · bias {row['bias']:.3g} · RMSE {row['rmse']:.3g} · r {rval}", fontsize=13)
        sample_note = f"Uniform deterministic display sample: {len(data):,}/{n:,}; exact descriptive metrics use all accepted rows." if len(data) < n else "All accepted pair rows displayed."
        fig.supxlabel("At least 90% spatial support; reference products are estimates, not truth. " + sample_note +
                       " Spatial/temporal dependence remains after overlap deduplication.", fontsize=8)
        _save_figure(fig, out / "figures" / "references" / token, pdf, figures,
                     {"kind": "native_reference_comparison", "variable": variable, "reference_product": product,
                      "scenario": scenario, "source": f"reference_plot_values_{token}.csv.gz"})
    _write_rows(out / "reference_metrics_all_pairs.csv", rows)
    _reference_group_figures(root, out, pdf, figures)
    _common_gpp_figure(root, out, pdf, figures, accepted_region_ids)
    return rows


def _model_grid_reference_figures(root, out, pdf, figures, accepted_region_ids):
    """Display archived reference values on model centres, never as fine-grid pairs."""
    import numpy as np
    import pandas as pd
    plt = _set_style()
    atlas, audit, source_cells = defaultdict(list), [], []
    for rid in sorted(accepted_region_ids)[:3]:
        candidates = (root / "regions" / rid / "references", root / "references" / rid)
        directory = next((p for p in candidates if (p / "model_grid_reference_display.npz").is_file()), None)
        if directory is None:
            audit.append(dict(region_id=rid, status="missing_model_grid_display"))
            continue
        source = directory / "model_grid_reference_display.npz"
        metadata = read_json(directory / "model_grid_reference_display.json", [])
        # Period and resolution belong to this product's actual archived support.
        support = {}
        pair_path = directory / "reference_pairs.csv.gz"
        if pair_path.is_file():
            columns = ["product", "variable", "scenario", "carbon_fraction", "period_start", "period_end",
                       "reference_lat_min", "reference_lat_max", "reference_lon_min", "reference_lon_max"]
            for chunk in pd.read_csv(pair_path, usecols=columns, chunksize=50_000):
                chunk["carbon_fraction"] = chunk["carbon_fraction"].fillna("not_applicable").astype(str)
                for key, rows in chunk.groupby(["product", "variable", "scenario", "carbon_fraction"], dropna=False):
                    item = support.setdefault(tuple(map(str, key)), dict(start="9999", end="0000", lat_step=[], lon_step=[]))
                    item["start"] = min(item["start"], str(rows["period_start"].min()))
                    item["end"] = max(item["end"], str(rows["period_end"].max()))
                    item["lat_step"].extend(np.unique(np.round(rows["reference_lat_max"] - rows["reference_lat_min"], 9)).tolist())
                    item["lon_step"].extend(np.unique(np.round(rows["reference_lon_max"] - rows["reference_lon_min"], 9)).tolist())
        with np.load(source, allow_pickle=False) as data:
            lat, lon = np.asarray(data["lat"], float), np.asarray(data["lon"], float)
            runmask = np.asarray(data["run_valid_mask"], bool)
            seen = {}
            ordered = sorted(metadata, key=lambda m: (m["product"], m["variable"], str(m["carbon_fraction"]), m["scenario"] != "No_Fire", m["scenario"]))
            for item in ordered:
                name = item["array"]
                raw = np.asarray(data[name], float)
                ids = np.asarray(data[name + "_source_cell_id"], np.int64)
                if raw.shape != (len(lat), len(lon)) or ids.shape != raw.shape or runmask.shape != raw.shape:
                    raise ValueError("Model-grid reference display geometry mismatch: " + rid)
                fraction = str(item.get("carbon_fraction", "")).strip()
                if fraction in ("", "None", "nan"):
                    fraction = "not_applicable"
                key = (str(item["product"]), str(item["variable"]), fraction)
                previous = seen.get(key)
                if previous is not None and np.array_equal(previous[0], raw, equal_nan=True) and np.array_equal(previous[1], ids):
                    audit.append(dict(region_id=rid, product=key[0], variable=key[1], carbon_fraction=key[2],
                                      scenario=item["scenario"], status="identical_reference_map_shown_once", source=str(source)))
                    continue
                seen[key] = (raw, ids)
                valid = runmask & np.isfinite(raw) & (ids >= 0)
                if not valid.any():
                    audit.append(dict(region_id=rid, product=key[0], variable=key[1], scenario=item["scenario"], status="no_finite_display_values"))
                    continue
                unique_ids = np.unique(ids[valid])
                index = np.full(raw.shape, np.nan)
                index[valid] = np.searchsorted(unique_ids, ids[valid]) + 1
                for number, cell_id in enumerate(unique_ids, 1):
                    source_cells.append(dict(region_id=rid, product=key[0], variable=key[1], carbon_fraction=key[2],
                                             scenario=item["scenario"], display_index=number, source_cell_id=int(cell_id)))
                info = support.get((*key[:2], str(item["scenario"]), key[2]), {})
                a = sorted(set(info.get("lat_step", []))); b = sorted(set(info.get("lon_step", [])))
                resolution = f"{a[0]:g}° × {b[0]:g}°" if len(a) == len(b) == 1 else "see source support table"
                scenario_note = "shared reference" if previous is None else str(item["scenario"]) + " distinct reference support"
                group_key = key if previous is None else (*key, str(item["scenario"]))
                atlas[group_key].append(dict(region_id=rid, lat=lat, lon=lon, values=np.where(valid, raw, np.nan),
                                            indices=index, source_count=len(unique_ids), units=item["units"], resolution=resolution,
                                            period_start=info.get("start", "not recorded"), period_end=info.get("end", "not recorded"),
                                            scenario=scenario_note, source=str(source), array=name))
                audit.append(dict(region_id=rid, product=key[0], variable=key[1], carbon_fraction=key[2], scenario=item["scenario"],
                                  status="displayed_only", array=name, source=str(source), source_cells=len(unique_ids),
                                  source_grid_resolution=resolution, period_start=info.get("start"), period_end=info.get("end"),
                                  interpretation="Reference means over available archived periods; no fine-resolution model pairing or metrics"))
    _write_rows(out / "model_grid_reference_display_audit.csv", audit)
    _write_rows(out / "model_grid_reference_source_cell_ids.csv", source_cells)
    for key, records in atlas.items():
        finite = np.concatenate([r["values"][np.isfinite(r["values"])] for r in records])
        low, high = float(finite.min()), float(finite.max())
        if low == high:
            high = low + max(1., abs(low)*.01)
        fig, axes = plt.subplots(len(records), 2, figsize=(11.7, 3.7*len(records)+1.4), squeeze=False, layout="constrained")
        for row, record in enumerate(records):
            ax, idax = axes[row]
            artist = ax.pcolormesh(record["lon"], record["lat"], record["values"], shading="auto", cmap="cividis", vmin=low, vmax=high, rasterized=True)
            fig.colorbar(artist, ax=ax, shrink=.75, label=record["units"])
            idartist = idax.pcolormesh(record["lon"], record["lat"], record["indices"], shading="auto", cmap="tab20", rasterized=True)
            fig.colorbar(idartist, ax=idax, shrink=.75, label="Categorical source-cell index; actual IDs in CSV")
            ax.set_title(f"Region {row+1}: reference mean on model centres\n{record['period_start']} to {record['period_end']}", fontsize=10)
            idax.set_title(f"Region {row+1}: {record['source_count']} original reference cells\nSource comparison grid: {record['resolution']}", fontsize=10)
            for axis in (ax, idax):
                axis.set(xlabel="Longitude (°E)", ylabel="Latitude (°N)")
                axis.set_aspect(1./max(.1, math.cos(math.radians(float(record["lat"].mean())))))
        cf = "" if key[2] == "not_applicable" else f" · carbon fraction {key[2]}"
        fig.suptitle(f"{key[1]} | {key[0]}{cf}\nReference-only model-grid display — no added spatial information", fontsize=13)
        text = ("Reference pixels are copied to model centres, without interpolation or additional observations. All finite available reference periods are shown; "
                "the 90% coverage gate applies to separate formal comparisons, not this display. No comparison to unmatched model ten-year means. Regions: "
                + "; ".join(r["region_id"] for r in records))
        fig.supxlabel(textwrap.fill(text, 145), fontsize=7)
        _save_figure(fig, out / "figures" / "model_grid_reference_display" / slug("__".join(key)), pdf, figures,
                     dict(kind="model_grid_reference_display", variable=key[1], reference_product=key[0], carbon_fraction=key[2],
                          source="model_grid_reference_display_audit.csv", source_cell_ids="model_grid_reference_source_cell_ids.csv",
                          region_ids=[r["region_id"] for r in records], metrics_generated=False))


def _common_gpp_figure(root, out, pdf, figures, accepted_region_ids=None):
    import numpy as np
    import pandas as pd
    paths = sorted(set(root.glob("references/**/annual_reference_pairs.csv.gz")) |
                   set(root.glob("regions/*/references/annual_reference_pairs.csv.gz")))
    if not paths:
        return
    from .references import common_annual_gpp_pairs
    frame = pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)
    if accepted_region_ids is not None:
        frame = frame[frame["region_id"].isin(accepted_region_ids)]
    pairs = common_annual_gpp_pairs(frame.to_dict("records"))
    if not pairs:
        atomic_json(out / "common_annual_gpp_status.json", dict(status="unavailable", reason="Both products have no shared accepted native cells and complete years in2015–2021"))
        return
    data = pd.DataFrame(pairs)
    data.to_csv(out / "common_annual_gpp_pairs.csv.gz", index=False, compression="gzip")
    metrics = []
    plt = _set_style()
    products = sorted(data["product"].unique())
    scenarios = [s for s in SCENARIOS if s in set(data["scenario"])]
    fig, axes = plt.subplots(len(scenarios), len(products), figsize=(6 * len(products), 4.6 * len(scenarios)), squeeze=False, layout="constrained")
    for i, scenario in enumerate(scenarios):
        for j, product in enumerate(products):
            sub = data[data["scenario"].eq(scenario) & data["product"].eq(product)]
            x, y = sub["reference"].to_numpy(float), sub["model"].to_numpy(float)
            m = basic_metrics(x, y)
            metrics.append(dict(product=product, scenario=scenario, years="complete common years within2015–2021", **m))
            if len(x) > 1500:
                axes[i, j].hexbin(x, y, gridsize=35, mincnt=1, bins="log", cmap="cividis", linewidths=0, rasterized=True)
            else:
                axes[i, j].scatter(x, y, s=10, alpha=.5, color=COLORS[scenario], linewidths=0)
            low, high = min(float(x.min()), float(y.min())), max(float(x.max()), float(y.max()))
            if low == high:
                low, high = low-1, high+1
            axes[i, j].plot([low, high], [low, high], color="#747B81", ls="--", lw=.8)
            axes[i, j].set(title=f"{product} | {scenario}\nn={m['n']:,}, RMSE={m['rmse']:.3g}",
                           xlabel="Reference annual GPP (g C m−2 year−1)", ylabel="Model annual GPP (g C m−2 year−1)")
            axes[i, j].set_aspect("equal", adjustable="box")
    fig.suptitle("GPP agreement with multiple products on identical cells and years", fontsize=15)
    fig.supxlabel("Exact shared0.05° cell centres, scenario and complete annual support; native overlap pairs deduplicated bycoverage. "
                   "Reference disagreement is distinct from model error; shared training/data lineage limits independence.", fontsize=8)
    _write_rows(out / "common_annual_gpp_metrics.csv", metrics)
    _save_figure(fig, out / "figures" / "references" / "common_annual_gpp", pdf, figures,
                 dict(kind="native_reference_comparison", variable="gpp", source="common_annual_gpp_pairs.csv.gz"))


def _reference_group_figures(root, out, pdf, figures):
    """Area-fraction group diagnostics, never dominant-cube relabelling."""
    import numpy as np
    import pandas as pd
    import sqlite3
    plt = _set_style()
    files = sorted(set(root.glob("references/**/reference_group_weights.csv.gz")) |
                   set(root.glob("regions/*/references/reference_group_weights.csv.gz")))
    database = out / "reference_pair_dedup.sqlite"
    if not files or not database.is_file():
        return
    weights = pd.concat([pd.read_csv(p, dtype={"reference_cell_id": str}) for p in files], ignore_index=True)
    weights = weights[weights["group_type"].isin(("pyrome", "vegetation")) & (weights["group_area_m2"] > 0)].copy()
    if weights.empty:
        return
    weights["carbon_fraction"] = weights["carbon_fraction"].fillna("not_applicable").astype(str)
    join = ["region_id", "product", "variable", "reference_cell_id", "carbon_fraction"]
    identity = join + ["group_type", "group"]
    if weights.duplicated(identity).any():
        duplicate = weights[weights.duplicated(identity, keep=False)]
        if duplicate.groupby(identity, dropna=False)["group_area_m2"].nunique().max() > 1:
            raise ValueError("Conflicting reference group-area identities")
        weights = weights.drop_duplicates(identity)
    moments, displays = {}, {}
    rng = np.random.default_rng(42)
    with sqlite3.connect(database) as connection:
        cursor = connection.execute("SELECT payload FROM best ORDER BY pair_key")
        while True:
            records = cursor.fetchmany(25_000)
            if not records:
                break
            frame = pd.DataFrame([json.loads(row[0]) for row in records])
            frame["reference_cell_id"] = frame["reference_cell_id"].astype(str)
            frame["carbon_fraction"] = frame["carbon_fraction"].fillna("not_applicable").astype(str)
            # Original pair aliases describe overall support; keep the area-table classes.
            frame = frame.drop(columns=[k for k in ("group_type", "group") if k in frame])
            merged = frame.merge(weights[join + ["group_type", "group", "group_area_m2"]], on=join, validate="many_to_many")
            keys = ["product", "variable", "scenario", "carbon_fraction", "group_type", "group"]
            for key, sub in merged.groupby(keys, dropna=False):
                x, y, w = sub["reference"].to_numpy(float), sub["model"].to_numpy(float), sub["group_area_m2"].to_numpy(float)
                m = moments.setdefault(key, dict(n=0, w=0., wx=0., wy=0., wxx=0., wyy=0., wxy=0., wd=0., wdd=0., units=str(sub["units"].iloc[0])))
                m["n"] += len(x)
                for name, arr in (("w", w), ("wx", w*x), ("wy", w*y), ("wxx", w*x*x), ("wyy", w*y*y), ("wxy", w*x*y), ("wd", w*(y-x)), ("wdd", w*(y-x)**2)):
                    m[name] += float(arr.sum())
                sub = sub.copy(); sub["display_priority"] = rng.random(len(sub))
                pooled = pd.concat([displays[key], sub], ignore_index=True) if key in displays else sub
                displays[key] = pooled.nsmallest(10_000, "display_priority") if len(pooled) > 10_000 else pooled
    metrics = []
    for key, m in moments.items():
        product, variable, scenario, carbon_fraction, family, group = map(str, key)
        w = m["w"]
        vx, vy = max(0., m["wxx"] - m["wx"]**2 / w), max(0., m["wyy"] - m["wy"]**2 / w)
        r = (m["wxy"] - m["wx"]*m["wy"] / w) / math.sqrt(vx*vy) if vx > 0 and vy > 0 else None
        metric = dict(product=product, variable=variable, scenario=scenario, carbon_fraction=carbon_fraction,
                      group_type=family, group=group, n=m["n"], bias=m["wd"]/w, rmse=math.sqrt(m["wdd"]/w),
                      r=r, units=m["units"], semantics="Native reference-cell agreement weighted by original-class overlap; mixed values are not class-specific observations")
        metrics.append(metric)
        sample = displays[key]
        token = slug("__".join(map(str, key)))
        sample.to_csv(out / f"reference_group_plot_{token}.csv.gz", index=False, compression="gzip")
        fig, axes = plt.subplots(1, 2, figsize=(11.7, 5.3), layout="constrained")
        x, y = sample["reference"].to_numpy(float), sample["model"].to_numpy(float)
        if len(x) > 1500:
            art = axes[0].hexbin(x, y, gridsize=35, bins="log", mincnt=1, cmap="cividis", linewidths=0, rasterized=True)
            fig.colorbar(art, ax=axes[0], label="Displayed pair count (log scale)")
        else:
            axes[0].scatter(x, y, s=9, color=COLORS[scenario], alpha=.5)
        low, high = float(min(x.min(), y.min())), float(max(x.max(), y.max()))
        if low == high:
            low, high = low-1, high+1
        axes[0].plot([low, high], [low, high], color="#747B81", ls="--", lw=.8)
        axes[0].set(xlabel=f"{product} ({m['units']})", ylabel=f"{scenario} ({m['units']})")
        axes[0].set_aspect("equal", adjustable="box")
        delta = y-x
        if len(delta) > 2 and np.ptp(delta) > 0:
            violin = axes[1].violinplot(delta, vert=False, showmedians=True)
            for body in violin["bodies"]:
                body.set_facecolor(COLORS[scenario]); body.set_alpha(.3)
        else:
            axes[1].scatter(delta, np.ones(len(delta)), color=COLORS[scenario], s=15)
        axes[1].boxplot(delta, vert=False, showfliers=False, widths=.15)
        axes[1].axvline(0, color="#747B81", ls="--", lw=.8)
        axes[1].set(xlabel=f"Model − reference ({m['units']})", yticks=[], title="Displayed pair difference distribution")
        fig.suptitle(f"{variable} | {product} | {family}: {group} | {scenario}\n{m['n']:,} unique coarse-cell periods; class-area-weighted RMSE {metric['rmse']:.3g}", fontsize=13)
        fig.supxlabel("Group membership uses original spatial area fractions. Mixed coarse-reference values are not class-specific observations. "
                       f"Uniform display sample {len(sample):,}; metrics use all area-weighted pairs. No independent-pixel confidence interval.", fontsize=8)
        _save_figure(fig, out / "figures" / "reference_groups" / family / slug(group) / token, pdf, figures,
                     dict(kind="group_reference_comparison", variable=variable, component=variable, reference_product=product,
                          group_type=family, group=group, source=f"reference_group_plot_{token}.csv.gz"))
    _write_rows(out / "reference_group_metrics_pooled.csv", metrics)


def _text_page(pdf, title: str, paragraphs: list[str]):
    plt = _set_style()
    lines = []
    for paragraph in paragraphs:
        lines.extend(textwrap.wrap(str(paragraph), width=105, break_long_words=False) or [""])
        lines.append("")
    for start in range(0, max(1, len(lines)), 46):
        fig = plt.figure(figsize=(11.7, 8.3))
        fig.text(.07, .94, title, fontsize=18, fontweight="bold", va="top")
        fig.text(.07, .86, "\n".join(lines[start:start + 46]), fontsize=10, va="top", linespacing=1.35)
        fig.text(.07, .035, "SINDBAD–WISP | Offline analysis | Evidence and caveats retained", fontsize=8, color="#52606B")
        pdf.savefig(fig)
        plt.close(fig)


def _save_figure(fig, destination: Path, pdf, records, metadata):
    import matplotlib.pyplot as plt
    destination.parent.mkdir(parents=True, exist_ok=True)
    for suffix in (".png", ".pdf"):
        # Product identifiers contain dots (e.g. MOD17A2HGF.061). They are
        # part of the complete figure identity, not a filename extension.
        final = Path(str(destination) + suffix)
        temporary = Path(str(destination) + suffix + ".partial")
        fig.savefig(temporary, format=suffix[1:], dpi=170, bbox_inches="tight")
        temporary.replace(final)
    pdf.savefig(fig)
    records.append(dict(metadata, png=str(destination) + ".png", pdf=str(destination) + ".pdf"))
    plt.close(fig)


def generate(root: str | Path, snapshot: Any = None, max_components: int | None = None,
             include_group_figures: bool = True) -> dict[str, Any]:
    """Generate an offline, reproducible report; partial completion stays explicit.

    ``max_components`` is a disclosed smoke-test option, never silently a final
    report.  Existing simulation/source files remain read-only.
    """
    root = Path(root)
    out = root / "report"
    out.mkdir(parents=True, exist_ok=True)
    regions = completed_regions(root, snapshot)
    qa, qa_counts = qa_summary(regions)
    _write_rows(out / "qa_checks.csv", qa)
    references = reference_status(root)
    inventory = read_json(root / "inventory.json", {})
    expected = inventory.get("regions", []) if isinstance(inventory, dict) else []
    quarantined = []
    for directory in regions:
        manifest = read_json(directory / "qa.json", {})
        failures = [c for c in manifest.get("checks", []) if c.get("status") == "fail"]
        if manifest.get("status") not in ("pass", "warning") or failures:
            quarantined.append({"region_id": directory.name, "status": manifest.get("status", "not_checked"),
                                "failed_checks": failures, "reason": "Excluded from primary comparisons; source summaries retained unchanged."})
    quarantine_ids = {r["region_id"] for r in quarantined}
    status = {"created_utc": datetime.now(timezone.utc).isoformat(),
              "completed_regions": len(regions), "snapshot_regions": len(expected),
              "primary_regions": len(regions) - len(quarantined), "quarantined_regions": quarantined,
              "qa_check_counts": qa_counts, "reference_status": references,
              "semantics": list(SEMANTIC_WARNINGS), "smoke_test_max_components": max_components,
              "figures": [], "components": [], "coverage": "interim_completed_subset"}
    status["source_snapshot_fingerprint"] = inventory.get("source_metadata_fingerprint") if isinstance(inventory, dict) else None
    status["analysis_fingerprints"] = {p.name: read_json(p / "completion.json", {}).get("fingerprint") for p in regions}
    status["report_code_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    _write_rows(out / "quarantined_regions.csv", quarantined)
    regions = [directory for directory in regions if directory.name not in quarantine_ids]
    reference_coverage = reference_region_coverage(root, {p.name for p in regions})
    _write_rows(out / "reference_region_coverage.csv", reference_coverage)
    status["reference_attempted_regions"] = sum(row["attempted"] for row in reference_coverage)
    status["reference_pair_file_regions"] = sum(row["pair_file_available"] for row in reference_coverage)
    status["reference_unattempted_region_ids"] = [row["region_id"] for row in reference_coverage if not row["attempted"]]
    if not regions:
        status["status"] = "awaiting_scientifically_accepted_region_analysis"
        message = ("# SINDBAD–WISP analysis: no accepted region summaries yet\n\n"
                   f"{status['completed_regions']} region(s) have processing completion records; none currently passes the primary QA gate. "
                   "No primary scientific comparison or synthetic plots have been generated. "
                   "Failed or unverified values remain in their unchanged source summaries.\n\n"
                   "[QA checks](qa_checks.csv) · [Quarantined regions](quarantined_regions.csv)\n\n"
                   + "\n".join(f"- {x}" for x in SEMANTIC_WARNINGS) + "\n")
        (out / "REPORT_EN.md").write_text(message, encoding="utf-8")
        if status["completed_regions"] and importlib.util.find_spec("matplotlib") is not None:
            from matplotlib.backends.backend_pdf import PdfPages
            with PdfPages(out / "REPORT_EN.pdf") as pdf:
                _text_page(pdf, "SINDBAD–WISP: quality gate pending", [
                    f"Processed regions: {status['completed_regions']}; primary accepted regions: 0.",
                    f"Quality checks: {qa_counts}.",
                    "No primary scientific comparisons are reported. Source arrays and failed evidence are retained.",
                    *[r["region_id"] + ": " + json.dumps(r["failed_checks"]) for r in quarantined]])
        atomic_json(out / "report_manifest.json", status)
        return status

    import pandas as pd
    from matplotlib.backends.backend_pdf import PdfPages
    annual = _load_annual(regions)
    monthly = _monthly_compact(regions)
    monthly.to_csv(out / "monthly_area_weighted_summary.csv", index=False)
    components = list(annual[["variable", "component"]].drop_duplicates().itertuples(index=False, name=None))
    all_components = list(components)
    try:
        from .registry import components as saved_components
        available = {(str(v), str(c)) for v, c in all_components}
        status["saved_component_coverage"] = [dict(variable=item["variable"], component=item["component"],
            status="summarized" if (item["variable"], item["component"]) in available else "not_available",
            reason="" if (item["variable"], item["component"]) in available else "No accepted regional summary present")
            for item in saved_components()]
    except ImportError:
        status["saved_component_coverage"] = []
    if max_components is not None:
        components = components[:max_components]
    figures, comparison_metrics = [], []
    pdf_path = out / "REPORT_EN.pdf.partial"
    with PdfPages(pdf_path) as pdf:
        _text_page(pdf, f"SINDBAD–WISP: {len(regions)}-region interim analysis", [
            f"Source experiment: WISP_V1_202610. Scenarios: No_Fire and VIIRS_Fire. "
            f"Completed compact regional analyses: {len(regions)}. Available components: {len(all_components)}. "
            f"Components rendered in this report: {len(components)}.",
            f"Quality checks by status: {qa_counts}. A finished file inventory is not a scientific validation verdict.",
            "The following figures describe analysed regions and common, valid supports. "
            "No full-design weights are applied to incomplete production results, and no whole-Mediterranean accuracy is claimed.",
            *SEMANTIC_WARNINGS])
        _text_page(pdf, "References and unavailable comparisons", [
            f"Reference status: {references.get('status', 'see manifest')}; "
            f"regional provenance files: {len(references.get('manifests', []))}. Full evidence is in report_manifest.json.",
            "Formal comparison requires verified product units and common space/time support. "
            "Coarse reference values displayed on a model grid remain coarse evidence.",
            "LAI is not saved; its absence is a requested-output limitation, not a plotting failure.",
            "Independent reference pair tables and plots are added only after reference harmonization completes."])
        for variable, component in components:
            frame = annual[annual["variable"].eq(variable) & annual["component"].eq(component)]
            cycle = monthly[monthly["variable"].eq(variable) & monthly["component"].astype(str).eq(str(component))] if not monthly.empty else monthly
            label, token = f"{variable} / {component}", slug(f"{variable}__{component}")
            component_entry = {"variable": str(variable), "component": str(component),
                               "regions": int(frame["region_id"].nunique()), "status": "summarized"}
            paired_all = _paired(frame)
            paired_all.to_csv(out / f"paired_{token}.csv", index=False)
            for family in GROUP_FAMILIES:
                data = frame[frame["group_type"].eq(family)]
                if data.empty:
                    continue
                fig = _component_figure(frame, cycle, label, family)
                if fig is not None:
                    _save_figure(fig, out / "figures" / family / token, pdf, figures,
                                 {"variable": str(variable), "component": str(component), "group_type": family,
                                  "kind": "scenario_summary", "source": f"paired_{token}.csv"})
                pairs = paired_all[paired_all["group_type"].eq(family)] if not paired_all.empty else paired_all
                for group, pairs_g in pairs.groupby("group", observed=True) if not pairs.empty else []:
                    comparison_metrics.append(dict(variable=str(variable), component=str(component), group_type=family,
                                                   group=str(group), grain="region_year_complete",
                                                   **basic_metrics(pairs_g["No_Fire"], pairs_g["VIIRS_Fire"])))
                if include_group_figures and family in ("pyrome", "vegetation"):
                    for group in sorted(set(data["group"].astype(str))):
                        fig = _component_figure(frame, cycle, label, family, group)
                        if fig is not None:
                            _save_figure(fig, out / "figures" / family / slug(group) / token, pdf, figures,
                                         {"variable": str(variable), "component": str(component), "group_type": family,
                                          "group": group, "kind": "group_summary", "source": f"paired_{token}.csv"})
            fig = _combined_figure(frame, label)
            _save_figure(fig, out / "figures" / "combined" / token, pdf, figures,
                         {"variable": str(variable), "component": str(component), "kind": "combined_distribution",
                          "source": f"paired_{token}.csv"})
            fig = _anomaly_figure(frame, label, out, token)
            if fig is not None:
                _save_figure(fig, out / "figures" / "annual_anomalies" / token, pdf, figures,
                             {"variable": str(variable), "component": str(component), "kind": "annual_anomaly",
                              "source": f"annual_anomaly_{token}.csv"})
            native_unit = str(frame["units"].iloc[0])
            if str(frame["aggregation"].iloc[0]) == "sum" and "day" not in native_unit:
                native_unit += " day-1"
            map_rows = _region_maps(regions, variable, component)
            _write_rows(out / f"geography_{token}.csv", map_rows)
            fig = _map_figure(map_rows, label, native_unit)
            if fig is not None:
                _save_figure(fig, out / "figures" / "geography" / token, pdf, figures,
                             {"variable": str(variable), "component": str(component), "kind": "geographic_centres",
                              "source": f"geography_{token}.csv"})
            fig = _pixel_figure(regions, variable, component, label, native_unit, out, token)
            if fig is not None:
                _save_figure(fig, out / "figures" / "pixel_pairs" / token, pdf, figures,
                             {"variable": str(variable), "component": str(component), "kind": "pixel_scenario_comparison",
                              "source": f"pixel_pairs_{token}.csv.gz"})
            fig = _pilot_pixel_maps(regions, variable, component, label, native_unit)
            if fig is not None:
                _write_rows(out / f"pilot_pixel_sources_{token}.csv", [dict(region_id=p.name, source=str(p / "spatial.npz"),
                               variable=variable, component=component) for p in regions[:3]])
                _save_figure(fig, out / "figures" / "pilot_pixel_maps" / token, pdf, figures,
                             {"variable": str(variable), "component": str(component), "kind": "pilot_pixel_maps",
                              "source": f"pilot_pixel_sources_{token}.csv"})
            status["components"].append(component_entry)
        if max_components is None:
            _science_figures(annual, regions, out, pdf, figures)
        reference_metrics = _reference_figures(root, out, pdf, figures, {p.name for p in regions})
        _model_grid_reference_figures(root, out, pdf, figures, {p.name for p in regions})
        status["reference_comparisons"] = len(reference_metrics)
        _text_page(pdf, "Interpretation and next checks", [
            "Component plots are systematic evidence, not automatically discoveries. A scientific finding needs paired support, "
            "a specified mechanism, physical QA, reference provenance and an alternative-explanation check.",
            "Maps show actual region centres and owned-area means. Detailed pixel maps and reference-cell pair plots "
            "are separate products; point area does not depict cube area.",
            "Violin/box distributions summarize region-years, dots show region means. Annual curves are descriptive "
            "area-weighted means across analysed supports. Their spread is not a confidence interval.",
            "Fire-aligned recovery and block/year intervals are included when source summaries exist. Environmental response "
            "models and external reference comparisons remain unavailable unless their own evidence tables are complete. "
            "Reference-product uncertainty, carbon-fraction sensitivity and bootstrap uncertainty are not interchangeable.",
            "See OUTPUT_SEMANTICS_EN.md for budget, area, restart, fAPAR, biomass and legacy CSV caveats."])
    _write_rows(out / "scenario_metrics.csv", comparison_metrics)
    pdf_path.replace(out / "REPORT_EN.pdf")
    status["figures"] = figures
    status["status"] = "smoke_test" if max_components is not None else "interim_report_generated"
    status["component_count_available"] = len(all_components)
    status["component_count_rendered"] = len(components)
    _write_markdown(out, status)
    _write_group_reports(out, status)
    source_doc = Path(__file__).resolve().parents[1] / "docs" / "OUTPUT_SEMANTICS_EN.md"
    if source_doc.is_file():
        (out / source_doc.name).write_text(source_doc.read_text(encoding="utf-8"), encoding="utf-8")
    atomic_json(out / "report_manifest.json", status)
    return status


def _write_markdown(out: Path, status: dict) -> None:
    figures = status["figures"]
    lines = [f"# SINDBAD–WISP: {status['primary_regions']}-region interim analysis", "",
             "## Executive summary", "",
             f"- **{status['completed_regions']} completed regional analyses**, of which **{status['primary_regions']} pass the primary QA gate**; "
             f"{status['component_count_rendered']} of {status['component_count_available']} available components are rendered.",
             f"- Recorded quality-check states: `{status['qa_check_counts']}`. Read failures and warnings before using a result.",
             f"- Reference comparisons have saved attempt evidence for **{status['reference_attempted_regions']} of {status['primary_regions']} primary regions**; "
             f"{status['reference_pair_file_regions']} have pair files. An attempt or pair file does not mean every product is available or accepted.",
             "- These are descriptive results for the completed subset. They are not a design-unbiased estimate for all 400 selected regions or the Mediterranean.",
             "- Reference-product agreement and missing-reference states are distinct from internal scenario comparisons. LAI is unavailable.", "",
             "[Multipage report](REPORT_EN.pdf) · [Scenario metrics](scenario_metrics.csv) · [QA checks](qa_checks.csv) · "
             "[Source and figure manifest](report_manifest.json)", "", "## Scope and interpretation", ""]
    lines.extend(f"- {item}" for item in SEMANTIC_WARNINGS)
    lines += ["", "Failed or unverified regions are explicitly [quarantined](quarantined_regions.csv), not silently removed or corrected. "
              "Their original summaries and failure evidence remain available.", "",
              "## Reference comparison status", "", "The complete product manifests are recorded in [report_manifest.json](report_manifest.json).", "",
              "[Regional reference coverage](reference_region_coverage.csv) records missing attempts separately from product-specific outcomes. "
              "Only verified harmonized pairs can support model–reference statistics. Missing products are not zero values. "
              "Interpolated displays do not create independent 375 m observations.", "", "## Component evidence", "",
              "The main distributions use complete region-years, so the partial 2014 year is excluded from scatter and violin comparisons. "
              "Annual curves retain 2014 and mark it as partial. Flux sums and stock means are labelled explicitly. "
              "Group results use their original classification support. Region dots are means across complete years; boxes/violins summarize region-years, not confidence intervals.", ""]
    for component in status["components"]:
        var, comp = component["variable"], component["component"]
        token = slug(f"{var}__{comp}")
        lines += [f"### {var} / {comp}", "",
                  f"{component['regions']} regions. [Paired plot values](paired_{token}.csv).", ""]
        relevant = [f for f in figures if f.get("variable") == var and f.get("component") == comp]
        for item in relevant:
            png, pdf = Path(item["png"]).relative_to(out), Path(item["pdf"]).relative_to(out)
            caption = item.get("group_type", item["kind"])
            if item.get("group"):
                caption += " / " + item["group"]
            if item["kind"] == "combined_distribution" or item.get("group_type") == "overall":
                lines += [f"![{var}: {caption}]({png.as_posix()})", ""]
            lines += [f"- {caption}: [PNG]({png.as_posix()}) · [PDF]({pdf.as_posix()})"]
        lines.append("")
    lines += ["## Scientific response and reference comparisons", ""]
    for item in figures:
        if item["kind"] in ("block_year_effects", "carbon_flux_decomposition", "fire_aligned_recovery", "stock_endpoints", "native_reference_comparison", "environment_response", "model_grid_reference_display", "common_annual_gpp_comparison"):
            png, pdf = Path(item["png"]).relative_to(out), Path(item["pdf"]).relative_to(out)
            title = item["kind"].replace("_", " ") + " — " + item.get("variable", "")
            lines += [f"### {title}", "", f"![{title}]({png.as_posix()})", "",
                      f"[PDF]({pdf.as_posix()}) · [Source values]({item['source']})", ""]
    lines += ["## Finding status and uncertainty", "",
              "This report does not turn a large number of figures into a claim of discovery. "
              "Mechanistic interpretations require completed carbon-budget checks, reference harmonization and paired common supports. "
              "Spatial-block/year bootstrap intervals, fire-aligned recovery and environmental response models are interpreted only when their upstream result tables exist. "
              "Scenario differences are modelled counterfactual responses, not identified real-world causal effects. "
              "Environmental association plots are descriptive. Predefined response means additionally receive approximate centered two-way bootstrap p-values "
              "only with at least five spatial blocks, three years and nonconstant bootstrap draws. BH-adjusted exploratory q-values cover all valid tests "
              "across seven responses and overall/pyrome/vegetation groups; nonsignificant and undefined tests remain in effects_block_year.csv. "
              "Percentile intervals are not multiplicity-adjusted; neither p nor q establishes real-world causal significance.", "",
              "## Reproducibility", "",
              f"Generated UTC: `{status['created_utc']}`. All source region summaries were selected using atomic completion records. "
              "Input simulation files remain unchanged. Figure source values and status metadata are retained beside this report.", ""]
    temporary = out / "REPORT_EN.md.partial"
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(out / "REPORT_EN.md")


def _write_group_reports(out: Path, status: dict) -> None:
    """Standalone English entry points for each pyrome and vegetation group."""
    import os
    grouped = defaultdict(list)
    for figure in status["figures"]:
        if figure.get("group_type") in ("pyrome", "vegetation") and figure.get("group"):
            grouped[(figure["group_type"], figure["group"])].append(figure)
    for (family, group), figures in grouped.items():
        directory = out / "groups" / family / slug(group)
        directory.mkdir(parents=True, exist_ok=True)
        lines = [f"# SINDBAD–WISP | {family}: {group}", "",
                 "This group report uses the same compact source tables and QA exclusions as the overall report. "
                 "A mixed cube can contribute only its corresponding area to this group. "
                 "Results describe the completed subset, not all Mediterranean land.", "",
                 f"[Overall report]({os.path.relpath(out / 'REPORT_EN.md', directory)})", ""]
        for figure in figures:
            lines += [f"## {figure['variable']} / {figure['component']}", "",
                      f"![Group result]({os.path.relpath(figure['png'], directory)})", "",
                      f"[PDF]({os.path.relpath(figure['pdf'], directory)}) · "
                      f"[Source values]({os.path.relpath(out / figure['source'], directory)})", ""]
        (directory / "REPORT_EN.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--max-components", type=int, help="Disclosed rendering smoke test only")
    parser.add_argument("--no-individual-groups", action="store_true", help="Keep group families and combined panels only")
    args = parser.parse_args(argv)
    result = generate(args.output_root, args.snapshot, args.max_components, not args.no_individual_groups)
    print(json.dumps({k: result[k] for k in ("status", "completed_regions", "qa_check_counts")}, indent=2))


if __name__ == "__main__":
    main()
