"""All-293-region update: retain raw outputs and annotate near-zero QA failures.

This is an explicit, dataset-specific inclusion decision, not a rewrite of the
strict QA checks or a claim that every physical process has been validated.
"""
from __future__ import annotations

import argparse
import datetime as dt
from email.message import EmailMessage
from email.policy import SMTP
import json
from pathlib import Path
import shutil
import zipfile

import numpy as np
import pandas as pd

from . import findings_report as fr
from . import report
from .io import file_hash
from .parallel_report import merge_pdfs

NAME = "report_findings_v3_all293"
LIMITS = {"evapotranspiration": -1e-6, "TWS": -1e-5}
KEYS = ["region_id", "variable", "component", "group_type", "group"]
BATCH = Path("/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/shift_v3_60km")


def allowed_note(check, minimum):
    """Only documented tiny water/ET negatives qualify; other failures block."""
    scenario, variable, component, test = check.split("/")
    return (scenario in fr.SCENARIOS and test == "nonnegative"
            and ((variable == "evapotranspiration" and component == variable)
                 or (variable == "TWS" and component.startswith("soil_")))
            and np.isfinite(minimum) and LIMITS[variable] <= minimum < 0)


def reduce_region(frame):
    d = frame[frame.complete_period & frame.year.isin(fr.YEARS)].copy()
    assert np.isfinite(d["mean"]).all() and (d.valid_area_m2 > 0).all()
    keys = KEYS + ["year", "aggregation", "units"]
    paired = d.pivot(index=keys, columns="scenario", values="mean")
    areas = d.pivot(index=keys, columns="scenario", values="valid_area_m2")
    assert np.isfinite(paired[list(fr.SCENARIOS)]).all().all()
    np.testing.assert_allclose(areas.No_Fire, areas.VIIRS_Fire, rtol=1e-9, atol=.01)
    paired["difference"] = paired.VIIRS_Fire - paired.No_Fire
    paired["area_m2"] = areas.No_Fire
    paired = paired.reset_index()
    groups = paired.groupby(KEYS + ["aggregation", "units"], observed=True)
    assert (groups.year.nunique() == 9).all()
    spread = groups.area_m2.agg(["min", "max"])
    np.testing.assert_allclose(spread["min"], spread["max"], rtol=1e-8, atol=.01)
    values = groups[[*fr.SCENARIOS, "difference", "area_m2"]].mean().reset_index()
    values["n_years"] = 9
    return values, paired[paired.group_type.eq("overall")].copy()


def summaries(values):
    result = []
    for (v, c, a, u), d in values.groupby(["variable", "component", "aggregation", "units"], observed=True):
        s = fr.weighted_summary(d)
        s["variable"], s["component"], s["aggregation"], s["units"] = v, c, a, u
        result.append(s)
    return pd.concat(result, ignore_index=True)


