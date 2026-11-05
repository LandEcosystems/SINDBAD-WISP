"""Deterministic, observation-conditioned fire-episode date selection.

Selection is deliberately separate from raw-input construction and prediction.
Dates denote prediction windows [D 00:00 UTC, D+1 00:00 UTC), not issue dates.
No future labels are passed through this module as model input.
"""
from __future__ import annotations

import argparse
import copy
import csv
from datetime import date, datetime, timedelta
import hashlib
import io
import json
import math
import os
from pathlib import Path

START = "2014-01-12"
END = "2023-12-31"
TOKENS = ("T000830", "T000001")
EXPECTED = {
    "T000830": {"fire_days": 28, "selected_days": 258, "episodes": 21, "tasks": 44},
    "T000001": {"fire_days": 12, "selected_days": 132, "episodes": 12, "tasks": 24},
}


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def as_date(value):
    if isinstance(value, datetime):
        if value.time().isoformat() != "00:00:00":
            raise ValueError("A prediction date must be at midnight: " + str(value))
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)
    if len(text) > 10 and text[10:] not in ("T00:00:00", " 00:00:00"):
        raise ValueError("A prediction date must be at midnight: " + text)
    return date.fromisoformat(text[:10])


def timestamp(day):
    return as_date(day).isoformat() + "T00:00:00"


def daily_range(start, end):
    start, end = as_date(start), as_date(end)
    if end < start:
        raise ValueError("End date precedes start date")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def select_dates(fire_dates, start=START, end=END, margin_days=5):
    """Expand each observed fire date once, then union and clip inclusively."""
    start, end = as_date(start), as_date(end)
    if end < start or not isinstance(margin_days, int) or margin_days < 0:
        raise ValueError("Invalid date window or margin")
    fires = [as_date(d) for d in fire_dates]
    if len(fires) != len(set(fires)):
        raise ValueError("Duplicate observed fire date")
    if any(d < start or d > end for d in fires):
        raise ValueError("Fire dates outside the research period")
    selected = set()
    for day in fires:
        selected.update(daily_range(max(start, day - timedelta(days=margin_days)),
                                    min(end, day + timedelta(days=margin_days))))
    return sorted(selected)


def merge_dates(selected_dates):
    """Merge overlapping or adjacent selected dates, never fill a gap."""
    days = [as_date(d) for d in selected_dates]
    if days != sorted(set(days)):
        raise ValueError("Selected dates must be unique and sorted")
    episodes = []
    for day in days:
        if not episodes or day != episodes[-1][-1] + timedelta(days=1):
            episodes.append([])
        episodes[-1].append(day)
    return episodes


def balanced_chunks(days, maximum=7):
    """Split one continuous episode into ceil(L / maximum) balanced chunks."""
    days = [as_date(d) for d in days]
    if not isinstance(maximum, int) or maximum < 1:
        raise ValueError("Maximum chunk length must be positive")
    if not days:
        return []
    if len(merge_dates(days)) != 1:
        raise ValueError("A chunk source must be one contiguous episode")
    count = math.ceil(len(days) / maximum)
    size, extra = divmod(len(days), count)
    result, offset = [], 0
    for index in range(count):
        length = size + (index < extra)
        result.append(days[offset:offset + length])
        offset += length
    return result


def build_region_selection(token, cube_record, fire_dates, *, start=START, end=END,
                           margin_days=5, max_chunk_days=7, audit=None):
    fires = sorted(as_date(d) for d in fire_dates)
    selected = select_dates(fires, start, end, margin_days)
    episodes, blocks = [], []
    for index, days in enumerate(merge_dates(selected), 1):
        episode_id = f"{token}_E{index:03d}"
        episodes.append({"episode_id": episode_id, "episode_index": index,
                         "start": days[0].isoformat(), "end": days[-1].isoformat(),
                         "dates": [timestamp(d) for d in days],
                         "fire_dates": [d.isoformat() for d in fires if days[0] <= d <= days[-1]]})
        for chunk in balanced_chunks(days, max_chunk_days):
            blocks.append({"block_id": f"B{len(blocks) + 1:03d}", "episode_id": episode_id,
                           "episode_index": index, "dates": [timestamp(d) for d in chunk]})
    summary = {"fire_days": len(fires), "selected_days": len(selected),
               "episodes": len(episodes), "tasks": len(blocks)}
    return {"token": token, "region_id": cube_record["region_id"],
            "cube_record": copy.deepcopy(cube_record), "source_audit": audit or {},
            "fire_dates": [d.isoformat() for d in fires],
            "selected_dates": [timestamp(d) for d in selected],
            "episodes": episodes, "blocks": blocks, "summary": summary}


