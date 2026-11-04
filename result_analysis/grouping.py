"""Read-only scientific grouping on the frozen area-ownership crosswalk.

All returned spatial arrays have (latitude, longitude) order. The native
NetCDF output often stores (longitude, latitude, component, time): callers
must transpose by dimension names, not infer order from equal axis lengths.
"""
from __future__ import annotations

import csv
import os
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy import sparse

DEFAULT_GEOMETRY = Path(os.environ.get("WISP_ANALYSIS_GEOMETRY", "configured_sources/land_geometry.npz"))
DEFAULT_PYROME = Path(os.environ.get("WISP_ANALYSIS_PYROME", "configured_sources/Pyromes_finalClassification_withUncertainty.csv"))
PYROMES = {0: "unknown", 1: "FIL", 2: "ICS", 3: "RCS", 4: "RIL", 5: "FCS"}
VEGETATION = {"forest": (1, 2, 3, 4, 5), "shrub": (6, 7), "savanna": (8, 9), "grass": (10,), "wetland": (11,)}


@lru_cache(maxsize=2)
def _geometry(path):
    with np.load(path, allow_pickle=False) as data:
        result = {key: data[key] for key in ("lat", "lon", "classes")}
    if result["classes"].shape != (len(result["lat"]), len(result["lon"])):
        raise ValueError("2019 source classification shape disagrees with coordinates")
    return result


@lru_cache(maxsize=2)
def _pyrome_lookup(path):
    """Native half-degree cells; absent official classifications remain unknown."""
    labels = np.zeros((360, 720), dtype=np.uint8)
    uncertainties = np.full((360, 720), np.nan)
    seen = set()
    with open(path, newline="") as stream:
        for row in csv.DictReader(stream):
            lat, lon = float(row["lat"]), float(row["long"])
            iy, ix = int(np.floor((lat + 90) * 2)), int(np.floor((lon + 180) * 2))
            label, uncertainty = int(row["Class5"]), float(row["Uncert"])
            if (iy, ix) in seen or label not in range(1, 6) or not 0 <= uncertainty <= 1:
                raise ValueError("Invalid or duplicated official pyrome record")
            if not (0 <= iy < 360 and 0 <= ix < 720):
                raise ValueError("Pyrome record outside geographic bounds")
            if abs(lat - (-89.75 + 0.5 * iy)) > 1e-7 or abs(lon - (-179.75 + 0.5 * ix)) > 1e-7:
                raise ValueError("Official pyrome coordinates are not half-degree cell centres")
            labels[iy, ix], uncertainties[iy, ix] = label, uncertainty
            seen.add((iy, ix))
    return labels, uncertainties


def classify_source_cells(lat, lon, pyrome_path=DEFAULT_PYROME):
    """Containing native pyrome, not nearest classified neighbour interpolation."""
    lat, lon = np.asarray(lat), np.asarray(lon)
    if lat.shape != lon.shape or np.any(~np.isfinite(lat)) or np.any(~np.isfinite(lon)):
        raise ValueError("Invalid source cell coordinates")
    iy = np.floor((lat + 90) * 2).astype(int)
    ix = np.floor((lon + 180) * 2).astype(int)
    if np.any((iy < 0) | (iy >= 360) | (ix < 0) | (ix >= 720)):
        raise ValueError("Source cell outside pyrome global grid")
    labels, uncertainties = _pyrome_lookup(str(pyrome_path))
    return labels[iy, ix], uncertainties[iy, ix]


def aggregate_groups(crosswalk, source_igbp, source_pyrome, owned_mask, run_valid_mask,
                     model_pft=None, include_cross_groups=True):
    """Group exact area intersections, never assign an entire mixed cube one class.

    ``crosswalk`` is model-cell by local source-cell area in square metres.
    Returned group arrays use owned AND runnable natural vegetation only.
    Runtime coverage and the full input window remain separate supports.
    """
    crosswalk = sparse.csr_matrix(crosswalk, dtype=np.float64)
    source_igbp, source_pyrome = np.asarray(source_igbp), np.asarray(source_pyrome)
    owned_mask, run_valid_mask = np.asarray(owned_mask, bool), np.asarray(run_valid_mask, bool)
    if owned_mask.shape != run_valid_mask.shape or crosswalk.shape != (owned_mask.size, source_igbp.size):
        raise ValueError("Crosswalk and masks/classification dimensions disagree")
    if source_pyrome.shape != source_igbp.shape or np.any(~np.isin(source_pyrome, list(PYROMES))):
        raise ValueError("Invalid source pyrome classification")
    if np.any(~np.isfinite(crosswalk.data)) or np.any(crosswalk.data < 0):
        raise ValueError("Area crosswalk contains invalid values")
    shape = owned_mask.shape
    common = owned_mask & run_valid_mask
    natural = np.isin(source_igbp, range(1, 12))
    groups = {}

    def add(key, select):
        groups[key] = np.asarray(crosswalk @ np.asarray(select, np.float64)).reshape(shape) * common

    add("overall:all", natural)
    for code, label in PYROMES.items():
        add("pyrome:" + label, natural & (source_pyrome == code))
    for label, codes in VEGETATION.items():
        add("vegetation:" + label, np.isin(source_igbp, codes))
    for code in range(1, 12):
        add("igbp:" + str(code), source_igbp == code)
    if include_cross_groups:
        for code, plabel in PYROMES.items():
            for vlabel, codes in VEGETATION.items():
                add("pyrome_vegetation:" + plabel + ":" + vlabel,
                    (source_pyrome == code) & np.isin(source_igbp, codes))
    if model_pft is not None:
        model_pft = np.asarray(model_pft)
        if model_pft.shape != shape:
            raise ValueError("Model PFT shape differs from ownership mask")
        for code in sorted(np.unique(model_pft[common])):
            # Parameter-group diagnostic only: never replace the 2019 grouping.
            groups["model_pft:" + str(int(code))] = groups["overall:all"] * (model_pft == code)
    for prefix in ("pyrome:", "vegetation:", "igbp:"):
        total = sum(v for k, v in groups.items() if k.startswith(prefix))
        if not np.allclose(total, groups["overall:all"], atol=1e-6, rtol=1e-12):
            raise ValueError("Non-conserving classification areas for " + prefix)
    return groups