def prepare(root):
    root = Path(root); out = root / NAME; out.mkdir(parents=True, exist_ok=True)
    inventory = json.loads((root / "inventory.json").read_text())
    ids = sorted(x["region_id"] for x in inventory["regions"])
    assert len(ids) == len(set(ids)) == 293
    old = fr.snapshot(root); old_ids = {Path(p).name for p in old["regions"]}
    qa_path = root / "report_parallel_v1/qa_checks.csv"
    qa = pd.read_csv(qa_path)
    assert set(qa.region_id) == set(ids)
    failed = qa[qa.status.ne("pass")]
    ledger, sources, values, annuals, profiles = [], [], [], [], []
    for k, rid in enumerate(ids, 1):
        directory = root / "regions" / rid
        assert (directory / "completion.json").is_file()
        profile = pd.read_csv(directory / "profile.csv")
        assert profile.nan.sum() == 0 and profile.inf.sum() == 0
        profiles.append(profile)
        checks = failed[failed.region_id.eq(rid)]
        notes = []
        for row in checks.itertuples():
            scenario, v, c, test = row.check.split("/")
            p = profile[profile.scenario.eq(scenario) & profile.variable.eq(v) & profile.component.eq(c)]
            assert len(p) == 1
            minimum = float(p["min"].iloc[0])
            assert allowed_note(row.check, minimum), (rid, row.check, minimum)
            notes.append(dict(check=row.check, minimum=minimum, count=int(row.failures)))
        ledger.append(dict(region_id=rid, included=True, previous_primary=rid in old_ids,
                           status="included_with_numerical_note" if notes else "included",
                           strict_failed_checks=len(notes), strict_checks_preserved=True,
                           numerical_notes=json.dumps(notes)))
        path = directory / "annual.csv"
        f = pd.read_csv(path)
        assert set(f.region_id) == {rid}
        reduced, annual = reduce_region(f)
        assert len(reduced[reduced.group_type.eq("overall")]) == 58
        values.append(reduced); annuals.append(annual)
        sources.append(dict(region_id=rid, annual=str(path), annual_sha256=file_hash(path),
                            profile_sha256=file_hash(directory / "profile.csv"),
                            qa_sha256=file_hash(directory / "qa.json")))
        if k % 30 == 0: print(f"Recomputed {k}/293 region summaries", flush=True)
    ledger = pd.DataFrame(ledger)
    assert ledger.status.eq("included_with_numerical_note").sum() == 46
    assert set(ledger.loc[~ledger.previous_primary, "region_id"]) == set(failed.region_id)
    ledger.to_csv(out / "inclusion_ledger_293.csv", index=False)
    v = pd.concat(values, ignore_index=True); a = pd.concat(annuals, ignore_index=True)
    v.to_parquet(out / "all_components_region_means.parquet", index=False)
    a.to_parquet(out / "all_components_overall_annual.parquet", index=False)
    summary = summaries(v); summary.to_csv(out / "all_components_group_summary_293.csv", index=False)
    baseline = summaries(v[v.region_id.isin(old_ids)])
    baseline.to_csv(out / "comparison_group_summary_247.csv", index=False)
    # Independently reconcile the old cohort before claiming a changed population.
    previous = pd.read_csv(root / "report_findings_v2/primary_findings_by_group.csv")
    keys = ["variable", "component", "group_type", "group"]
    match = previous.merge(baseline, on=keys, suffixes=("_old", "_recomputed"), validate="one_to_one")
    assert len(match) == len(previous)
    for name in ["No_Fire", "VIIRS_Fire", "difference", "area_m2"]:
        np.testing.assert_allclose(match[name + "_old"], match[name + "_recomputed"], rtol=1e-8, atol=1e-7)
    comparison = summary.merge(baseline, on=keys, suffixes=("_293", "_247"), validate="one_to_one")
    comparison.to_csv(out / "inclusion_sensitivity_247_vs_293.csv", index=False)
    pp = pd.concat(profiles, ignore_index=True)
    neg = []
    for variable in ["cEco", "c_fire_efflux", "evapotranspiration", "TWS"]:
        p = pp[pp.variable.eq(variable)]
        neg.append(dict(variable=variable, regions_with_negative=p[p.negative.gt(0)].region_id.nunique(),
                        minimum=float(p["min"].min()), negative_samples=int(p.negative.sum())))
    pd.DataFrame(neg).to_csv(out / "negative_value_summary.csv", index=False)
    carbon = a[a.component.isin(["nee", "gpp", "eco_respiration", "fire_carbon_emission"])]
    cc = carbon.pivot(index=["region_id", "year"], columns="component", values="difference")
    cc["net_atmospheric_fire_contribution"] = cc.nee + cc.fire_carbon_emission
    cc.reset_index().to_csv(out / "annual_fire_contributions_293.csv", index=False)
    evidence = dict(created_utc=dt.datetime.now(dt.timezone.utc).isoformat(), included_regions=293,
        previous_primary_regions=247, included_with_numerical_note=46, excluded_regions=0,
        policy="User-requested inclusion of documented near-zero soil-water/ET negatives; raw data and strict QA unchanged",
        note_lower_bounds=LIMITS, bound_scope="Dataset-specific inclusion guardrails; not universal physical tolerances",
        years=list(fr.YEARS), components=58, negative_values=neg, source_inventory_sha256=file_hash(root / "inventory.json"),
        strict_qa_sha256=file_hash(qa_path), old_prepared_sha256=file_hash(root / "report_parallel_v1/prepared.json"),
        old_primary_reconciliation="pass", sources=sources,
        negative_net_contribution_region_years=int(cc.net_atmospheric_fire_contribution.lt(-.01).sum()),
        weighting="Owned natural-vegetation area within this interim cohort; not Mediterranean design-weighted totals")
    (out / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return {k: evidence[k] for k in ["included_regions", "included_with_numerical_note", "components"]}


def plots(root):
    root = Path(root); out = root / NAME
    v = pd.read_parquet(out / "all_components_region_means.parquet")
    summary = pd.read_csv(out / "all_components_group_summary_293.csv")
    plt = report._set_style()
    fig, axes = plt.subplots(2, 3, figsize=(13.6, 8), gridspec_kw={"height_ratios": [2.1, 1.3]})
    examples = [("gpp", "GPP", "g C m⁻² yr⁻¹"),
                ("evapotranspiration", "Evapotranspiration", "mm yr⁻¹"),
                ("cVeg", "Mean vegetation carbon", "g C m⁻²")]
    for j, (c, label, units) in enumerate(examples):
        d = v[v.component.eq(c) & v.group_type.eq("overall")].sort_values("region_id")
        assert len(d) == 293
        s = summary[summary.component.eq(c) & summary.group_type.eq("overall")].iloc[0]
        top, bottom = axes[:, j]
        for x, scenario, color in zip([0, 1], fr.SCENARIOS, fr.COLORS):
            fr.draw_violin(top, d[scenario], x, color, width=.65)
        top.set_xticks([0, 1], ["No fire", "VIIRS fire"])
        top.set_ylabel(units); top.set_title(f"{chr(65+j)}  {label}", loc="left")
        top.text(.02, .98, f"293 cubes\nArea-weighted mean: {s.No_Fire:,.1f} → {s.VIIRS_Fire:,.1f}",
                 transform=top.transAxes, va="top", fontsize=9,
                 bbox=dict(facecolor="white", edgecolor="none", alpha=.85))
        fr.draw_violin(bottom, d.difference, 0, fr.COLORS[1], width=.65)
        bottom.axhline(0, color="#6d7b83", lw=.8)
        bottom.set_xlim(-.8, .8); bottom.set_xticks([0], ["VIIRS fire − no fire"])
        bottom.set_ylabel(units)
        bottom.set_title(f"{chr(68+j)}  Paired response · weighted mean {s.difference:+.2f}", loc="left", fontsize=10)
        for ax in [top, bottom]: ax.grid(axis="y", alpha=.17); ax.set_axisbelow(True)
    fig.suptitle("All 293 regions included: compare the same cubes with and without fire",
                 x=.065, y=.98, ha="left", fontsize=17, fontweight="semibold")
    fig.text(.065, .927, "Each point is one cube’s 2015–2023 mean of annual values; paired differences expose responses hidden by regional variation.", fontsize=10)
    fig.text(.065, .025, "46 regions retained with numerical QA notes: soil water ≥ −3.82×10⁻⁶ mm; ET ≥ −5.64×10⁻⁸ mm/day. No negative carbon stocks found.\nRaw values are unchanged. Boxes show median/IQR; violins show between-cube distributions, not uncertainty. Stocks are not accumulated.\nOwned natural-vegetation support; interim 293-region cohort, not an estimate of the whole Mediterranean. Case studies on the next pages are illustrative.", fontsize=8.4)
    fig.subplots_adjust(top=.86, bottom=.16, left=.065, right=.98, wspace=.32, hspace=.42)
    fr.savefig(fig, out / "00_all293_overview")
    appendix = []
    for c, _, _ in examples:
        d = v[v.component.eq(c)].copy()
        item = dict(component=c, variable=d.variable.iloc[0])
        u = str(d.units.iloc[0]).replace("g C m-2", "g C m⁻²") + (" yr⁻¹" if d.aggregation.iloc[0] == "sum" else "")
        for family in ["pyrome", "vegetation"]:
            f = fr.absolute_figure(d, item, u, family)
            stem = out / "groups" / f"{c}_{family}_293"
            fr.savefig(f, stem); appendix.append(stem.with_suffix(".pdf"))
    merge_pdfs(appendix, out / "GROUPED_GPP_ET_CVEG_293_EN.pdf")
    cases = root / "report_findings_v2/email_three_findings_v1"
    names = json.loads((cases / "evidence.json").read_text())["figures"]
    for name in names:
        for ext in [".pdf", ".png"]: shutil.copyfile(cases / (name + ext), out / (name + ext))
    merge_pdfs([out / "00_all293_overview.pdf", *[out / (n + ".pdf") for n in names]],
               out / "THREE_RESEARCH_FINDINGS_ALL293_EN.pdf")
    # Individual cases remain identical: none was part of the former excluded 46.
    return dict(main_pages=4, grouped_pages=6, cases_reused_unchanged=True)


def email(root):
    root = Path(root); out = root / NAME
    states = [json.loads(p.read_text()) for p in (BATCH / "state/regions").glob("*.json")]
    assert len(states) == 400
    completed = [q for q in states if q["status"] == "complete"]
    assert all(Path(q["final_path"]).is_file() for q in completed)
    n = len(completed)
    # The forecast is explicitly linked to the recent measured production audit.
    previous_audit = root / "report_findings_v2/email_update_20261105_v2/evidence.json"
    progress = json.loads(previous_audit.read_text())
    eta = ("The remaining 16 are provisionally expected within 2–4 days, subject to I/O and final checks."
           if n == 384 else "The remaining cubes are still being processed; the completion estimate needs refreshing.")
    draft = f"""Subject: SINDBAD–WISP: progress and preliminary findings

Hi Nuno, hi Sujan,

**Progress:** {n}/400 cubes are complete. {eta}

I have updated the analysis of your **WISP_V1_202610** experiments to include **all 293 completed regions**. The attached four-page PDF contains an overview and three examples for discussion:

- **Sétif:** mean annual GPP changes by only −1.0%, while end-2023 vegetation carbon is 13.4% lower with fire.
- **Portugal:** adding combustion changes the 2016 VIIRS-fire carbon balance from a sink to a source (−25.3 to +8.2 g C m⁻² yr⁻¹).
- **Nile Delta:** both scenarios strongly underestimate reference GPP and ET, suggesting a baseline-model issue to investigate.

Portugal also has a **negative net fire contribution in 2018** (−2.7 g C m⁻²): lower respiration outweighs reduced uptake plus combustion. Direct combustion remains positive, and the cumulative 2015–2023 contribution is positive.

The 46 previously excluded regions are now included with numerical QA notes: their negative soil-water/ET values are tiny (about 10⁻⁶ mm / 10⁻⁸ mm day⁻¹). No negative carbon stocks were found. These are preliminary model findings, not confirmed observations.

I am preparing the online report for joint revision and will continue analysing tomorrow. Hope to discuss more. :)

Best,\nYuchen
"""
    (out / "SHORT_EMAIL_EN.md").write_text(draft)
    plain = draft.replace("**", "")
    (out / "SHORT_EMAIL_EN.txt").write_text(plain)
    import markdown
    body = markdown.markdown(draft)
    html = ('<!doctype html><html><head><meta charset="utf-8"></head><body style="font-family:Arial,sans-serif;'
            'font-size:15px;line-height:1.5;color:#24313a;max-width:760px;margin:24px auto;">' + body + '</body></html>')
    (out / "SHORT_EMAIL_EN.html").write_text(html)
    msg = EmailMessage(policy=SMTP)
    msg["Subject"] = "SINDBAD–WISP: progress and preliminary findings"
    msg["X-Unsent"] = "1"
    msg.set_content(plain.split("\n", 1)[1].lstrip()); msg.add_alternative(html, subtype="html")
    pdf = out / "THREE_RESEARCH_FINDINGS_ALL293_EN.pdf"
    msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name)
    (out / "SHORT_EMAIL_DRAFT.eml").write_bytes(msg.as_bytes())
    (out / "email_status_snapshot.json").write_text(json.dumps(dict(
        checked_utc=dt.datetime.now(dt.timezone.utc).isoformat(), completed=n, target=400,
        forecast_source=str(previous_audit), forecast_as_of=progress["as_of_utc"],
        word_count=len(plain.split()), sent=False), indent=2) + "\n")
    return dict(words=len(plain.split()), completed=n, sent=False)


