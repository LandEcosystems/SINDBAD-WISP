"""Extract effective run metadata from JLD2 without launching a Julia model.

Only the known serializable metadata branches are read. Arbitrary Julia
objects/functions are not deserialized or executed. The saved JSON templates
can predate per-run overrides and are not authoritative for forcing scalars.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import h5py
import numpy as np

from .registry import file_identity


def _decode(h, value, depth=0):
    if depth > 12:
        raise ValueError("Metadata reference nesting exceeds the audited reader limit")
    if isinstance(value, h5py.Reference):
        if not value:
            return None
        return _decode(h, h[value][()], depth + 1)
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, np.void) and value.dtype.names:
        return {name: _decode(h, value[name], depth + 1) for name in value.dtype.names}
    if isinstance(value, np.ndarray):
        return [_decode(h, item, depth + 1) for item in value] if value.ndim else _decode(h, value.item(), depth + 1)
    if isinstance(value, np.generic):
        return value.item()
    return value


def _parameter_map(path):
    import zarr
    z = zarr.open_group(str(path), mode="r")
    arrays = {}
    digest = hashlib.sha256()
    for name in sorted(z.array_keys()):
        a = np.asarray(z[name][:])
        digest.update(name.encode()); digest.update(str(a.dtype).encode()); digest.update(str(a.shape).encode())
        digest.update(a.tobytes())
        digest.update(json.dumps(dict(z[name].attrs), sort_keys=True, ensure_ascii=False).encode())
        arrays[name] = {"shape": list(a.shape), "dtype": str(a.dtype), "attrs": dict(z[name].attrs)}
    if "f_pft" not in z or "fAPAR__k_extinction" not in z:
        raise ValueError("Actual PFT map is missing f_pft or fAPAR__k_extinction")
    pft, values = np.asarray(z["f_pft"][:]), np.asarray(z["fAPAR__k_extinction"][:])
    if pft.shape != values.shape or np.any(~np.isfinite(values)):
        raise ValueError("Invalid fAPAR PFT map values")
    return {"path": str(path), "content_sha256": digest.hexdigest(), "array_metadata": arrays,
            "fapar_k_extinction_by_pft": {str(int(p)): float(v) for p, v in zip(pft, values)},
            "fire_parameters_in_map": sorted(name for name in arrays if name.startswith("cFire"))}


def inspect_run(runpath):
    """Return evidence and explicit checks for the actual frozen V1 run.

    Metadata decoding failures are returned as failures, not replaced with
    template defaults. Callers should save the record and gate scientific
    comparisons on ``status`` while allowing an evidence-only inventory.
    """
    runpath = Path(runpath)
    result = {"path": str(runpath), "status": "pending", "checks": [], "evidence": {},
              "precedence": "Actual info.jld2 forcing and parameter paths, then saved code/structure; README is explanatory only"}

    def check(name, passed, **evidence):
        result["checks"].append({"check": name, "status": "pass" if passed else "fail", **evidence})

    try:
        info_path = runpath / "settings/info.jld2"
        result["evidence"]["info_jld2"] = file_identity(info_path, content_hash=True)
        with h5py.File(info_path, "r") as h:
            info = h["info"][()]
            experiment = h[info["experiment"]][()]
            result["basics"] = _decode(h, experiment["basics"])
            forcing = _decode(h, experiment["data_settings"]["forcing"])
            result["forcing"] = forcing
            result["parameter_map"] = _decode(h, experiment["data_settings"]["parameters"]["default_parameter_map"])
            result["data_compression"] = _decode(h, info["output"]["data_compression"])
            models = h[info["models"]][()]
            result["selected_processes"] = _decode(h, models["selected_models"]["data"]["model"])
            spinup = h[info["spinup"]][()]
            result["spinup_options"] = _decode(h, spinup["options"])
        structure_path = runpath / "settings/model_structure_WISP_DD.json"
        result["evidence"]["saved_structure"] = file_identity(structure_path, content_hash=True)
        structure = json.loads(structure_path.read_text())
        result["model_approaches"] = {name: meta["approach"] for name, meta in structure["models"].items()}
        result["configured_water_structure"] = structure["pools"]["water"]
        codepath = runpath / "code/model_functions.jl"
        codetext = codepath.read_text()
        result["evidence"]["saved_model_functions"] = file_identity(codepath, content_hash=True)
        check("saved_structure_matches_actual_selected_processes",
              set(result["model_approaches"]) == set(result["selected_processes"]))
        check("extra_nonfire_dieoff_disabled",
              result["model_approaches"].get("cVegetationDieOff") == "none" and "# cVegetationDieOff_none" in codetext)
        check("lai_not_enabled", "LAI" not in result["selected_processes"])
        check("quantization_four_digits", result["data_compression"].get("digits") == 4,
              digits=result["data_compression"].get("digits"))
        variable = forcing["variables"]
        fire = variable["f_burnt_area"]
        expected = 0.0 if runpath.name.endswith("_No_Fire") else 0.99 if runpath.name.endswith("_VIIRS_Fire") else None
        check("actual_fire_forcing_scalar", expected is not None and abs(fire["source_to_sindbad_unit"] - expected) < 1e-12,
              expected=expected, actual=fire["source_to_sindbad_unit"], source_variable=fire["source_variable"])
        check("fire_source_is_viirs_proxy", fire["source_variable"] == "viirs_fire_binary")
        check("actual_vegetation_scalar", abs(variable["f_frac_vegetation"]["source_to_sindbad_unit"] - 0.99) < 1e-12,
              actual=variable["f_frac_vegetation"]["source_to_sindbad_unit"])
        parameter_path = Path(result["parameter_map"]["data_path"])
        result["actual_parameter_values"] = _parameter_map(parameter_path)
        check("parameter_lookup_matches_model_pft",
              result["parameter_map"]["lookup_parameter_map"]["lookup_variable"] == "f_pft")
        check("fapar_uses_mapped_parameters", len(result["actual_parameter_values"]["fapar_k_extinction_by_pft"]) == 13)
        result["warnings"] = [
            "A saved restart may precede age regrowth; it is not used as the first forward stock.",
            "PFT map values override scalar template defaults; saved parameter_table.optimized alone is insufficient.",
            "Per-m2 area interpretation is retained without multiplying vegetation fraction; reference-total scaling remains a separate gate.",
            "Fire forcing is a VIIRS active-fire proxy scaled to 0.99, not measured burned-area fraction.",
        ]
    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        result["checks"].append({"check": "actual_run_metadata_readable", "status": "fail", "error": type(exc).__name__ + ": " + str(exc)})
    result["status"] = "fail" if any(c["status"] == "fail" for c in result["checks"]) else "pass"
    return result
