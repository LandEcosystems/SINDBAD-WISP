"""Saved-output identities and a read-only, immutable-source inventory.

``components`` use the Julia layer order, but Python arrays are zero based.
Inventory fingerprints cover file metadata and small saved code/settings files;
they are NOT checksums of the approximately one-terabyte scientific arrays.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

CARBON_POOLS = ["root", "wood", "leaf", "reserve", "litter_fast", "litter_slow", "soil_slow", "soil_old"]
WATER_POOLS = ["soil_1", "soil_2", "soil_3", "soil_4", "soil_5", "soil_6", "soil_7", "groundwater", "snow", "surface"]
SCENARIOS = ("No_Fire", "VIIRS_Fire")


def _variable(components, kind, units, meaning, nonnegative=True):
    return {"components": components, "kind": kind, "units": units,
            "meaning": meaning, "nonnegative": nonnegative}


VARIABLES = {
    "gpp": _variable(["gpp"], "sumflux", "g C m-2 day-1", "Gross primary production"),
    "auto_respiration": _variable(["auto_respiration"], "sumflux", "g C m-2 day-1", "Autotrophic respiration"),
    "hetero_respiration": _variable(["hetero_respiration"], "sumflux", "g C m-2 day-1", "Heterotrophic respiration"),
    "eco_respiration": _variable(["eco_respiration"], "sumflux", "g C m-2 day-1", "Total ecosystem respiration"),
    "nee": _variable(["nee"], "sumflux", "g C m-2 day-1", "Ecosystem respiration minus GPP; excludes direct fire emissions", False),
    "evapotranspiration": _variable(["evapotranspiration"], "sumflux", "mm day-1", "Total evapotranspiration"),
    "transpiration": _variable(["transpiration"], "sumflux", "mm day-1", "Plant transpiration"),
    "runoff": _variable(["runoff"], "sumflux", "mm day-1", "Runoff"),
    "cEco": _variable(CARBON_POOLS, "meanstock", "g C m-2", "Eight ecosystem carbon stocks; do not integrate stocks as emissions"),
    "TWS": _variable(WATER_POOLS, "meanstock", "mm", "Seven soil water layers, groundwater, snow and surface water"),
    "c_fire_efflux": _variable(CARBON_POOLS, "sumflux", "g C m-2 day-1", "Direct fire carbon emissions by source pool"),
    "c_fire_flow": _variable(CARBON_POOLS, "sumflux", "g C m-2 day-1", "Internal non-combusted carbon transfer; not additional emissions"),
    "c_fire_mortality": _variable(CARBON_POOLS, "sumflux", "g C m-2 day-1", "Fire-caused carbon mortality; not additive to fire efflux and transfer"),
}

DERIVED = {
    "cVeg": {"source": "cEco", "indices": [0, 1, 2, 3], "units": "g C m-2", "kind": "meanstock"},
    "cLitter": {"source": "cEco", "indices": [4, 5], "units": "g C m-2", "kind": "meanstock"},
    "cSoil": {"source": "cEco", "indices": [6, 7], "units": "g C m-2", "kind": "meanstock"},
    "cEcosystem": {"source": "cEco", "indices": list(range(8)), "units": "g C m-2", "kind": "meanstock"},
    "total_water_storage": {"source": "TWS", "indices": list(range(10)), "units": "mm", "kind": "meanstock"},
    "fire_carbon_emission": {"source": "c_fire_efflux", "indices": list(range(8)), "units": "g C m-2 day-1", "kind": "sumflux"},
    "fire_carbon_transfer": {"source": "c_fire_flow", "indices": list(range(8)), "units": "g C m-2 day-1", "kind": "sumflux"},
    "fire_carbon_mortality": {"source": "c_fire_mortality", "indices": list(range(8)), "units": "g C m-2 day-1", "kind": "sumflux"},
}

UNAVAILABLE = {"LAI": "Not saved and not uniquely recoverable from current leaf-carbon/fAPAR outputs; no supplementary model run authorized."}


def components():
    """Return 50 independent saved component descriptors."""
    return [dict(variable=name, component=component, index=index, **{k: v for k, v in meta.items() if k != "components"})
            for name, meta in VARIABLES.items() for index, component in enumerate(meta["components"])]


def file_identity(path, content_hash=False):
    path = Path(path)
    stat = path.stat()
    record = {"path": str(path.resolve()), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    if content_hash:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        record["sha256"] = digest.hexdigest()
    return record


def inventory(source_root, output_json=None, region_ids=None):
    """Freeze completed IDs and source identities without modifying source data.

    A done marker is evidence of workflow completion, not scientific acceptance.
    Existing snapshots cannot be overwritten with different content. Refreshes
    must use a new snapshot filename.
    """
    source_root = Path(source_root)
    discovered = sorted(p.name for p in (source_root / "done").glob("B1_*") if p.is_file())
    ids = sorted(region_ids if region_ids is not None else discovered)
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate requested region IDs")
    records = []
    for rid in ids:
        if rid not in discovered:
            raise ValueError("No workflow completion marker for " + rid)
        entry = {"region_id": rid, "done": file_identity(source_root / "done" / rid), "scenarios": {}}
        for scenario in SCENARIOS:
            directory = source_root / rid / ("output_WISP_" + rid + "-full_" + scenario)
            files = {p.name: file_identity(p) for p in sorted((directory / "data").glob("*.nc"))}
            expected = {name + ".nc" for name in VARIABLES} | {"restart.nc"}
            provenance = []
            for folder in ("settings", "code"):
                for p in sorted((directory / folder).glob("*")):
                    if p.is_file():
                        provenance.append(file_identity(p, content_hash=p.suffix in (".json", ".jl", ".toml")))
            entry["scenarios"][scenario] = {"path": str(directory), "files": files, "provenance": provenance,
                                               "missing_files": sorted(expected - set(files)),
                                               "unexpected_files": sorted(set(files) - expected)}
        comparison = source_root / rid / "comparison" / ("comparison_metrics_" + rid + "-full.csv")
        entry["comparison_csv"] = None
        if comparison.is_file():
            with comparison.open(newline="") as stream:
                rows = sum(1 for _ in csv.DictReader(stream))
            entry["comparison_csv"] = dict(file_identity(comparison, content_hash=True), rows=rows)
        records.append(entry)
    fingerprint = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    snapshot = {"schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
                "source_root": str(source_root), "regions": records, "n_regions": len(records),
                "scenarios": list(SCENARIOS), "saved_variables": len(VARIABLES), "saved_components": len(components()),
                "source_metadata_fingerprint": fingerprint,
                "checksum_scope": "Scientific files: size/mtime metadata only; small saved code/settings and comparison CSV: SHA256",
                "status": "inventory_only_not_scientific_acceptance"}
    if output_json is not None:
        output_json = Path(output_json)
        if output_json.exists():
            previous = json.loads(output_json.read_text())
            if previous.get("source_metadata_fingerprint") != fingerprint:
                raise FileExistsError("Refusing to replace a different analysis snapshot: " + str(output_json))
            return previous
        output_json.parent.mkdir(parents=True, exist_ok=True)
        with output_json.open("x") as stream:
            json.dump(snapshot, stream, indent=2)
    return snapshot
