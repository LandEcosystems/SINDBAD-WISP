"""Freeze and verify scientific dependencies without mutating source artifacts."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .grouping import DEFAULT_GEOMETRY, DEFAULT_PYROME, get_groups
from .io import atomic_json, canonical_hash, exclusive
from .provenance import inspect_run
from .registry import SCENARIOS, file_identity


def _identities(paths):
    """Small scientific masks/configuration: content; full input NC: metadata."""
    return [file_identity(p, content_hash=Path(p).suffix != ".nc") for p in paths]


def _unchanged(expected):
    for entry in expected:
        current = file_identity(entry["path"], content_hash="sha256" in entry)
        if current != entry:
            raise RuntimeError("Analysis dependency changed: " + entry["path"])


def audit_inventory(config, snapshot=None):
    """Create one immutable dependency audit for the frozen result inventory.

    Run this before production. It reads accepted masks and actual per-run
    metadata, never changes running model jobs or creates source caches.
    Scientific array acceptance is performed separately by the region engine.
    """
    root, batch = Path(config["output_root"]), Path(config["batch_root"])
    snapshot = snapshot or json.loads((root / "inventory.json").read_text())
    destination = root / "dependency_audit.json"
    with exclusive(root / "dependency_audit.lock"):
        if destination.exists():
            previous = json.loads(destination.read_text())
            if previous["inventory_fingerprint"] != snapshot["source_metadata_fingerprint"]:
                raise RuntimeError("Dependency audit belongs to a different immutable inventory")
            return previous
        shared_paths = [DEFAULT_GEOMETRY, DEFAULT_PYROME,
                        batch / "sampling/catalog.json", batch / "sampling/selected_regions_named.csv"]
        shared = _identities(shared_paths)
        records = []
        for j, region in enumerate(snapshot["regions"]):
            rid = region["region_id"]
            qa = batch / "qa" / rid
            cube = batch / "cubes" / (rid + "_frozen_v1") / ("daily_master_dev_v5_" + rid + "_frozen_v1.nc")
            paths = [qa / "ownership.npz", qa / "run_masks.npz", qa / "static/static_inputs.npz", cube]
            record = {"region_id": rid, "dependencies": _identities(paths), "scenarios": {}, "checks": []}
            try:
                groups = get_groups(rid, batch / "qa")
                record["grouping"] = {"owned_natural_area_m2": float(groups["owned_area_m2"].sum()),
                                      "run_cell_area_m2": float(groups["run_area_m2"].sum()),
                                      "run_pixels": int(groups["run_valid_mask"].sum()),
                                      "group_area_m2": {k: float(v.sum()) for k, v in groups["group_area_m2"].items()}}
                record["checks"].append({"check": "ownership_group_conservation", "status": "pass"})
            except (OSError, ValueError, KeyError) as exc:
                record["checks"].append({"check": "ownership_group_conservation", "status": "fail", "error": str(exc)})
            for scenario in SCENARIOS:
                run = Path(config["source_root"]) / rid / ("output_WISP_" + rid + "-full_" + scenario)
                record["scenarios"][scenario] = inspect_run(run)
            a, b = [record["scenarios"][s] for s in SCENARIOS]
            matched = a["status"] == b["status"] == "pass"
            if matched:
                matched = a["actual_parameter_values"]["content_sha256"] == b["actual_parameter_values"]["content_sha256"]
                matched &= a["model_approaches"] == b["model_approaches"]
                matched &= a["data_compression"] == b["data_compression"]
                f0, f1 = json.loads(json.dumps(a["forcing"])), json.loads(json.dumps(b["forcing"]))
                del f0["variables"]["f_burnt_area"]
                del f1["variables"]["f_burnt_area"]
                matched &= f0 == f1
                matched &= all(Path(x["forcing"]["default_forcing"]["data_path"]) == cube for x in (a, b))
            record["checks"].append({"check": "paired_configuration_except_fire_matches", "status": "pass" if matched else "fail"})
            record["status"] = "pass" if matched and all(c["status"] == "pass" for c in record["checks"]) else "fail"
            record["fingerprint"] = canonical_hash(record)
            records.append(record)
            if (j + 1) % 25 == 0:
                print("dependency audit", j + 1, "/", len(snapshot["regions"]), flush=True)
        _unchanged(shared)
        audit_fingerprint = canonical_hash({"inventory": snapshot["source_metadata_fingerprint"], "shared": shared,
                                            "regions": [(r["region_id"], r["fingerprint"]) for r in records]})
        folder = root / "dependency_audits" / audit_fingerprint
        for record in records:
            atomic_json(folder / "regions" / (record["region_id"] + ".json"), record)
        atomic_json(folder / "shared.json", shared)
        summary = {"created_utc": datetime.now(timezone.utc).isoformat(), "audit_fingerprint": audit_fingerprint,
                   "inventory_fingerprint": snapshot["source_metadata_fingerprint"], "folder": str(folder),
                   "regions": len(records), "passed": sum(r["status"] == "pass" for r in records),
                   "failed": [r["region_id"] for r in records if r["status"] != "pass"],
                   "owned_natural_area_m2": sum(r.get("grouping", {}).get("owned_natural_area_m2", 0) for r in records),
                   "run_cell_area_m2": sum(r.get("grouping", {}).get("run_cell_area_m2", 0) for r in records),
                   "scope": "Source dependency identity and effective configuration; not full numerical array acceptance"}
        atomic_json(destination, summary)
        return summary


def validate_dependencies(config, rid):
    """Mandatory pre/post gate for each region; returns supplemental provenance.

    This does not modify region arrays or authorize reuse under a changed code
    identity. The engine's own immutable processing fingerprint remains active.
    """
    root = Path(config["output_root"])
    audit = json.loads((root / "dependency_audit.json").read_text())
    folder = Path(audit["folder"])
    record = json.loads((folder / "regions" / (rid + ".json")).read_text())
    if record["status"] != "pass":
        raise RuntimeError("Effective configuration or grouping dependency audit failed for " + rid)
    _unchanged(json.loads((folder / "shared.json").read_text()))
    _unchanged(record["dependencies"])
    for scenario, expected in record["scenarios"].items():
        current = inspect_run(expected["path"])
        if canonical_hash(current) != canonical_hash(expected):
            raise RuntimeError("Effective run metadata or parameter values changed: " + scenario + "/" + rid)
    return {"dependency_audit_fingerprint": audit["audit_fingerprint"], "region_dependency_fingerprint": record["fingerprint"],
            "region_id": rid, "status": "pass", "evidence_path": str(folder / "regions" / (rid + ".json")),
            "scope": "Current source masks, ownership, effective metadata and parameter values match frozen audit"}


def independent_spotcheck(config, rid, fire_pixel=False):
    """Recompute one fixed owned pixel from source, independently of engine helpers.

    This is a limited arithmetic regression audit, not a replacement for the
    engine's full spatial validation. It checks every saved component's annual
    summary and the ten-year carbon budget for the same physical pixel.
    """
    import netCDF4
    import numpy as np
    from .registry import VARIABLES
    root = Path(config["output_root"])
    groups = get_groups(rid, Path(config["batch_root"]) / "qa")
    cells = np.argwhere(groups["owned_area_m2"] > 0)
    row, col = cells[len(cells) // 2]
    selection = "Middle row-major owned runnable pixel, fixed independently of model outputs"
    checks, failures, budgets = 0, [], []
    compact_path = root / "regions" / rid / "compact.nc"
    with netCDF4.Dataset(compact_path) as compact:
        if fire_pixel:
            fire_total = np.zeros(groups["owned_area_m2"].shape)
            for component in VARIABLES["c_fire_efflux"]["components"]:
                values = np.ma.filled(compact.variables["c_fire_efflux__" + component + "_annual"][1, ...], np.nan)
                fire_total += np.nansum(values, axis=0)
            candidates = np.argwhere((groups["owned_area_m2"] > 0) & (fire_total > 0))
            if not len(candidates):
                return {"region_id": rid, "status": "not_applicable", "reason": "No owned runnable pixel has positive saved VIIRS fire efflux"}
            row, col = candidates[0]
            selection = "First row-major owned runnable pixel with positive saved VIIRS fire efflux; targeted arithmetic QA, not scientific sample selection"
        years = np.asarray(compact.variables["year"][:], int)
        for si, scenario in enumerate(SCENARIOS):
            stored = {}
            for variable, meta in VARIABLES.items():
                path = Path(config["source_root"]) / rid / ("output_WISP_" + rid + "-full_" + scenario) / "data" / (variable + ".nc")
                with netCDF4.Dataset(path) as source:
                    v = source.variables[variable]
                    index = tuple(int(col) if d == "lon" else int(row) if d == "lat" else slice(None) for d in v.dimensions)
                    raw = np.ma.filled(v[index], np.nan)
                    remaining = [d for d in v.dimensions if d not in ("lat", "lon")]
                    raw = np.moveaxis(raw, remaining.index("time"), -1)
                    if raw.ndim == 1:
                        raw = raw[None, :]
                    t = source.variables["time"]
                    dates = netCDF4.num2date(t[:], t.units, getattr(t, "calendar", "standard"))
                    y = np.array([d.year for d in dates])
                if variable in ("cEco", "nee", "c_fire_efflux"):
                    stored[variable] = raw.astype(np.float64)
                for ci, component in enumerate(meta["components"]):
                    values = raw[ci].astype(np.float64)
                    expected = np.array([values[y == yr].sum() if meta["kind"] == "sumflux" else values[y == yr].mean() for yr in years], dtype=np.float32)
                    observed = np.asarray(compact.variables[variable + "__" + component + "_annual"][si, :, row, col])
                    checks += len(years)
                    equal = (expected == observed) | (np.isnan(expected) & np.isnan(observed))
                    if not equal.all():
                        failures.append({"scenario": scenario, "variable": variable, "component": component,
                                         "years": years[~equal].tolist(), "maximum_abs": float(np.nanmax(np.abs(expected - observed)))})
            stock, nee, fire = stored["cEco"], stored["nee"][0], stored["c_fire_efflux"]
            residual = np.diff(stock.sum(axis=0)) + nee[1:] + fire[:, 1:].sum(axis=0)

            def half_ulp(x):
                absolute = np.abs(x)
                exponent = np.floor(np.log2(np.where(absolute > 0, absolute, 1)))
                return np.where(absolute > 0, 2.0 ** (exponent - 15), 0.0)

            bound = (half_ulp(stock[:, 1:]).sum(axis=0) + half_ulp(stock[:, :-1]).sum(axis=0)
                     + half_ulp(nee[1:]) + half_ulp(fire[:, 1:]).sum(axis=0) + config["budget_model_tolerance"])
            budgets.append({"scenario": scenario, "maximum_abs_residual": float(np.abs(residual).max()),
                            "days_checked": len(residual), "days_exceeding_quantization_and_model_bound": int((np.abs(residual) > bound).sum())})
    return {"region_id": rid, "pixel_lat_index": int(row), "pixel_lon_index": int(col),
            "selection": selection,
            "annual_component_values_checked": checks, "annual_mismatches": failures, "carbon_budgets": budgets,
            "status": "pass" if not failures and all(b["days_exceeding_quantization_and_model_bound"] == 0 for b in budgets) else "fail",
            "scope": "One deterministic pixel; annual summaries of all 50 saved components and full-period budget; not full spatial validation"}