def make_manifest(regions, *, start=START, end=END, margin_days=5, max_chunk_days=7):
    """Build immutable selection and task identities without run timestamps."""
    by_token = {r["token"]: copy.deepcopy(r) for r in regions}
    if len(by_token) != len(regions):
        raise ValueError("Duplicate region token")
    rules = {"start": as_date(start).isoformat(), "end_inclusive": as_date(end).isoformat(),
             "time_zone": "UTC", "prediction_window": "[D 00:00, D+1 00:00)",
             "fire_variable": "viirs_fire_binary", "fire_test": "any centre pixel == 1",
             "centre_slices": [[16, 112], [16, 112]], "margin_days": margin_days,
             "margin_rule": "union of one expansion per observed fire day; clipped to period",
             "merge_adjacent": True, "max_chunk_days": max_chunk_days,
             "chunk_rule": "ceil(L/max) balanced lengths; earlier chunks receive remainders",
             "missing_fire_policy": "reject unknown or nonbinary centre pixel; never coerce to zero",
             "input_window_hours": [-96, 24], "future_labels_used_as_model_input": False,
             "experiment_interpretation": "retrospective observation-conditioned event experiment"}
    identity = {"schema": "wisp_fire_episodes_selection_v1", "rules": rules,
                "regions": by_token}
    selection_sha = digest(identity)
    tasks = {}
    for token, region in by_token.items():
        for block in region["blocks"]:
            key = f"{token}_evt_{selection_sha[:12]}_{block['block_id']}"
            tasks[key] = {"region": token, "kind": "episode", **copy.deepcopy(block),
                          "selection_sha256": selection_sha}
    summary = {key: sum(r["summary"][key] for r in by_token.values())
               for key in ("fire_days", "selected_days", "episodes", "tasks")}
    summary.update(regions=len(by_token), full_cube_days=len(by_token) * len(daily_range(start, end)))
    result = {**identity, "selection_sha256": selection_sha, "tasks": tasks, "summary": summary}
    validate_manifest(result)
    return result


def validate_manifest(manifest):
    """Validate the complete derived task mapping, not merely the headline hash."""
    identity = {k: manifest[k] for k in ("schema", "rules", "regions")}
    if digest(identity) != manifest["selection_sha256"]:
        raise ValueError("Selection identity checksum mismatch")
    rules = manifest["rules"]
    expected_tasks = {}
    for token, region in manifest["regions"].items():
        expected = build_region_selection(token, region["cube_record"], region["fire_dates"],
            start=rules["start"], end=rules["end_inclusive"], margin_days=rules["margin_days"],
            max_chunk_days=rules["max_chunk_days"], audit=region["source_audit"])
        if expected != region:
            raise ValueError("Region dates or episodes differ from selection rule: " + token)
        for block in region["blocks"]:
            key = f"{token}_evt_{manifest['selection_sha256'][:12]}_{block['block_id']}"
            expected_tasks[key] = {"region": token, "kind": "episode", **copy.deepcopy(block),
                                  "selection_sha256": manifest["selection_sha256"]}
    if expected_tasks != manifest["tasks"]:
        raise ValueError("Tasks do not exactly partition the selected dates")
    expected_summary = {k: sum(r["summary"][k] for r in manifest["regions"].values())
                        for k in ("fire_days", "selected_days", "episodes", "tasks")}
    expected_summary.update(regions=len(manifest["regions"]), full_cube_days=len(manifest["regions"]) *
                            len(daily_range(rules["start"], rules["end_inclusive"])))
    if manifest["summary"] != expected_summary:
        raise ValueError("Selection summary differs")
    return True


def read_cube_fire_days(record, *, start=START, end=END, verify_checksum=True):
    """Read exact daily centre labels in bounded chunks with strict missingness."""
    import netCDF4
    import numpy as np

    path = Path(record["cube_path"])
    before = path.stat()
    supplied_stat = record.get("cube_stat", {})
    if (supplied_stat.get("bytes", before.st_size) != before.st_size or
            supplied_stat.get("mtime_ns", before.st_mtime_ns) != before.st_mtime_ns):
        raise ValueError("Source cube changed since its accepted manifest: " + str(path))
    actual_sha = file_sha256(path) if verify_checksum else record.get("cube_sha256")
    if not actual_sha or actual_sha != record.get("cube_sha256"):
        raise ValueError("Source cube checksum differs: " + str(path))
    expected_dates = daily_range(start, end)
    fire_dates = []
    with netCDF4.Dataset(path) as ds:
        if int(record.get("day_start_hour_utc", 0)) != 0:
            raise ValueError("The episode pilot requires UTC midnight windows")
        time = ds["time"]
        decoded = netCDF4.num2date(time[:], units=time.units,
                                  calendar=getattr(time, "calendar", "standard"))
        observed_dates = []
        for value in decoded:
            if (value.hour, value.minute, value.second, value.microsecond) != (0, 0, 0, 0):
                raise ValueError("Cube timestamps are not exact UTC midnight")
            observed_dates.append(date(value.year, value.month, value.day))
        if observed_dates != expected_dates:
            raise ValueError("Cube date axis is incomplete, duplicated, reordered or out of period")
        for name in ("lat", "lon"):
            axis = np.asarray(ds[name][:])
            if axis.shape != (128,) or not np.isfinite(axis).all() or not np.all(np.diff(axis) > 0):
                raise ValueError("Invalid 128-pixel spatial axis: " + name)
            if name in record and not np.array_equal(axis, np.asarray(record[name])):
                raise ValueError("Cube spatial axis differs from frozen region: " + name)
        var = ds["viirs_fire_binary"]
        if var.dimensions != ("time", "lat", "lon") or var.shape != (len(expected_dates), 128, 128):
            raise ValueError("Unexpected VIIRS dimensions")
        for offset in range(0, len(expected_dates), 64):
            values = np.ma.filled(var[offset:offset + 64, 16:112, 16:112], np.nan)
            if not np.isfinite(values).all():
                raise ValueError("Unknown VIIRS centre pixels cannot be treated as no fire: " +
                                 expected_dates[offset].isoformat())
            if not np.isin(values, (0, 1)).all():
                raise ValueError("VIIRS reference contains nonbinary values")
            found = np.any(values == 1, axis=(1, 2))
            fire_dates.extend(expected_dates[offset + i] for i in np.flatnonzero(found))
        variable_source = getattr(var, "source_description", "")
    after = path.stat()
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise ValueError("Cube changed while selecting fire dates")
    return fire_dates, {"cube_sha256": actual_sha, "checksum_read_verified": verify_checksum,
                        "cube_bytes": before.st_size, "cube_mtime_ns": before.st_mtime_ns,
                        "date_count": len(expected_dates), "all_centre_labels_finite_binary": True,
                        "variable_source_description": variable_source}