def get_groups(region_id, qa_root, source_grid_path=None, pyrome_path=None):
    """Read all grouping/mask provenance for one accepted region.

    ``qa_root`` is the deployment's ``qa/`` directory. This function creates no
    caches in source directories, and never imports the old mutating Support
    constructor. A model-output coordinate equality check is still required by
    the reader before applying these masks.
    """
    qa_root = Path(qa_root)
    folder = qa_root / region_id
    source_grid_path = Path(source_grid_path or DEFAULT_GEOMETRY)
    pyrome_path = Path(pyrome_path or DEFAULT_PYROME)
    with np.load(folder / "ownership.npz", allow_pickle=False) as data:
        owned = {key: data[key] for key in data.files}
    with np.load(folder / "run_masks.npz", allow_pickle=False) as data:
        masks = {key: data[key] for key in data.files}
    with np.load(folder / "static/static_inputs.npz", allow_pickle=False) as data:
        pft = data["f_pft"]
    geom = _geometry(str(source_grid_path))
    shape = (len(owned["lat"]), len(owned["lon"]))
    for name in ("owned_mask", "owned_natural_area_m2", "natural_area_m2"):
        if owned[name].shape != shape:
            raise ValueError("Ownership array shape invalid: " + name)
    for name in ("run_valid_mask", "water_mask", "unknown_mask", "cell_area_m2"):
        if masks[name].shape != shape:
            raise ValueError("Run-mask shape invalid: " + name)
    run_mask = masks["run_valid_mask"].astype(bool)
    if np.any(run_mask & (masks["water_mask"] | masks["unknown_mask"])):
        raise ValueError("Water or unknown pixels enter the accepted run mask")
    index = owned["source_global_flat_index"].astype(np.int64)
    if len(set(index.tolist())) != index.size or np.any(index < 0) or np.any(index >= geom["classes"].size):
        raise ValueError("Invalid or duplicate crosswalk source IDs")
    row, col = np.unravel_index(index, geom["classes"].shape)
    igbp = geom["classes"].ravel()[index]
    pyrome, uncertainty = classify_source_cells(geom["lat"][row], geom["lon"][col], pyrome_path)
    crosswalk = sparse.csr_matrix((owned["crosswalk_data_m2"], owned["crosswalk_indices"], owned["crosswalk_indptr"]),
                                 shape=tuple(owned["crosswalk_shape"]))
    if crosswalk.shape != (int(np.prod(shape)), index.size):
        raise ValueError("Crosswalk shape does not match source-global index lookup")
    # The archived crosswalk is clipped to the original study support. Its
    # natural area may be smaller than a full model pixel at domain boundaries.
    natural_from_csr = np.asarray(crosswalk @ np.isin(igbp, range(1, 12)).astype(float)).reshape(shape)
    if not np.allclose(natural_from_csr, owned["natural_area_m2"], rtol=1e-9, atol=1e-3):
        raise ValueError("Recomputed natural area disagrees with frozen ownership support")
    groups = aggregate_groups(crosswalk, igbp, pyrome, owned["owned_mask"], run_mask, pft)
    run_groups = aggregate_groups(crosswalk, igbp, pyrome, np.ones(shape, bool), run_mask, pft)
    expected = owned["owned_natural_area_m2"] * run_mask
    if not np.allclose(groups["overall:all"], expected, rtol=1e-9, atol=1e-3):
        raise ValueError("Recomputed owned natural area disagrees with archived support")
    if np.any(groups["overall:all"] > masks["cell_area_m2"] + 1):
        raise ValueError("Owned natural area exceeds physical model-cell area")
    uncertainty_weight = np.where(np.isfinite(uncertainty), uncertainty, 0)
    classified_area = np.asarray(crosswalk @ (pyrome > 0).astype(float)).reshape(shape)
    uncertainty_sum = np.asarray(crosswalk @ uncertainty_weight).reshape(shape)
    uncertainty_mean = np.divide(uncertainty_sum, classified_area, out=np.full(shape, np.nan), where=classified_area > 0)
    return {"lat": owned["lat"], "lon": owned["lon"], "run_valid_mask": run_mask,
            "owned_mask": owned["owned_mask"].astype(bool), "owned_area_m2": groups["overall:all"],
            "group_area_m2": groups, "run_group_area_m2": run_groups,
            "run_area_m2": masks["cell_area_m2"] * run_mask,
            "run_natural_area_m2": natural_from_csr * run_mask, "cell_area_m2": masks["cell_area_m2"],
            "water_mask": masks["water_mask"].astype(bool), "unknown_mask": masks["unknown_mask"].astype(bool),
            "model_pft": pft, "pyrome_mean_uncertainty": uncertainty_mean,
            "source_global_flat_index": index, "source_igbp": igbp, "source_pyrome": pyrome,
            "crosswalk": crosswalk, "source_grid_path": str(source_grid_path), "pyrome_path": str(pyrome_path),
            "area_semantics": "group_area_m2=unique owned, runnable, original-2019 natural vegetation area; run_area_m2=full runnable model cells"}
