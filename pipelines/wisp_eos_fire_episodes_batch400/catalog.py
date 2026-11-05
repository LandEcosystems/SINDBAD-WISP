"""Enroll the fixed Batch1 population and partition event tasks into bounded waves.

Importing this module never reads cluster files, launches jobs, or writes output.
Only ``freeze_region`` writes, and only below an explicitly supplied new campaign
region directory. The original pilot selection implementation remains unchanged.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import math
import re
from pathlib import Path

_PILOT_PATH = Path(__file__).resolve().parent.parent / "wisp_eos_fire_episodes" / "selection.py"
_SPEC = importlib.util.spec_from_file_location("batch400_frozen_selection", _PILOT_PATH)
selection = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(selection)

GIB = 2**30
SCRATCH_BUDGET_BYTES = 256 * GIB
WAVE_LIMIT_BYTES = 240 * GIB
FIXED_CACHE_BYTES = 8 * GIB
MAX_WAVE_DAYS = 1000
SHAPES = {
    "hist_weather": (48, 34, 128, 128),
    "hist_quasi_static": (48, 5, 128, 128),
    "static_land": (14, 128, 128),
    "hist_fire": (48, 4, 128, 128),
    "future_weather": (24, 34, 128, 128),
    "hist_fire_any_48h": (1, 128, 128),
    "hist_fire_delta_norm_48h": (1, 128, 128),
}


def _read(path):
    return json.loads(Path(path).read_text())


def token_for(region):
    """Stable production tile identity; the search anchor is a different ID."""
    value = region.get("tile_id")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 999999:
        raise ValueError("Catalog tile_id must be a nonnegative six-digit integer")
    return f"T{value:06d}"


def inventory(catalog_path, acceptance_dir, expected_count=400):
    """Lightweight readiness inventory, without scanning or hashing whole cubes.

    Source-access failures remain explicit retryable pending records. Invalid
    accepted receipts are never treated as scientific rejections or substitutes.
    The returned accepted metadata is read again and verified on enrollment.
    """
    catalog_path, acceptance_dir = Path(catalog_path), Path(acceptance_dir)
    raw = catalog_path.read_bytes()
    catalog = json.loads(raw)
    regions = catalog.get("regions")
    if not isinstance(regions, list) or len(regions) != expected_count:
        raise ValueError(f"Expected exactly {expected_count} frozen production regions")
    tokens = [token_for(r) for r in regions]
    region_ids = [r["region_id"] for r in regions]
    if len(set(tokens)) != len(tokens) or len(set(region_ids)) != len(region_ids):
        raise ValueError("Frozen production catalog contains duplicate identities")
    result = {"catalog_path": str(catalog_path), "catalog_sha256": selection.file_sha256(catalog_path),
              "expected_count": expected_count, "accepted": [], "pending": [], "invalid": []}
    for region in sorted(regions, key=token_for):
        token = token_for(region)
        ap = acceptance_dir / (region["region_id"] + ".json")
        item = {"token": token, "region": copy.deepcopy(region), "acceptance_path": str(ap)}
        try:
            if not ap.exists():
                result["pending"].append({**item, "reason": "acceptance_not_published"})
                continue
            receipt = _read(ap)
            if receipt.get("passed") is not True:
                result["pending"].append({**item, "reason": "acceptance_not_passed",
                                          "errors": receipt.get("errors", [])})
                continue
            if receipt.get("errors"):
                raise ValueError("Accepted receipt also contains validation errors")
            if receipt.get("region_id") != region["region_id"]:
                raise ValueError("Acceptance identity differs from frozen catalog")
            for name in ("file_sha256", "masks_sha256", "owner_sha256"):
                if not re.fullmatch(r"[0-9a-f]{64}", receipt.get(name, "")):
                    raise ValueError("Missing or malformed acceptance checksum: " + name)
            path = Path(receipt["path"])
            if not path.is_file():
                result["pending"].append({**item, "reason": "accepted_cube_unavailable", "retryable": True})
                continue
            if path.stat().st_size != receipt["bytes"]:
                raise ValueError("Accepted cube size differs from validation receipt")
            item.update(acceptance=receipt, acceptance_sha256=selection.file_sha256(ap),
                        qa_paths={"masks_path": receipt["masks_path"],
                                  "masks_sha256": receipt["masks_sha256"],
                                  "owner_path": receipt["owner_path"],
                                  "owner_sha256": receipt["owner_sha256"]})
            result["accepted"].append(item)
        except OSError as exc:
            result["pending"].append({**item, "reason": "source_access_error", "retryable": True,
                                      "error": str(exc)})
        except (ValueError, KeyError, TypeError) as exc:
            result["invalid"].append({**item, "reason": "invalid_acceptance", "error": str(exc)})
    # A changing catalog invalidates this inventory, rather than mixing versions.
    if catalog_path.read_bytes() != raw:
        raise ValueError("Frozen production catalog changed while reading inventory")
    result["counts"] = {name: len(result[name]) for name in ("accepted", "pending", "invalid")}
    return result


def accepted_cube_record(item, region_root, verify_checksum=True):
    """Return legacy-compatible region metadata after checking source identities.

    ``coverage_qa`` is a planned new output path; this function does not create it.
    The caller writes ``coverage_arrays(record)`` there before executing workers.
    Cube labels and full date coverage are checked by ``prepare_region`` next.
    """
    import numpy as np
    import netCDF4

    token, region = item["token"], item["region"]
    if token != token_for(region):
        raise ValueError("Region token differs from tile_id")
    ap = Path(item["acceptance_path"])
    if selection.file_sha256(ap) != item["acceptance_sha256"]:
        raise ValueError("Acceptance changed since readiness inventory")
    receipt = _read(ap)
    if receipt != item["acceptance"] or receipt.get("passed") is not True or receipt.get("errors"):
        raise ValueError("Acceptance is no longer valid")
    path = Path(receipt["path"])
    before = path.stat()
    if before.st_size != receipt["bytes"]:
        raise ValueError("Accepted cube changed size")
    if verify_checksum and selection.file_sha256(path) != receipt["file_sha256"]:
        raise ValueError("Accepted cube checksum changed")
    qa = {k: receipt[k] for k in ("masks_path", "masks_sha256", "owner_path", "owner_sha256")}
    for pathkey, hashkey in (("masks_path", "masks_sha256"), ("owner_path", "owner_sha256")):
        if selection.file_sha256(qa[pathkey]) != qa[hashkey]:
            raise ValueError("Source coverage QA changed: " + pathkey)
    with netCDF4.Dataset(path) as ds:
        for name in ("lat", "lon"):
            actual = np.asarray(ds[name][:])
            if actual.shape != (128,) or not np.array_equal(actual, np.asarray(region[name])):
                raise ValueError("Frozen catalog grid differs from accepted cube: " + name)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Cube changed during enrollment")
    return {**copy.deepcopy(region), "token": token, "cube_path": str(path),
            "cube_sha256": receipt["file_sha256"], "cube_acceptance_sha256": item["acceptance_sha256"],
            "cube_stat": {"path": str(path), "bytes": after.st_size, "mtime_ns": after.st_mtime_ns},
            "coverage_qa": str(Path(region_root) / "qa" / token / "centre_coverage.npz"),
            "source_qa": qa, "acceptance_path": str(ap),
            "enrollment_cube_checksum_verified": bool(verify_checksum)}


def coverage_arrays(record):
    """Read and crop the accepted masks; never infer water from filled soil depth."""
    import numpy as np

    source = record["source_qa"]
    for pathkey, hashkey in (("masks_path", "masks_sha256"), ("owner_path", "owner_sha256")):
        if selection.file_sha256(source[pathkey]) != source[hashkey]:
            raise ValueError("Source QA changed before coverage export")
    output = {}
    with np.load(source["masks_path"], allow_pickle=False) as masks, np.load(source["owner_path"], allow_pickle=False) as owned:
        for container, names in ((masks, ("run_valid_mask", "water_mask", "cell_area_m2")),
                                 (owned, ("owned_mask", "owned_natural_area_m2"))):
            for name in names:
                values = container[name]
                if values.shape != (128, 128):
                    raise ValueError("Source coverage QA shape differs: " + name)
                output[name] = values[16:112, 16:112].copy()
    return output


def task_reservation(task):
    raw_day_bytes = 4 * sum(math.prod(shape) for shape in SHAPES.values())
    return math.ceil(len(task["dates"]) * raw_day_bytes * 1.25) + 32 * 2**20


def partition_waves(tasks, max_days=MAX_WAVE_DAYS, max_reserved_bytes=WAVE_LIMIT_BYTES,
                    fixed_bytes=FIXED_CACHE_BYTES):
    """Partition chronological whole tasks; reserved_bytes includes fixed cache.

    Inputs must be the immutable selection task mapping, with each selected day
    appearing exactly once. Oversized individual tasks fail rather than overfill.
    """
    if not isinstance(max_days, int) or max_days < 1 or not 0 <= fixed_bytes < max_reserved_bytes:
        raise ValueError("Invalid wave limits")
    ordered = sorted(tasks.items(), key=lambda item: (item[1]["dates"][0], item[0]))
    seen_dates, waves, keys, days, reserved = set(), [], [], 0, fixed_bytes
    region = None
    for key, task in ordered:
        dates = task["dates"]
        if not 1 <= len(dates) <= 7 or dates != sorted(set(dates)):
            raise ValueError("Wave task must have one to seven unique ordered dates")
        parsed = [selection.as_date(d) for d in dates]
        if len(selection.merge_dates(parsed)) != 1:
            raise ValueError("Wave task has a date gap")
        if seen_dates.intersection(dates):
            raise ValueError("Duplicate selected day across tasks")
        seen_dates.update(dates)
        if region is None:
            region = task["region"]
        if task["region"] != region:
            raise ValueError("One wave plan must contain exactly one region")
        amount = task_reservation(task)
        if len(dates) > max_days or fixed_bytes + amount > max_reserved_bytes:
            raise ValueError("A whole short task exceeds the configured wave capacity")
        if keys and (days + len(dates) > max_days or reserved + amount > max_reserved_bytes):
            waves.append({"wave_id": len(waves), "keys": keys, "days": days, "reserved_bytes": reserved})
            keys, days, reserved = [], 0, fixed_bytes
        keys.append(key)
        days += len(dates)
        reserved += amount
    if keys:
        waves.append({"wave_id": len(waves), "keys": keys, "days": days, "reserved_bytes": reserved})
    return waves


def parity_dates(manifest):
    """First prediction and a distinct block start; sparse singletons need one."""
    result = {}
    for token, region in manifest["regions"].items():
        own = sorted((task for task in manifest["tasks"].values() if task["region"] == token),
                     key=lambda task: task["dates"][0])
        if not own:
            result[token] = []
            continue
        candidates = [own[0]["dates"][0]]
        candidates += [task["dates"][0] for task in own[1:]]
        candidates += [own[0]["dates"][-1]]
        result[token] = list(dict.fromkeys(candidates))[:2]
    return result


def bundle_from_manifest(manifest):
    """Pure queue constructor, also used to verify frozen-date fixtures."""
    selection.validate_manifest(manifest)
    if len(manifest["regions"]) != 1:
        raise ValueError("A campaign region manifest must contain exactly one region")
    token, selected = next(iter(manifest["regions"].items()))
    waves = partition_waves(manifest["tasks"])
    parity = parity_dates(manifest)
    tasks = copy.deepcopy(manifest["tasks"])
    task_wave = {key: wave["wave_id"] for wave in waves for key in wave["keys"]}
    for priority, key in enumerate(sorted(tasks, key=lambda key: (tasks[key]["dates"][0], key))):
        task = tasks[key]
        task.update(status="queued", input_status="queued", prediction_status="queued", priority=priority,
                    reserved=0, wave_id=task_wave[key],
                    parity_dates=[date for date in parity[token] if date in task["dates"]])
    state = {"phase": "BUILD", "tasks": tasks, "waves": copy.deepcopy(waves), "active_wave": 0,
             "selection_sha256": manifest["selection_sha256"], "quarantine_bytes": 0,
             "region": token, "no_observed_fire": not bool(tasks)}
    return {"token": token, "record": copy.deepcopy(selected["cube_record"]),
            "selection": copy.deepcopy(manifest), "waves": waves, "state": state,
            "parity_dates": parity, "qa_paths": copy.deepcopy(selected["cube_record"].get("source_qa", {})),
            "status": "no_observed_fire" if not tasks else "ready",
            "selected_days": selected["summary"]["selected_days"]}


def prepare_region(record, verify_checksum=True):
    fire_dates, audit = selection.read_cube_fire_days(record, verify_checksum=verify_checksum)
    selected = selection.build_region_selection(record["token"], record, fire_dates, audit=audit)
    return bundle_from_manifest(selection.make_manifest([selected]))


def ready_order(bundles):
    """Longer ready regions first; no error metric or prediction affects order."""
    return sorted(bundles, key=lambda bundle: (-bundle["selected_days"], bundle["token"]))


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def freeze_region(bundle, region_root, campaign_root):
    """Publish immutable region manifests and initialize, never reset, its queue.

    Call only after the caller has chosen an independent new campaign root.
    This function deliberately does not create configs, coverage files or jobs.
    """
    root, campaign = Path(region_root).resolve(), Path(campaign_root).resolve()
    token = bundle["token"]
    if not re.fullmatch(r"T\d{6}", token) or root != campaign / "regions" / token:
        raise ValueError("Region output must be <new campaign>/regions/<tile token>")
    if campaign.name in {"wisp_eos_fire_episodes_v1", "wisp_eos_cube_first_v1", "wisp_eos_q10_v1"}:
        raise ValueError("Refuse to enroll into a legacy running experiment")
    selection.validate_manifest(bundle["selection"])
    if bundle_from_manifest(bundle["selection"]) != bundle:
        raise ValueError("Bundle differs from deterministic selection/wave construction")
    manifest_dir = root / "manifests"
    selection.write_manifest(bundle["selection"], manifest_dir)
    files = {"regions.json": {token: bundle["record"]}, "parity_dates.json": bundle["parity_dates"],
             "waves.json": {"waves": bundle["waves"]}, "source_qa.json": bundle["qa_paths"]}
    for name, value in files.items():
        selection.write_new_or_identical(manifest_dir / name, _json_bytes(value))
    queue_path = root / "state" / "queue.json"
    if queue_path.exists():
        current = _read(queue_path)
        if current.get("selection_sha256") != bundle["selection"]["selection_sha256"]:
            raise ValueError("Existing queue belongs to another selection")
        if set(current.get("tasks", {})) != set(bundle["selection"]["tasks"]):
            raise ValueError("Existing queue task set differs")
        for key, immutable in bundle["selection"]["tasks"].items():
            if any(current["tasks"][key].get(name) != value for name, value in immutable.items()):
                raise ValueError("Existing queue task identity differs: " + key)
    else:
        selection.write_new_or_identical(queue_path, _json_bytes(bundle["state"]))
    names = ["selection.json", "selection.sha256", "fire_dates.csv", "selected_dates.csv", "episodes.csv",
             "tasks.csv", *files]
    receipt = {"schema": "wisp_fire_episode_region_freeze_v2", "token": token,
               "selection_sha256": bundle["selection"]["selection_sha256"],
               "files": {name: selection.file_sha256(manifest_dir / name) for name in names}}
    selection.write_new_or_identical(manifest_dir / "region_freeze.json", _json_bytes(receipt))
    return {"region_root": str(root), "selection_path": str(manifest_dir / "selection.json"),
            "queue_path": str(queue_path), "freeze_receipt": str(manifest_dir / "region_freeze.json")}