def load_regions(path):
    data = json.loads(Path(path).read_text())
    if isinstance(data, list):
        result = {r["token"]: r for r in data}
        if len(result) != len(data):
            raise ValueError("Duplicate token in region records")
        return result
    if isinstance(data, dict) and "regions" in data:
        data = data["regions"]
        if isinstance(data, list):
            result = {r["token"]: r for r in data}
            if len(result) != len(data):
                raise ValueError("Duplicate token in region records")
            return result
    if not isinstance(data, dict):
        raise ValueError("Expected a token-to-region map or a list of region records")
    return data


def write_new_or_identical(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError("Refuse to overwrite a different frozen selection: " + str(path))
        return
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(temporary, "xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        # link provides no-clobber publication even if two selectors race.
        os.link(temporary, path)
    except FileExistsError:
        if path.read_bytes() != content:
            raise ValueError("Concurrent selection differs: " + str(path))
    finally:
        temporary.unlink()


def csv_bytes(rows, fields):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


def write_manifest(manifest, output_dir):
    validate_manifest(manifest)
    output = Path(output_dir)
    files = {"selection.json": (json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(),
             "selection.sha256": (manifest["selection_sha256"] + "\n").encode()}
    fires, days, episodes, tasks = [], [], [], []
    for token, region in manifest["regions"].items():
        fires.extend({"region": token, "fire_date": day} for day in region["fire_dates"])
        fire_set = set(region["fire_dates"])
        for episode in region["episodes"]:
            episodes.append({"region": token, "episode_id": episode["episode_id"],
                             "start": episode["start"], "end_inclusive": episode["end"],
                             "selected_days": len(episode["dates"]), "fire_days": len(episode["fire_dates"])})
            days.extend({"region": token, "date": day, "episode_id": episode["episode_id"],
                         "observed_fire": int(day[:10] in fire_set)} for day in episode["dates"])
    for key, task in manifest["tasks"].items():
        tasks.append({"task_key": key, "region": task["region"], "episode_id": task["episode_id"],
                      "block_id": task["block_id"], "start": task["dates"][0],
                      "end_inclusive": task["dates"][-1], "days": len(task["dates"]),
                      "selection_sha256": manifest["selection_sha256"]})
    files["fire_dates.csv"] = csv_bytes(fires, ["region", "fire_date"])
    files["selected_dates.csv"] = csv_bytes(days, ["region", "date", "episode_id", "observed_fire"])
    files["episodes.csv"] = csv_bytes(episodes, ["region", "episode_id", "start", "end_inclusive", "selected_days", "fire_days"])
    files["tasks.csv"] = csv_bytes(tasks, ["task_key", "region", "episode_id", "block_id", "start", "end_inclusive", "days", "selection_sha256"])
    for name, content in files.items():
        write_new_or_identical(output / name, content)
    return output / "selection.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regions-json", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--tokens", nargs="+", default=list(TOKENS))
    args = parser.parse_args(argv)
    records, selections = load_regions(args.regions_json), []
    for token in args.tokens:
        record = records[token]
        fire_dates, audit = read_cube_fire_days(record)
        selection = build_region_selection(token, record, fire_dates, audit=audit)
        if token in EXPECTED and selection["summary"] != EXPECTED[token]:
            raise ValueError(f"Selection count differs from reviewed plan for {token}: {selection['summary']}")
        selections.append(selection)
    manifest = make_manifest(selections)
    result = write_manifest(manifest, args.output_dir)
    print(json.dumps({"manifest": str(result), "selection_sha256": manifest["selection_sha256"],
                      "summary": manifest["summary"]}, sort_keys=True))


if __name__ == "__main__":
    main()
