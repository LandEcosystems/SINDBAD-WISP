"""Four outcomes, overall plus pyrome/vegetation groups, for all 293 regions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd

from . import findings_report as fr
from .io import file_hash
from .parallel_report import merge_pdfs
from .report import _set_style

ITEMS = [
    ("gpp", "gpp", "GPP", "g C m⁻² yr⁻¹"),
    ("cEco", "cVeg", "Vegetation carbon (cVeg)", "g C m⁻²"),
    ("evapotranspiration", "evapotranspiration", "Evapotranspiration (ET)", "mm yr⁻¹"),
    ("c_fire_efflux", "fire_carbon_emission", "Direct fire carbon emission", "g C m⁻² yr⁻¹"),
]
NAME = "report_findings_v3_all293/email_section2_grouped_v2"


def group_order(family):
    return [("overall", "all"), *[(family, g) for g in fr.FAMILIES[family]]]


def distribution(ax, values, position, color, width=.32):
    """Avoid a smoothed distribution for sparse groups or all-zero series."""
    values = np.asarray(values, dtype=float)
    assert np.isfinite(values).all()
    if not len(values): return
    if len(values) >= 10 and np.ptp(values) > max(1e-12, abs(values.mean())*1e-12):
        violin = ax.violinplot([values], positions=[position], widths=width,
                              showextrema=False, bw_method=.25)
        for body in violin["bodies"]:
            body.set_facecolor(color); body.set_edgecolor(color); body.set_alpha(.22)
    ax.boxplot([values], positions=[position], widths=width*.30, showfliers=False,
        patch_artist=True, manage_ticks=False,
        boxprops=dict(facecolor="white", edgecolor=color, lw=1.1),
        medianprops=dict(color="#25313A", lw=1.3),
        whiskerprops=dict(color=color), capprops=dict(color=color))
    jitter = np.random.default_rng(42).uniform(-width*.23, width*.23, len(values))
    ax.scatter(position+jitter, values, s=6, color=color, alpha=.28, linewidths=0, rasterized=True)


def panel(ax, values, family, difference=False, compact=False):
    labels = []
    ax.axvspan(-.49, .49, color="#f0f4f7", zorder=0)
    for k, (f, group) in enumerate(group_order(family)):
        d = values[values.group_type.eq(f)&values.group.eq(group)].sort_values("region_id")
        title = "All cubes" if f == "overall" else "Unknown" if group == "unknown" else group if family == "pyrome" else {
            "forest":"Forest", "shrub":"Shrubland", "savanna":"Savanna", "grass":"Grassland", "wetland":"Wetland"}[group]
        labels.append(f"{title}\nn={len(d)}")
        if difference:
            distribution(ax, d.difference, k, fr.COLORS[1], width=.58)
            if len(d): ax.scatter(k, np.average(d.difference, weights=d.area_m2), marker="D", s=25,
                                  color=fr.COLORS[1], edgecolor="#23323c", linewidth=.6, zorder=5)
        else:
            for s, offset, color in zip(fr.SCENARIOS, [-.19, .19], fr.COLORS):
                distribution(ax, d[s], k+offset, color, width=.33)
                if len(d): ax.scatter(k+offset, np.average(d[s], weights=d.area_m2), marker="D", s=25,
                                      color=color, edgecolor="#23323c", linewidth=.6, zorder=5)
    ax.set_xticks(range(len(labels)), labels, fontsize=8.8 if compact else 10)
    ax.set_xlim(-.55, len(labels)-.45)
    ax.grid(axis="y", color="#e2e7eb", lw=.65); ax.set_axisbelow(True)
    if difference: ax.axhline(0, color="#65737e", lw=.8)
    else:
        # Pad below zero so the genuine all-zero no-fire emission is visible.
        shown = values[values.group_type.isin(["overall", family])]
        maximum = float(shown[list(fr.SCENARIOS)].to_numpy().max())
        ax.set_ylim(-.025*maximum, 1.09*maximum)


def legend(fig):
    from matplotlib.patches import Patch
    from matplotlib.lines import Line2D
    fig.legend(handles=[Patch(facecolor=c, alpha=.5, edgecolor=c, label=s.replace("_", " "))
                        for s,c in zip(fr.SCENARIOS, fr.COLORS)] +
                       [Line2D([], [], marker="D", ls="none", color="#344957", ms=5, label="Area-weighted mean")],
               loc="upper right", bbox_to_anchor=(.98,.925), frameon=False, ncol=3, fontsize=10)


def caption(fig, fontsize=8.4, y=.025):
    fig.text(.075, y,
        "Each dot = one cube’s 2015–2023 mean annual value on its owned natural-vegetation support. Boxes: median/IQR; diamonds: area-weighted means.\n"
        "Fluxes use annual totals; cVeg uses annual mean stocks. A mixed cube can appear in several groups, using only that group’s pixels; All cubes counts each once.\n"
        "All 293 regions included; 46 near-zero water/ET notes retained. These are interim cohort comparisons, not Mediterranean-wide totals or confidence intervals.\n"
        "Direct fire emission sums the eight combustion pools; No Fire is exactly zero. It is carbon mass, not net NEE or separately resolved CO₂. Groups with n<10 show points/boxes only.",
        fontsize=fontsize)


def build(root):
    from pypdf import PdfReader
    root = Path(root); out = root / NAME; out.mkdir(parents=True, exist_ok=True)
    source = root / "report_findings_v3_all293/all_components_region_means.parquet"
    stats_source = root / "report_findings_v3_all293/all_components_group_summary_293.csv"
    data = pd.read_parquet(source); previous = pd.read_csv(stats_source)
    assert json.loads((root / "report_findings_v3_all293/evidence.json").read_text())["included_regions"] == 293
    subsets = []; stats = []
    for variable, component, title, unit in ITEMS:
        d = data[data.variable.eq(variable)&data.component.eq(component)&data.group_type.isin(["overall","pyrome","vegetation"])].copy()
        assert d[d.group_type.eq("overall")].region_id.nunique() == 293
        assert (d.n_years==9).all() and np.isfinite(d[[*fr.SCENARIOS,"difference","area_m2"]]).all().all()
        assert not d.duplicated(["region_id","group_type","group"]).any()
        np.testing.assert_allclose(d.VIIRS_Fire-d.No_Fire, d.difference, rtol=1e-8, atol=1e-8)
        if component == "fire_carbon_emission":
            assert (d.No_Fire == 0).all() and (d.VIIRS_Fire >= 0).all()
        summary = fr.weighted_summary(d)
        summary["variable"], summary["component"], summary["display_units"] = variable, component, unit
        existing = previous[previous.variable.eq(variable)&previous.component.eq(component)]
        match = summary.merge(existing,on=["variable","component","group_type","group"],suffixes=("_new","_old"),validate="one_to_one")
        assert len(match) == len(summary)
        for column in ["No_Fire", "VIIRS_Fire", "difference", "area_m2"]:
            np.testing.assert_allclose(match[column+"_new"],match[column+"_old"],rtol=1e-10,atol=1e-8)
        overall_area = summary[summary.group_type.eq("overall")].area_m2.iloc[0]
        for family in ["pyrome", "vegetation"]:
            np.testing.assert_allclose(summary[summary.group_type.eq(family)].area_m2.sum(),overall_area,rtol=1e-10,atol=.1)
        subsets.append(d);stats.append(summary)
    all_values = pd.concat(subsets,ignore_index=True)
    all_values.to_csv(out/"plotted_region_group_values.csv",index=False)
    pd.concat(stats,ignore_index=True).to_csv(out/"group_statistics.csv",index=False)
    plt = _set_style(); overview_pages=[]; detailed=[]; figure_records=[]
    for index, family in enumerate(["pyrome", "vegetation"],1):
        name = "pyromes" if family=="pyrome" else "vegetation types"
        fig, axes = plt.subplots(2,2,figsize=(16,11.5))
        fig.suptitle(f"Overall and {name}: four outcomes across 293 cubes",x=.075,y=.982,ha="left",fontsize=19,weight="semibold")
        fig.text(.075,.932,"Paired No Fire / VIIRS Fire comparisons · complete years 2015–2023",fontsize=10.5)
        legend(fig)
        for k, (ax, item, d) in enumerate(zip(axes.flat, ITEMS, subsets)):
            _,component,title,unit=item
            panel(ax,d,family,compact=True)
            ax.set_title(f"{chr(65+k)}  {title}",loc="left",pad=10,fontsize=13)
            ax.set_ylabel(unit)
        caption(fig,fontsize=8.8,y=.023)
        fig.subplots_adjust(left=.075,right=.98,top=.87,bottom=.15,wspace=.22,hspace=.37)
        stem = out/f"Figure_{index}_overall_and_{family}_four_features"
        fr.savefig(fig,stem);overview_pages.append(stem.with_suffix(".pdf"))
        for item,d in zip(ITEMS,subsets):
            variable,component,title,unit=item
            fig,axes=plt.subplots(2,1,figsize=(13.8,8.8),gridspec_kw={"height_ratios":[2.3,1.3]},sharex=True)
            panel(axes[0],d,family);panel(axes[1],d,family,difference=True)
            axes[0].set_title("Absolute values in the two scenarios",loc="left",fontsize=11)
            axes[0].set_ylabel(unit);axes[1].set_ylabel(f"VIIRS fire − no fire\n({unit})")
            axes[1].set_title("Paired differences within the same cubes" if component!="fire_carbon_emission" else
                              "No-fire combustion is zero: the difference equals VIIRS-fire emissions",loc="left",fontsize=10)
            fig.suptitle(f"{title}: overall and {name}",x=.075,y=.98,ha="left",fontsize=17,weight="semibold")
            legend(fig);caption(fig,fontsize=8.1,y=.02)
            fig.subplots_adjust(left=.09,right=.98,top=.855,bottom=.20,hspace=.31)
            stem=out/f"{family}_{component}_comparison"
            fr.savefig(fig,stem);detailed.append(stem.with_suffix(".pdf"))
            figure_records.append(dict(family=family,variable=variable,component=component,units=unit,
                                       overall_regions=293,pdf=stem.with_suffix('.pdf').name,png=stem.with_suffix('.png').name))
    assert merge_pdfs(overview_pages,out/"SECTION2_OVERALL_PYROME_VEGETATION_EN.pdf")==2
    assert merge_pdfs(detailed,out/"SECTION2_ALL_EIGHT_COMPARISONS_EN.pdf")==8
    text=("2. Overall comparisons by pyrome and vegetation type\n\n"
          "Figures 1–2 compare GPP, vegetation carbon (cVeg), ET and direct fire carbon emissions across all 293 cubes and within each pyrome or vegetation group. "
          "No_Fire and VIIRS_Fire are shown side by side, with detailed paired differences in the supplementary figures. "
          "Each point represents one cube’s 2015–2023 mean annual value; group statistics use only the corresponding pixels within each cube. "
          "Fire emissions are direct combustion, separate from the net carbon contributions discussed in the case studies.\n")
    (out/"SECTION2_EMAIL_TEXT_EN.txt").write_text(text)
    notes=("All four outcomes are available for BOTH grouping schemes; every chart includes the same overall 293-cube baseline.\n"
           "Pyromes: FCS, FIL, ICS, RCS, RIL, unknown. Vegetation: forest, shrubland, savanna, grassland, wetland.\n"
           "Vegetation groups use the original 2019 IGBP classification, not FLUXNET-compatible model recoding: 1–5 forest; 6–7 shrub; 8–9 savanna; 10 grass; 11 wetland.\n"
           "Groups use owned class area, not a single dominant label assigned to an entire cube. Therefore group counts can sum above 293 while owned group areas sum to the overall area.\n"
           "Each dot is one region/group nine-year mean. Distributions weight cubes equally; diamond markers separately show owned-area-weighted means.\n"
           "GPP, ET and direct fire fluxes are annual totals averaged across 2015–2023. cVeg is the annual mean stock averaged over those years.\n"
           "No-fire direct combustion is exactly zero: the flat blue series is a valid result. Do not interpret fire emission as NEE, net scenario contribution, observed burned area, or gas-speciated CO2.\n"
           "Partial 2014 is excluded. All 293 regions include the previous 46 tiny water/ET numerical QA notes; raw results are unchanged.\n"
           "The two-page overview is for the email; the eight-page version provides one full comparison per outcome per grouping. Axis ranges depend on the outcome and grouping; no values are omitted.\n")
    (out/"FIGURE_NOTES_EN.txt").write_text(notes)
    for pdf in overview_pages+detailed:
        assert len(PdfReader(pdf).pages)==1
    manifest=dict(status="pass",regions=293,features=4,groupings=2,individual_comparisons=8,
                  overview_pdf_pages=2,detailed_pdf_pages=8,source_sha256=file_hash(source),
                  summary_source_sha256=file_hash(stats_source),code_sha256=file_hash(Path(__file__)),
                  area_partition_check="pass",summary_reconciliation="pass",source_outputs_modified=False,
                  figures=figure_records)
    (out/"validation.json").write_text(json.dumps(manifest,indent=2)+'\n')
    members=[p for p in sorted(out.iterdir()) if p.is_file() and p.suffix in ['.pdf','.png','.txt','.csv','.json']]
    with zipfile.ZipFile(out/"SECTION2_FIGURES_AND_DATA.zip","w",compression=zipfile.ZIP_DEFLATED) as z:
        for path in members:z.write(path,arcname=path.name)
    with zipfile.ZipFile(out/"SECTION2_FIGURES_AND_DATA.zip") as z:assert z.testzip() is None
    return dict(status="pass",output=str(out),comparisons=8,overview_pages=2,detailed_pages=8)


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("root",type=Path)
    print(json.dumps(build(p.parse_args().root)))
