"""Upgrade only quiescent, pre-production campaign operations, with a journal.

This never cancels jobs, edits frozen sources, resets selection, or resumes the
controller. Run from a checkout outside the live code directory. The caller must
stop only this campaign's scanner/controller first. A journal permits rerunning
the same command after interruption between directory swaps or identity updates.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from common import DEFAULT_ROOT, PREFIX, atomic, locked, now, read, sha, verify_release

ALLOWED_STATUSES = {"READY", "REUSED", "NO_OBSERVED_FIRE", "WAITING_SOURCE", "SCANNING", "RETRY_SOURCE", "BLOCKED"}
FROZEN_PATHS = ["config/campaign.json", "config/frozen_deployment.json",
                "manifests/named_production_regions.json", "manifests/deployment.json",
                "manifests/source_files.json", "manifests/fire_index_source.json"]
IDENTITY_PATHS = {"config_sha256": "config/deployment.json", "selection_sha256": "manifests/selection.json",
                  "regions_sha256": "manifests/regions.json", "parity_dates_sha256": "manifests/parity_dates.json",
                  "waves_sha256": "manifests/waves.json"}


def active_jobs():
    result = subprocess.run(["squeue", "-h", "-r", "-u", os.environ.get("USER", "ybai"),
                             "-o", "%i|%j|%T"], check=True, text=True, capture_output=True, timeout=30)
    output = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        fields = line.split("|")
        if len(fields) != 3:
            raise ValueError("Cannot parse live Slurm queue; upgrade denied")
        output.append(dict(zip(("id", "name", "state"), fields)))
    return output


def check_quiescent(root, rows):
    root = Path(root)
    registered = {p.stem for p in (root / "state/submissions").glob("*.json")}
    active = [r for r in rows if str(r["id"]) in registered or r["name"].startswith(PREFIX)]
    if active:
        raise ValueError("Campaign jobs are still active or queued: " + ", ".join(str(r["id"]) for r in active))
    state = read(root / "state/campaign.json")
    if any(state.get("lanes", [])) or state.get("pilot_passed"):
        raise ValueError("Production has been admitted; pre-production upgrade denied")
    if any(record.get("status") not in ALLOWED_STATUSES for record in state["regions"].values()):
        raise ValueError("Campaign has a production or unknown region state")
    for queue_path in (root / "regions").glob("*/state/queue.json"):
        queue = read(queue_path)
        if queue.get("phase") != "BUILD" or queue.get("active_wave", 0) != 0:
            raise ValueError("Region has advanced beyond enrollment: " + str(queue_path))
        for task in queue.get("tasks", {}).values():
            if (any(task.get(k) is not None for k in ("lease", "job", "pid", "stage"))
                    or task.get("input_status") != "queued" or task.get("prediction_status") != "queued"
                    or task.get("reserved", 0) != 0):
                raise ValueError("Region contains active or started work: " + str(queue_path))
    return state


def _code_check(code, release):
    code = Path(code).resolve()
    if not release.get("files"):
        raise ValueError("Release has no immutable code manifest")
    for name, expected in release["files"].items():
        path = (code / name).resolve()
        if not path.is_relative_to(code) or sha(path) != expected:
            raise ValueError("Archived or staged release file changed: " + name)


def _source_files(source):
    source = Path(source)
    return {p.name: sha(p) for p in source.iterdir() if p.is_file() and p.suffix in {".py", ".md"}}


def _verify_region(root, rr, identity, allowed_release_hashes):
    if identity.get("campaign_release_sha256") not in allowed_release_hashes:
        raise ValueError("Region is bound to a different campaign release: " + rr.name)
    for field, relative in IDENTITY_PATHS.items():
        if identity.get(field) != sha(rr / relative):
            raise ValueError("Frozen region identity changed: " + rr.name + " " + relative)
    if identity.get("deployment_fingerprint") != read(root / "manifests/deployment.json")["fingerprint"]:
        raise ValueError("Scientific deployment fingerprint changed: " + rr.name)
    if read(rr / "manifests/deployment.json") != read(root / "manifests/deployment.json"):
        raise ValueError("Region scientific deployment differs: " + rr.name)
    region = read(rr / "manifests/regions.json")[rr.name]
    if sha(region["coverage_qa"]) != identity.get("coverage_qa_sha256"):
        raise ValueError("Region coverage QA changed: " + rr.name)


def _preserved_hashes(root):
    paths = FROZEN_PATHS + ["state/campaign.json"]
    return {name: sha(root / name) for name in paths}


def _assert_preserved(root, expected):
    for name, value in expected.items():
        if sha(root / name) != value:
            raise ValueError("Frozen configuration/source/state changed during upgrade: " + name)


def _prepare(source, root, commit):
    old = verify_release(root)
    old_sha = sha(root / "code/release.json")
    old_code = root / "code"
    pilot_source = source.parent / "wisp_eos_fire_episodes"
    if _source_files(pilot_source) != _source_files(old_code / "wisp_eos_fire_episodes"):
        raise ValueError("Frozen pilot dependency differs; operational upgrade cannot replace it")
    identity_paths = sorted((root / "regions").glob("*/manifests/identity.json"))
    for path in identity_paths:
        _verify_region(root, path.parent.parent, read(path), {old_sha})
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = root / "releases" / (old["git_commit"][:12] + "_" + stamp)
    archive.mkdir(parents=True, exist_ok=False)
    for name in ("config", "manifests"):
        shutil.copytree(root / name, archive / name)
    shutil.copy2(root / "state/campaign.json", archive / "campaign_before.json")
    for path in identity_paths:
        target = archive / "region_identities" / (path.parent.parent.name + ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    staged = archive / "staged_code"
    shutil.copytree(old_code, staged)
    shutil.rmtree(staged / "batch400")
    (staged / "batch400").mkdir()
    for name in _source_files(source):
        shutil.copy2(source / name, staged / "batch400" / name)
    # Preserve the launcher and pilot byte-for-byte. Drop incidental interpreter caches.
    for directory in list(staged.rglob("__pycache__")):
        shutil.rmtree(directory)
    (staged / "release.json").unlink()
    files = {str(p.relative_to(staged)): sha(p) for p in sorted(staged.rglob("*")) if p.is_file()}
    new = dict(created=now(), git_commit=commit, files=files,
               campaign_config_sha256=sha(root / "config/campaign.json"),
               catalog_sha256=sha(root / "manifests/named_production_regions.json"),
               source_files={name: sha(root / name) for name in FROZEN_PATHS
                             if name not in {"config/campaign.json", "manifests/named_production_regions.json"}},
               operational_upgrade=dict(previous_commit=old["git_commit"], previous_release_sha256=old_sha,
                                        archive=str(archive)))
    atomic(staged / "release.json", new)
    _code_check(staged, new)
    preserved = _preserved_hashes(root)
    journal = dict(schema=1, phase="PREPARED", started=now(), root=str(root), archive=str(archive),
                   source=str(source), source_hashes=_source_files(source),
                   old_commit=old["git_commit"], new_commit=commit,
                   old_release_sha256=old_sha, new_release_sha256=sha(staged / "release.json"),
                   preserved_hashes=preserved,
                   deployment_fingerprint=read(root / "manifests/deployment.json")["fingerprint"],
                   identity_tokens=[path.parent.parent.name for path in identity_paths])
    atomic(root / "state/operational_upgrade.json", journal)
    atomic(archive / "migration.json", journal)
    return journal


def _finish(root, journal, rows_provider):
    archive = Path(journal["archive"])
    _assert_preserved(root, journal["preserved_hashes"])
    check_quiescent(root, rows_provider())
    old_copy, staged, live = archive / "code", archive / "staged_code", root / "code"
    if live.exists():
        live_sha = sha(live / "release.json")
        if live_sha == journal["old_release_sha256"]:
            _code_check(live, read(live / "release.json"))
            if old_copy.exists():
                raise ValueError("Both old live and archived code exist; refuse ambiguous state")
            os.replace(live, old_copy)
        elif live_sha != journal["new_release_sha256"]:
            raise ValueError("Live code is neither the old nor the staged release")
    if not old_copy.exists() or sha(old_copy / "release.json") != journal["old_release_sha256"]:
        raise ValueError("Missing verified old-release archive")
    _code_check(old_copy, read(old_copy / "release.json"))
    if not live.exists():
        if sha(staged / "release.json") != journal["new_release_sha256"]:
            raise ValueError("Staged release changed before directory swap")
        _code_check(staged, read(staged / "release.json"))
        os.replace(staged, live)
    _code_check(live, read(live / "release.json"))
    for token in journal["identity_tokens"]:
        rr = root / "regions" / token
        path = rr / "manifests/identity.json"
        before = read(archive / "region_identities" / (token + ".json"))
        current = read(path)
        expected = dict(before, campaign_release_sha256=journal["new_release_sha256"])
        if current not in (before, expected):
            raise ValueError("Region identity changed beyond release binding: " + token)
        _verify_region(root, rr, current, {journal["old_release_sha256"], journal["new_release_sha256"]})
        if current != expected:
            atomic(path, expected)
        _verify_region(root, rr, read(path), {journal["new_release_sha256"]})
    _assert_preserved(root, journal["preserved_hashes"])
    verify_release(root)
    completed = dict(journal, phase="COMPLETE", completed=now(),
                     region_identities_rebound=len(journal["identity_tokens"]),
                     science_and_campaign_state_unchanged=True)
    atomic(archive / "migration.json", completed)
    atomic(root / "reports/operational_upgrade.json", completed)
    atomic(root / "state/operational_upgrade.json", completed)
    return completed


def upgrade(source, root, commit, slurm_rows=None):
    source, root = Path(source).resolve(), Path(root).resolve()
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        raise ValueError("New Git commit must be a hexadecimal commit identifier")
    if source.is_relative_to(root / "code"):
        raise ValueError("Run upgrade from an external checkout, not the live code tree")
    rows_provider = active_jobs if slurm_rows is None else (
        slurm_rows if callable(slurm_rows) else lambda: slurm_rows)
    with contextlib.ExitStack() as locks:
        for name in ("upgrade", "controller", "scanner", "submit", "campaign"):
            locks.enter_context(locked(root / "locks" / (name + ".lock"), False))
        check_quiescent(root, rows_provider())
        journal_path = root / "state/operational_upgrade.json"
        if journal_path.exists():
            journal = read(journal_path)
            if journal["phase"] == "COMPLETE":
                if journal["new_commit"] == commit:
                    verify_release(root)
                    _assert_preserved(root, journal["preserved_hashes"])
                    return journal
                # Keep a complete historical receipt in its own archive.
                journal = _prepare(source, root, commit)
            else:
                if journal["new_commit"] != commit or journal["source_hashes"] != _source_files(source):
                    raise ValueError("An interrupted upgrade must resume with the same frozen source/commit")
        else:
            journal = _prepare(source, root, commit)
        return _finish(root, journal, rows_provider)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    print(json.dumps(upgrade(args.source, args.root, args.commit), sort_keys=True))