def package(root):
    from pypdf import PdfReader
    from email.parser import BytesParser
    from email import policy
    root = Path(root); out = root / NAME
    assert len(PdfReader(out / "THREE_RESEARCH_FINDINGS_ALL293_EN.pdf").pages) == 4
    assert len(PdfReader(out / "GROUPED_GPP_ET_CVEG_293_EN.pdf").pages) == 6
    assert "293" in PdfReader(out / "THREE_RESEARCH_FINDINGS_ALL293_EN.pdf").pages[0].extract_text()
    message = BytesParser(policy=policy.default).parsebytes((out / "SHORT_EMAIL_DRAFT.eml").read_bytes())
    assert message["To"] is None and message["X-Unsent"] == "1"
    assert len(list(message.iter_attachments())) == 1
    evidence = json.loads((out / "evidence.json").read_text())
    assert file_hash(root / "report_parallel_v1/qa_checks.csv") == evidence["strict_qa_sha256"]
    assert file_hash(root / "report_parallel_v1/prepared.json") == evidence["old_prepared_sha256"]
    docs = Path(__file__).resolve().parents[1] / "docs"
    shutil.copyfile(docs / "ALL293_INCLUSION_UPDATE_EN.md", out / "README_EN.md")
    case_root = root / "report_findings_v2/email_three_findings_v1"
    case_evidence = json.loads((case_root / "evidence.json").read_text())
    case_identity = {}
    for name in case_evidence["figures"]:
        source = case_root / (name + ".pdf")
        assert file_hash(source) == file_hash(out / source.name)
        case_identity[name] = dict(source=str(source), sha256=file_hash(source))
    shutil.copyfile(case_root / "evidence.json", out / "original_case_evidence.json")
    files = ["SHORT_EMAIL_EN.md", "SHORT_EMAIL_EN.txt", "SHORT_EMAIL_EN.html", "SHORT_EMAIL_DRAFT.eml",
             "THREE_RESEARCH_FINDINGS_ALL293_EN.pdf", "GROUPED_GPP_ET_CVEG_293_EN.pdf",
             "inclusion_ledger_293.csv", "negative_value_summary.csv", "email_status_snapshot.json",
             "README_EN.md", "original_case_evidence.json"]
    with zipfile.ZipFile(out / "EMAIL_AND_FIGURES_ALL293.zip", "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name in files: z.write(out / name, arcname=name)
    with zipfile.ZipFile(out / "EMAIL_AND_FIGURES_ALL293.zip") as z: assert z.testzip() is None
    record = dict(status="pass", included=293, prior_strict_flags_retained=46, raw_values_modified=False,
                  old_reports_modified=False, main_pdf_pages=4, group_pdf_pages=6, sent=False,
                  unchanged_case_figures=case_identity,
                  code_sha256=file_hash(Path(__file__)), artifacts={n: file_hash(out / n) for n in files})
    (out / "delivery_validation.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    p.add_argument("stage", choices=["prepare", "plots", "email", "package"])
    args = p.parse_args()
    print(json.dumps(globals()[args.stage](args.root)))
