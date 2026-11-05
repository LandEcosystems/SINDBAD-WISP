"""Restartable component plots and concise reports from unchanged analysis tables.

Workers own separate directories. Only the final merger publishes the report;
no worker appends to a shared PDF. Existing serial plots can be reused after
their paired values and exported files have been checked.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from . import report as old
from .io import canonical_hash, file_hash
from .registry import file_identity

POLICY = "parallel-components-concise-main-v1"
MAIN_COMPONENTS = (("gpp", "gpp"), ("nee", "nee"),
                   ("evapotranspiration", "evapotranspiration"),
                   ("cEco", "cVeg"), ("cEco", "cEcosystem"),
                   ("c_fire_efflux", "fire_carbon_emission"))


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def destination(root):
    return Path(root) / "report_parallel_v1"


@contextmanager
def exclusive(directory):
    lock = directory / "worker.lockdir"
    lock.mkdir()  # Atomic across nodes; intentionally do not use flock.
    try:
        (lock / "owner.json").write_text(json.dumps({"pid": os.getpid(), "job": os.environ.get("SLURM_JOB_ID"), "utc": utc()}))
        yield
    finally:
        (lock / "owner.json").unlink()
        lock.rmdir()


def _identities(root, regions):
    paths = [Path(root) / "inventory.json"]
    for region in regions:
        for name in ("completion.json", "qa.json", "annual.csv", "monthly.csv", "spatial.npz", "recovery.csv", "environment.csv"):
            path = region / name
            if path.exists():
                paths.append(path)
        paths.extend(p for p in (region / "references").glob("*") if p.is_file())
    return [file_identity(p) for p in sorted(paths)]


def prepare(root):
    """One read/partition pass; plotting workers never scan all monthly CSVs."""
    import pandas as pd
    root, out = Path(root), destination(root)
    out.mkdir(exist_ok=True)
    snapshot = root / "inventory.json"
    regions = old.completed_regions(root, snapshot)
    identities = _identities(root, regions)
    fingerprint = canonical_hash({"policy": POLICY, "inputs": identities,
        "code": {p.name: file_hash(p) for p in (Path(__file__), Path(old.__file__))}})
    complete = out / "prepared.json"
    if complete.exists():
        saved = old.read_json(complete)
        if saved["fingerprint"] != fingerprint:
            raise ValueError("Prepared report identity changed; use a new output version")
        return saved
    with exclusive(out):
        rows, checks = old.qa_summary(regions)
        old._write_rows(out / "qa_checks.csv", rows)
        accepted, quarantined = [], []
        for region in regions:
            qa = old.read_json(region / "qa.json", {})
            failed = [x for x in qa.get("checks", []) if x.get("status") == "fail"]
            if qa.get("status") not in ("pass", "warning") or failed:
                quarantined.append({"region_id": region.name, "status": qa.get("status", "missing"), "failed_checks": failed})
            else:
                accepted.append(region)
        if not accepted:
            raise ValueError("No scientifically accepted regions; retain QA evidence without generating primary plots")
        old._write_rows(out / "quarantined_regions.csv", quarantined)
        old._write_rows(out / "reference_region_coverage.csv", old.reference_region_coverage(root, {p.name for p in accepted}))
        annual = old._load_annual(accepted)
        monthly = old._monthly_compact(accepted)
        monthly.to_csv(out / "monthly_area_weighted_summary.csv", index=False)
        cache = out / "prepared"; cache.mkdir(exist_ok=True)
        components = []
        # Retain the established order, independent of effects or reference fit.
        for variable, component in annual[["variable", "component"]].drop_duplicates().itertuples(index=False, name=None):
            variable, component = str(variable), str(component)
            token = old.slug(variable + "__" + component)
            frame = annual[annual.variable.eq(variable) & annual.component.eq(component)].copy()
            cycle = monthly[monthly.variable.eq(variable) & monthly.component.astype(str).eq(component)].copy() if not monthly.empty else monthly
            a, m = cache / (token + "_annual.parquet"), cache / (token + "_monthly.parquet")
            frame.to_parquet(a, index=False); cycle.to_parquet(m, index=False)
            components.append({"index": len(components), "variable": variable, "component": component, "token": token,
                               "annual": str(a), "monthly": str(m), "annual_sha256": file_hash(a), "monthly_sha256": file_hash(m)})
        groups = [{"family": str(f), "group": str(g)} for f, g in
                  annual.loc[annual.group_type.isin(("pyrome", "vegetation")), ["group_type", "group"]].drop_duplicates().itertuples(index=False, name=None)]
        groups.sort(key=lambda x: (x["family"], x["group"]))
        tasks = ([{"kind": "component", "component_index": i} for i in range(len(components))] +
                 [{"kind": "group", **g} for g in groups] + [{"kind": "science"}, {"kind": "references"}])
        for index, task in enumerate(tasks):
            task["index"] = index
        result = dict(policy=POLICY, fingerprint=fingerprint, created_utc=utc(), inputs=identities,
                      root=str(root), output=str(out), regions=[str(p) for p in accepted],
                      processed_regions=len(regions), primary_regions=len(accepted), quarantine=quarantined,
                      qa_counts=checks, components=components, groups=groups, tasks=tasks,
                      source_snapshot_fingerprint=old.read_json(snapshot).get("source_metadata_fingerprint"))
        old.atomic_json(complete, result)
        return result


def read_frames(item):
    import pandas as pd
    for kind in ("annual", "monthly"):
        if file_hash(item[kind]) != item[kind + "_sha256"]:
            raise ValueError("Prepared component was changed: " + item["token"])
    return pd.read_parquet(item["annual"]), pd.read_parquet(item["monthly"])


def validated_pdf(path):
    from pypdf import PdfReader
    reader = PdfReader(str(path), strict=True)
    if not reader.pages:
        raise ValueError("Empty PDF: " + str(path))
    for page in reader.pages:
        _ = page.get_contents()
    return len(reader.pages)


def merge_pdfs(paths, target):
    from pypdf import PdfWriter
    paths = list(paths)
    if not paths:
        raise ValueError("No PDFs to merge")
    writer = PdfWriter(); expected = 0
    for path in paths:
        expected += validated_pdf(path)
        writer.append(str(path))
    temporary = target.with_name(target.name + ".partial")
    with temporary.open("wb") as stream:
        writer.write(stream)
    writer.close()
    if validated_pdf(temporary) != expected:
        raise ValueError("Merged PDF page count differs from its inputs")
    temporary.replace(target)
    return expected


def _legacy_pair_matches(legacy, item, frame):
    import pandas as pd
    path = legacy / ("paired_" + item["token"] + ".csv")
    if not path.exists():
        return False
    try:
        current = old._paired(frame)
        order = ["region_id", "year", "group_type", "group"]
        # Compare the exact float64 values after a lossless CSV parse.
        prior = pd.read_csv(path, float_precision="round_trip")
        current = current.copy()
        current.columns.name = None  # CSV does not store the pivot axis name.
        for column in current.columns:
            if str(current[column].dtype) == "category":
                current[column] = current[column].astype(str)
        pd.testing.assert_frame_equal(prior.sort_values(order).reset_index(drop=True),
                                      current.sort_values(order).reset_index(drop=True), check_dtype=False, check_exact=True)
        return True
    except (AssertionError, ValueError, KeyError):
        return False


def _reuse_figure(legacy, subdir, token, out, records, metadata):
    from PIL import Image
    from pypdf.errors import PdfReadError
    source = legacy / "figures" / subdir / token
    try:
        validated_pdf(Path(str(source) + ".pdf"))
        with Image.open(str(source) + ".png") as image:
            image.verify()
    except (OSError, ValueError, PdfReadError):
        return False
    target = out / "figures" / subdir / token; target.parent.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        shutil.copy2(str(source) + "." + ext, str(target) + "." + ext)
    records.append(dict(metadata, png=str(target) + ".png", pdf=str(target) + ".pdf", reused=True,
                        reused_source=str(source), source_pdf_sha256=file_hash(str(source) + ".pdf")))
    return True


def component_task(prepared, task, out):
    from matplotlib.backends.backend_pdf import PdfPages
    item = prepared["components"][task["component_index"]]
    frame, monthly = read_frames(item)
    variable, component, token = item["variable"], item["component"], item["token"]
    regions = list(map(Path, prepared["regions"]))
    paired = old._paired(frame); paired.to_csv(out / ("paired_" + token + ".csv"), index=False)
    metrics = []
    for (family, group), rows in paired.groupby(["group_type", "group"], observed=True):
        metrics.append(dict(variable=variable, component=component, group_type=str(family), group=str(group),
                            grain="region_year_complete", **old.basic_metrics(rows.No_Fire, rows.VIIRS_Fire)))
    old._write_rows(out / "scenario_metrics.csv", metrics)
    legacy = Path(prepared["root"]) / "report"
    reuse = _legacy_pair_matches(legacy, item, frame)
    figures = []
    with PdfPages(out / "new_plots.pdf") as pdf:
        for subdir, kind, draw in (
                ("overall", "scenario_summary", lambda: old._component_figure(frame, monthly, variable + " / " + component, "overall")),
                ("combined", "combined_distribution", lambda: old._combined_figure(frame, variable + " / " + component))):
            metadata = dict(variable=variable, component=component, kind=kind, source="paired_" + token + ".csv")
            if reuse and _reuse_figure(legacy, subdir, token, out, figures, metadata):
                continue
            fig = draw()
            if fig is not None:
                old._save_figure(fig, out / "figures" / subdir / token, pdf, figures, metadata)
        # Exact pixel moments remain based on all owned pixels, not the display sample.
        units = str(frame.units.iloc[0])
        if str(frame.aggregation.iloc[0]) == "sum" and "day" not in units:
            units += " day-1"
        pixel_sources = ["pixel_pairs_" + token + ".csv.gz", "pixel_metrics_" + token + ".json"]
        metadata = dict(variable=variable, component=component, kind="pixel_scenario_comparison", source=pixel_sources[0])
        if (reuse and all((legacy / n).exists() for n in pixel_sources) and
                _reuse_figure(legacy, "pixel_pairs", token, out, figures, metadata)):
            for name in pixel_sources:
                shutil.copy2(legacy / name, out / name)
        else:
            fig = old._pixel_figure(regions, variable, component, variable + " / " + component, units, out, token)
            if fig is not None:
                old._save_figure(fig, out / "figures/pixel_pairs" / token, pdf, figures, metadata)
    pages = merge_pdfs([f["pdf"] for f in figures], out / "component.pdf")
    return dict(component=item, figures=figures, pages=pages, reused_figures=sum(f.get("reused", False) for f in figures))


def group_task(prepared, task, out):
    """Three components per page, preserving each group's scatter and violin."""
    import pandas as pd
    from matplotlib.backends.backend_pdf import PdfPages
    family, group = task["family"], task["group"]
    plt = old._set_style(); metrics, page_index, tables = [], [], []
    partial = out / "GROUP_REPORT_EN.pdf.partial"
    with PdfPages(partial) as pdf:
        for start in range(0, len(prepared["components"]), 3):
            items = prepared["components"][start:start + 3]
            fig, axes = plt.subplots(len(items), 2, figsize=(11.7, 3.8 * len(items) + .8), squeeze=False, layout="constrained")
            for row, item in enumerate(items):
                frame, _ = read_frames(item)
                frame = frame[frame.group_type.eq(family) & frame.group.astype(str).eq(group)]
                paired = old._paired(frame)
                unit = str(frame.units.iloc[0]) if not frame.empty else "not available"
                label = item["variable"] + " / " + item["component"]
                old._plot_scatter(axes[row, 0], paired, unit, label)
                old._plot_distribution(axes[row, 1], paired, unit)
                if not paired.empty:
                    table = paired.copy(); table["variable"] = item["variable"]; table["component"] = item["component"]; tables.append(table)
                    metrics.append(dict(variable=item["variable"], component=item["component"], group_type=family, group=group,
                                        **old.basic_metrics(paired.No_Fire, paired.VIIRS_Fire)))
                page_index.append(dict(page=start // 3 + 1, row=row + 1, variable=item["variable"], component=item["component"]))
            fig.suptitle(f"{family}: {group} | scenario comparisons", fontsize=14)
            fig.supxlabel("Original class-area support; complete region-years. Boxes/violins describe distributions, not confidence intervals. Different rows have their labelled units and scales.", fontsize=8)
            pdf.savefig(fig); plt.close(fig)
    partial.replace(out / "GROUP_REPORT_EN.pdf")
    if tables:
        pd.concat(tables, ignore_index=True).to_csv(out / "paired_group_values.csv.gz", index=False, compression="gzip")
    old._write_rows(out / "scenario_metrics.csv", metrics)
    old._write_rows(out / "component_page_index.csv", page_index)
    return dict(group_type=family, group=group, pdf=str(out / "GROUP_REPORT_EN.pdf"),
                pages=validated_pdf(out / "GROUP_REPORT_EN.pdf"), figures=[])


def science_task(prepared, out):
    import pandas as pd
    from matplotlib.backends.backend_pdf import PdfPages
    annual = pd.concat([read_frames(c)[0] for c in prepared["components"]], ignore_index=True)
    regions = list(map(Path, prepared["regions"])); figures = []
    with PdfPages(out / "SCIENCE_REPORT_EN.pdf") as pdf:
        old._science_figures(annual, regions, out, pdf, figures)
        for variable, component in MAIN_COMPONENTS:
            frame = annual[annual.variable.eq(variable) & annual.component.eq(component)]
            if frame.empty:
                continue
            unit = str(frame.units.iloc[0])
            if str(frame.aggregation.iloc[0]) == "sum" and "day" not in unit:
                unit += " day-1"
            rows = old._region_maps(regions, variable, component)
            token = old.slug(variable + "__" + component)
            old._write_rows(out / ("geography_" + token + ".csv"), rows)
            fig = old._map_figure(rows, variable + " / " + component, unit)
            if fig is not None:
                old._save_figure(fig, out / "figures/maps" / token, pdf, figures,
                                 dict(kind="geographic_centres", variable=variable, component=component, source="geography_" + token + ".csv"))
    return dict(figures=figures, pdf=str(out / "SCIENCE_REPORT_EN.pdf"))


def reference_task(prepared, out):
    from matplotlib.backends.backend_pdf import PdfPages
    figures = []; root = Path(prepared["root"]); accepted = {Path(p).name for p in prepared["regions"]}
    with PdfPages(out / "REFERENCE_REPORT_EN.pdf") as pdf:
        rows = old._reference_figures(root, out, pdf, figures, accepted)
        old._model_grid_reference_figures(root, out, pdf, figures, accepted)
    return dict(figures=figures, reference_comparisons=len(rows), pdf=str(out / "REFERENCE_REPORT_EN.pdf"))


def verify_task(manifest):
    for item in manifest["artifacts"]:
        if file_hash(item["path"]) != item["sha256"]:
            raise ValueError("Task artifact checksum mismatch: " + item["path"])


def worker(root, index):
    out = destination(root); prepared = old.read_json(out / "prepared.json")
    task = prepared["tasks"][index]
    folder = out / "tasks" / f"{index:03d}_{task['kind']}"; folder.mkdir(parents=True, exist_ok=True)
    complete = folder / "complete.json"
    if complete.exists():
        result = old.read_json(complete)
        if result["fingerprint"] != prepared["fingerprint"]:
            raise ValueError("Task belongs to another prepared report")
        verify_task(result)
        return result
    start = time.monotonic()
    with exclusive(folder):
        if task["kind"] == "component":
            result = component_task(prepared, task, folder)
        elif task["kind"] == "group":
            result = group_task(prepared, task, folder)
        elif task["kind"] == "science":
            result = science_task(prepared, folder)
        elif task["kind"] == "references":
            result = reference_task(prepared, folder)
        else:
            raise ValueError("Unknown task")
        artifacts = [dict(path=str(p), sha256=file_hash(p)) for p in sorted(folder.rglob('*'))
                     if p.is_file() and "worker.lockdir" not in p.parts and not p.name.endswith('.partial') and p.name != "complete.json"]
        result.update(task=task, fingerprint=prepared["fingerprint"], completed_utc=utc(),
                      elapsed_seconds=time.monotonic() - start, artifacts=artifacts)
        old.atomic_json(complete, result)
    return result


def select_main_figures(results):
    """Select by scientific topic, never by effect magnitude or significance."""
    chosen = []
    for result in results:
        for f in result.get("figures", []):
            pair = (f.get("variable"), f.get("component")); kind = f["kind"]
            if ((kind == "scenario_summary" and pair in MAIN_COMPONENTS) or
                (kind == "combined_distribution" and pair in (("gpp", "gpp"), ("cEco", "cEcosystem"), ("c_fire_efflux", "fire_carbon_emission"))) or
                kind in ("carbon_flux_decomposition", "fire_aligned_recovery", "stock_endpoints", "common_annual_gpp_comparison") or
                f.get("source") == "common_annual_gpp_pairs.csv.gz" or
                (kind == "block_year_effects" and pair in (("cEco", "cEcosystem"), ("c_fire_efflux", "fire_carbon_emission"))) or
                (kind == "geographic_centres" and pair in (("cEco", "cEcosystem"), ("c_fire_efflux", "fire_carbon_emission"))) or
                (kind == "native_reference_comparison" and f.get("scenario") == "VIIRS_Fire" and f.get("variable") in ("nee", "evapotranspiration"))):
                chosen.append(f)
    return chosen


def merge(root):
    import pandas as pd
    from matplotlib.backends.backend_pdf import PdfPages
    out = destination(root); prepared = old.read_json(out / "prepared.json")
    results = []
    for task in prepared["tasks"]:
        path = out / "tasks" / f"{task['index']:03d}_{task['kind']}" / "complete.json"
        if not path.exists():
            raise ValueError("Required plotting task incomplete: " + str(task["index"]))
        record = old.read_json(path)
        if record["fingerprint"] != prepared["fingerprint"]:
            raise ValueError("Mixed report versions")
        verify_task(record); results.append(record)
    regions = old.completed_regions(Path(root), Path(root) / "inventory.json")
    if _identities(root, regions) != prepared["inputs"]:
        raise ValueError("Analysis inputs changed while plotting; do not publish mixed snapshots")
    selected = select_main_figures(results)
    ref = next(r for r in results if r["task"]["kind"] == "references")
    refdir = Path(ref["pdf"]).parent
    metrics = pd.read_csv(refdir / "reference_metrics_all_pairs.csv")
    metric_lines = []
    for _, row in metrics.loc[metrics.scenario.eq("VIIRS_Fire")].iterrows():
        metric_lines.append(f"{row['reference_product']} / {row['variable']}: bias {row['bias']:.3g}; RMSE {row['rmse']:.3g} {row['units']}; {int(row['n']):,} matched pairs.")
    front = out / "SUMMARY_EN.pdf"
    with PdfPages(front) as pdf:
        old._text_page(pdf, "SINDBAD–WISP | Production, water and fire responses", [
            f"{prepared['processed_regions']} regions processed; {prepared['primary_regions']} pass the primary physical QA gate; {len(prepared['quarantine'])} are flagged and retained separately.",
            "The main report concentrates on six primary quantities. It links to all 58 components and independent pyrome/vegetation atlases rather than repeating every diagnostic in the main reading flow.",
            "No_Fire versus VIIRS_Fire is a model experiment. MODIS, FLUXCOM-X and CCI are reference estimates, not ground truth. This completed subset does not support an unbiased whole-Mediterranean total.",
            "The 2014 model year is partial. Comparable full-year and paired-period statistics keep their documented common support. LAI was not saved and is not reconstructed.",
            "Known reference limitation: MOD16 2014 has ambiguous source versions and is excluded; the audited 2015–2021 years are used."])
        old._text_page(pdf, "Reference agreement | exact matched-support metrics", metric_lines + [
            "Metrics have product-specific units, reference scales and periods; compare sources on common support, not by raw RMSE across unlike units. Spatial and temporal dependence remains.",
            "Group membership uses original area fractions; FLUXNET-compatible parameter classes are a separate diagnostic. Missing comparisons are retained explicitly."])
    main_pages = merge_pdfs([front] + [Path(f["pdf"]) for f in selected], out / "REPORT_EN.pdf")
    component_pdfs = [out / "tasks" / f"{r['task']['index']:03d}_component" / "component.pdf" for r in results if r["task"]["kind"] == "component"]
    appendix_pages = merge_pdfs(component_pdfs, out / "ALL_COMPONENTS_EN.pdf")
    component_metrics = [pd.read_csv(out / "tasks" / f"{r['task']['index']:03d}_component" / "scenario_metrics.csv") for r in results if r["task"]["kind"] == "component"]
    pd.concat(component_metrics, ignore_index=True).to_csv(out / "scenario_metrics.csv", index=False)
    index = []
    page = 1
    for result in results:
        task = result["task"]
        if task["kind"] == "component":
            c = result["component"]
            index.append(dict(variable=c["variable"], component=c["component"], appendix_start_page=page,
                              appendix_pages=result["pages"], task=task["index"], reused_figures=result["reused_figures"]))
            page += result["pages"]
    old._write_rows(out / "component_index.csv", index)
    lines = ["# SINDBAD–WISP: production, water and fire responses", "", "## Executive summary", "",
             f"- **{prepared['processed_regions']} regions analysed; {prepared['primary_regions']} pass primary QA.** The other {len(prepared['quarantine'])} regions remain in the [QA record](quarantined_regions.csv); input outputs were not corrected.",
             "- Fire/no-fire scenario differences and reference-product agreement answer different questions. The main figures show both, with original spatial support and units.",
             "- These are interim results for a nonrandom completed subset. They are not unbiased estimates of all Mediterranean emissions, and no real WISP forecast scenario is present.", "",
             f"[Main report: {main_pages} pages](REPORT_EN.pdf) · [All components: {appendix_pages} pages](ALL_COMPONENTS_EN.pdf) · [Component index](component_index.csv)", "",
             "## Reading guide", "", "Each component has a multi-panel overview, one combined overall/pyrome/vegetation distribution and a pixel-wise scenario comparison. Independent group atlases place three components on each page. Original IGBP and parameter-group numerical metrics remain in [scenario_metrics.csv](scenario_metrics.csv).", "",
             "## Main evidence", ""]
    for figure in selected:
        png = Path(figure["png"]).relative_to(out).as_posix(); pdf = Path(figure["pdf"]).relative_to(out).as_posix()
        owner = next(r for r in results if figure in r.get("figures", []))
        taskdir = out / "tasks" / f"{owner['task']['index']:03d}_{owner['task']['kind']}"
        values = (taskdir / figure["source"]).relative_to(out).as_posix()
        title = figure["kind"].replace("_", " ") + " — " + figure.get("variable", "")
        lines += [f"### {title}", "", f"![{title}]({png})", "", f"[PDF]({pdf}) · [Figure values]({values})", ""]
    lines += ["## Independent group results", ""]
    for result in results:
        if result["task"]["kind"] == "group":
            path = Path(result["pdf"]).relative_to(out)
            lines.append(f"- {result['group_type']} / {result['group']}: [atlas]({path.as_posix()}) · [values]({(path.parent/'paired_group_values.csv.gz').as_posix()}) · [metrics]({(path.parent/'scenario_metrics.csv').as_posix()})")
    lines += ["", "## Reference and scientific appendices", ""]
    for result in results:
        if result["task"]["kind"] in ("science", "references"):
            lines.append(f"- [{result['task']['kind']} appendix]({Path(result['pdf']).relative_to(out).as_posix()})")
    lines += ["", "## Interpretation and limits", ""] + ["- " + s for s in old.SEMANTIC_WARNINGS]
    lines += ["", "Results and uncertainty must be interpreted on their saved support. Model counterfactual differences do not establish observed causal effects. Reference uncertainty, carbon-conversion sensitivity and block/year bootstrap intervals are different quantities. Nonsignificant and unavailable results are retained.", "",
              "All original figures from the interrupted serial job remain available in the legacy report directory. Validated exports were reused when their exact paired values matched. Numeric and reference analyses were not rerun for this presentation change.", ""]
    (out / "REPORT_EN.md").write_text("\n".join(lines))
    state = dict(status="complete", policy=POLICY, fingerprint=prepared["fingerprint"], completed_utc=utc(),
                 processed_regions=prepared["processed_regions"], primary_regions=prepared["primary_regions"],
                 component_count=len(index), main_pages=main_pages, component_appendix_pages=appendix_pages,
                 tasks=len(results), reused_figures=sum(r.get("reused_figures", 0) for r in results),
                 main_figures=selected, source_snapshot_fingerprint=prepared["source_snapshot_fingerprint"],
                 task_manifests=[str(out / "tasks" / f"{r['task']['index']:03d}_{r['task']['kind']}" / "complete.json") for r in results])
    old.atomic_json(out / "report_manifest.json", state)
    return state


def launch(root):
    """Submit independent workers once, then an after-success PDF merger."""
    prepared = prepare(root)
    out = destination(root)
    code = Path(os.environ["ANALYSIS_CODE"])
    logs = Path(root) / "logs"
    ledger = out / "jobs.json"
    jobs = old.read_json(ledger, {"fingerprint": prepared["fingerprint"], "created_utc": utc()})
    if jobs["fingerprint"] != prepared["fingerprint"]:
        raise ValueError("Existing plotting jobs have a different input identity")
    for label, kinds, cpus, memory, parallel in (
            ("component_group", {"component", "group"}, 1, "4G", 32),
            ("science_reference", {"science", "references"}, 2, "16G", 2)):
        if label in jobs:
            continue
        indices = [str(task["index"]) for task in prepared["tasks"] if task["kind"] in kinds]
        command = ["sbatch", "--parsable", "--array=" + ",".join(indices) + "%" + str(parallel),
                   "--cpus-per-task=" + str(cpus), "--mem=" + memory, "--chdir=" + str(code),
                   "--output=" + str(logs / ("plot_" + label + "_%A_%a.log")), str(code / "result_analysis/slurm_plot_task.sh")]
        job = subprocess.check_output(command, text=True).strip().split(";")[0]
        jobs[label] = {"job_id": job, "task_indices": indices, "max_parallel": parallel,
                       "cpus": cpus, "memory": memory, "submitted_utc": utc()}
        old.atomic_json(ledger, jobs)
    if "merge" not in jobs:
        dependency = "afterok:" + jobs["component_group"]["job_id"] + ":" + jobs["science_reference"]["job_id"]
        job = subprocess.check_output(["sbatch", "--parsable", "--dependency=" + dependency,
            "--chdir=" + str(code), "--output=" + str(logs / "plot_merge_%j.log"),
            str(code / "result_analysis/slurm_plot_merge.sh")], text=True).strip().split(";")[0]
        jobs["merge"] = {"job_id": job, "dependency": dependency, "submitted_utc": utc()}
        old.atomic_json(ledger, jobs)
    return jobs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("stage", choices=("prepare", "worker", "merge", "launch"))
    parser.add_argument("--index", type=int)
    args = parser.parse_args()
    if args.stage == "prepare":
        result = prepare(args.root)
    elif args.stage == "worker":
        if args.index is None:
            parser.error("worker requires --index")
        result = worker(args.root, args.index)
    elif args.stage == "merge":
        result = merge(args.root)
    else:
        result = launch(args.root)
    print(json.dumps({k: v for k, v in result.items() if isinstance(v, (str, int, float, bool))}))


if __name__ == "__main__":
    main()
