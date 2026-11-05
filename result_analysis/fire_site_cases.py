"""Rank all 293 cubes and illustrate high direct fire emissions / negative net effects.

Site means are over owned runnable natural vegetation, not just burned pixels.
Positive direct combustion and signed scenario differences are different metrics.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd

from .email_case_studies import SOURCE_ROOT, save, format_time
from .findings_report import COLORS
from .io import file_hash
from .parallel_report import merge_pdfs
from .report import _set_style

NAME = "report_findings_v3_all293/fire_site_cases_v1"
COMPONENTS = ["gpp", "nee", "eco_respiration", "fire_carbon_emission"]
CASES = [
    ("T1862", "Pyrenees-Atlantiques, France", 2019,
     "Highest accumulated direct fire-emission intensity among 293 cubes"),
    ("T1494", "Near Vila Real, Portugal", 2018,
     "The largest annual fire pulse is followed by a negative net contribution"),
    ("T1771", "Near Podgorica, Montenegro", 2023,
     "Several negative annual contributions coexist with a positive cumulative effect"),
]


def folder(root):
    p = Path(root) / NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def screen(root):
    root = Path(root); out = folder(root)
    source = root / "report_findings_v3_all293/all_components_overall_annual.parquet"
    a = pd.read_parquet(source)
    a = a[a.component.isin(COMPONENTS)]
    p = a.pivot(index=["region_id", "year", "area_m2"], columns="component",
                values=["No_Fire", "VIIRS_Fire", "difference"])
    assert len(p) == 293 * 9 and np.isfinite(p.to_numpy()).all()
    assert (p["No_Fire", "fire_carbon_emission"] == 0).all()
    d = pd.DataFrame(index=p.index)
    d["direct_fire"] = p["VIIRS_Fire", "fire_carbon_emission"]
    for name in ["gpp", "nee", "eco_respiration"]:
        d["delta_" + name] = p["difference", name]
    d["net_fire_contribution"] = d.direct_fire + d.delta_nee
    d["decomposition_residual"] = d.delta_nee - d.delta_eco_respiration + d.delta_gpp
    d = d.reset_index()
    assert d.direct_fire.min() >= 0
    assert d.decomposition_residual.abs().max() < .001  # Saved Float32/quantized components.
    d["direct_fire_ktC"] = d.direct_fire * d.area_m2 / 1e9
    d["net_fire_contribution_ktC"] = d.net_fire_contribution * d.area_m2 / 1e9
    d.to_csv(out / "annual_fire_metrics_all293.csv", index=False)
    s = d.groupby("region_id").agg(
        direct_fire_gC_m2=("direct_fire", "sum"), direct_fire_ktC=("direct_fire_ktC", "sum"),
        net_fire_contribution_gC_m2=("net_fire_contribution", "sum"),
        minimum_annual_net_gC_m2=("net_fire_contribution", "min"),
        maximum_annual_direct_gC_m2=("direct_fire", "max"), area_m2=("area_m2", "first")).reset_index()
    for column, rank in [("direct_fire_gC_m2", "direct_intensity_rank"),
                         ("direct_fire_ktC", "direct_mass_rank"),
                         ("maximum_annual_direct_gC_m2", "annual_peak_rank")]:
        s[rank] = s[column].rank(method="min", ascending=False).astype(int)
    s = s.sort_values(["direct_intensity_rank", "region_id"])
    s.to_csv(out / "ranked_sites_2015_2023.csv", index=False)
    d.sort_values(["net_fire_contribution", "region_id", "year"]).to_csv(out / "ranked_annual_net_contributions.csv", index=False)
    chosen = []
    for tag, place, year, title in CASES:
        q = s[s.region_id.str.endswith(tag)]
        assert len(q) == 1
        rec = q.iloc[0].to_dict(); rec.update(place=place, focus_year=year, title=title, tag=tag)
        selected = d[d.region_id.eq(rec["region_id"]) & d.year.eq(year)].iloc[0].to_dict()
        rec["focus"] = selected
        chosen.append(rec)
    assert chosen[0]["direct_intensity_rank"] == 1 and chosen[1]["annual_peak_rank"] == 1
    assert chosen[1]["focus"]["net_fire_contribution"] < 0 and chosen[2]["focus"]["net_fire_contribution"] < 0
    assert d.sort_values("net_fire_contribution").head(2).region_id.tolist() == [chosen[1]["region_id"], chosen[2]["region_id"]]
    evidence = dict(created_utc=dt.datetime.now(dt.timezone.utc).isoformat(), regions=293, years=list(range(2015,2024)),
        cases=chosen, source=str(source), source_sha256=file_hash(source),
        negative_direct_emission_region_years=int(d.direct_fire.lt(0).sum()),
        negative_net_region_years_below_minus_001=int(d.net_fire_contribution.lt(-.01).sum()),
        negative_cumulative_sites=s[s.net_fire_contribution_gC_m2 < -.01].to_dict("records"),
        selection="Exploratory: largest cumulative direct intensity; largest annual direct pulse (also most negative annual net contribution); second-most-negative annual net contribution, in another region.",
        scope="Owned runnable natural-vegetation area, including unburned cells; nine complete years; not a station, burned-area-only mean or Mediterranean total")
    (out / "selection.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return chosen


def raw(root):
    """Read native NetCDF longitude chunks once and recompute nine annual totals."""
    import xarray as xr
    root = Path(root); out = folder(root)
    selected = json.loads((out / "selection.json").read_text())
    records, source_records = [], []
    for case in selected["cases"]:
        rid = case["region_id"]
        with np.load(root / "regions" / rid / "spatial.npz") as z:
            area = z["owned_area_m2"].astype(float)
            mask = z["run_valid_mask"].astype(bool) & (area > 0)
            lat, lon = z["lat"], z["lon"]
        denominator = area[mask].sum()
        np.testing.assert_allclose(denominator, case["area_m2"], rtol=1e-12)
        reference = pd.read_csv(root / "regions" / rid / "annual.csv")
        reference = reference[reference.group_type.eq("overall") & reference.year.between(2015, 2023)]
        variables = [(s, v) for v in ["nee", "gpp", "eco_respiration"] for s in ["No_Fire", "VIIRS_Fire"]]
        variables.append(("VIIRS_Fire", "c_fire_efflux"))
        for scenario, variable in variables:
            path = SOURCE_ROOT / rid / f"output_WISP_{rid}-full_{scenario}" / "data" / f"{variable}.nc"
            stat = path.stat()
            with xr.open_dataset(path) as ds:
                np.testing.assert_allclose(ds.lat.values, lat, rtol=0, atol=1e-10)
                np.testing.assert_allclose(ds.lon.values, lon, rtol=0, atol=1e-10)
                indices = np.flatnonzero(ds.time.dt.year.values >= 2015)
                times = pd.DatetimeIndex(ds.time.values[indices])
                assert times.equals(pd.date_range("2015-01-01", "2023-12-31"))
                years = times.year.to_numpy()
                da = ds[variable]; step = da.encoding["chunksizes"][da.dims.index("lon")]
                daily = np.zeros(len(times), dtype=np.float64)
                for start in range(0, len(lon), step):
                    end = min(start + step, len(lon))
                    block = da.isel(lon=slice(start, end), time=slice(indices[0], indices[-1]+1))
                    extra = [x for x in block.dims if x not in ["lat", "lon", "time"]]
                    b = block.transpose("lat", "lon", *extra, "time").values
                    valid = mask[:, start:end]
                    if not valid.any():
                        continue
                    vals = b[valid].reshape(int(valid.sum()), -1, len(times))
                    assert np.isfinite(vals).all()
                    totals = vals.sum(axis=1, dtype=np.float64)
                    daily += np.einsum("pt,p->t", totals, area[:, start:end][valid], optimize=False)
                daily /= denominator
            q = reference[reference.scenario.eq(scenario) & reference.variable.eq(variable)]
            if variable == "c_fire_efflux": q = q[q.component.eq("fire_carbon_emission")]
            assert len(q) == 9
            for year in range(2015, 2024):
                value = float(daily[years == year].sum())
                expected = float(q[q.year.eq(year)]["mean"].iloc[0])
                np.testing.assert_allclose(value, expected, rtol=1e-6, atol=1e-5)
                records.append(dict(region_id=rid, scenario=scenario, variable=variable, year=year,
                                    raw_gC_m2=value, summary_gC_m2=expected, abs_difference=abs(value-expected)))
            assert path.stat().st_mtime_ns == stat.st_mtime_ns
            source_records.append(dict(path=str(path), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns))
            print(f"Verified {case['tag']} {scenario} {variable}, all nine years", flush=True)
        monthly = pd.read_csv(root / "regions" / rid / "monthly.csv")
        monthly = monthly[monthly.group_type.eq("overall") & monthly.year.between(2015,2023) & monthly.component.isin(COMPONENTS)]
        assert monthly.complete_period.all()
        grouped = monthly.groupby(["scenario", "variable", "component", "year"])["mean"].agg(["sum", "count"])
        assert (grouped["count"] == 12).all()
        expected = reference[reference.component.isin(COMPONENTS)].set_index(["scenario", "variable", "component", "year"])["mean"]
        np.testing.assert_allclose(grouped["sum"], expected.reindex(grouped.index), rtol=1e-9, atol=1e-7)
    pd.DataFrame(records).to_csv(out / "raw_annual_validation.csv", index=False)
    state = dict(status="pass", raw_checks=len(records), raw_files=len(source_records),
                 maximum_absolute_difference=max(x["abs_difference"] for x in records),
                 monthly_to_annual="pass", original_sources=source_records,
                 selection_sha256=file_hash(out / "selection.json"))
    (out / "raw_validation.json").write_text(json.dumps(state, indent=2) + "\n")
    return {k: state[k] for k in ["status", "raw_checks", "raw_files", "maximum_absolute_difference"]}


def figures(root):
    root = Path(root); out = folder(root)
    selection = json.loads((out / "selection.json").read_text())
    validation = json.loads((out / "raw_validation.json").read_text())
    assert validation["status"] == "pass" and validation["selection_sha256"] == file_hash(out / "selection.json")
    assert file_hash(Path(selection["source"])) == selection["source_sha256"]
    annual = pd.read_csv(out / "annual_fire_metrics_all293.csv")
    plt = _set_style(); plt.rcParams.update({"font.size": 10.5, "axes.titlesize": 11, "axes.labelsize": 10.5})
    pages = []
    for index, case in enumerate(selection["cases"], 1):
        d = annual[annual.region_id.eq(case["region_id"])].set_index("year").sort_index()
        year = case["focus_year"]; q = d.loc[year]
        fig, ax = plt.subplots(1, 3, figsize=(14.4, 6.2), gridspec_kw={"width_ratios": [1.22, 1, 1]})
        ax[0].bar(d.index, d.direct_fire, color=COLORS[1], alpha=.65, width=.65, label="Direct combustion")
        ax[0].plot(d.index, d.net_fire_contribution, color="#304F60", marker="o", ms=4, lw=1.8, label="Net fire contribution ΔF")
        ax[0].axhline(0, color="#596970", lw=.8)
        negative = d.net_fire_contribution < 0
        ax[0].scatter(d.index[negative], d.net_fire_contribution[negative], color="#8B4160", edgecolor="white", linewidth=.7, s=38, zorder=4)
        ax[0].set_ylabel("g C m⁻² yr⁻¹")
        ax[0].set_title("A  Annual direct emissions and net contribution", loc="left")
        format_time(ax[0]); ax[0].margins(y=.24)
        ax[0].legend(frameon=False, fontsize=8.8, loc="upper left")
        ax[0].annotate(f"{year}: {q.net_fire_contribution:+.2f}",
                       (year, q.net_fire_contribution), xytext=(0 if q.net_fire_contribution<0 else 25, -23 if q.net_fire_contribution<0 else 10),
                       textcoords="offset points", ha="center" if q.net_fire_contribution<0 else "left", fontsize=10, color="#8B4160" if q.net_fire_contribution<0 else "#304F60")

        terms = [q.delta_eco_respiration, -q.delta_gpp, q.direct_fire, q.net_fire_contribution]
        ax[1].bar(range(4), terms, width=.62, color=[COLORS[0], "#B59A58", COLORS[1], "#8B4160" if terms[-1]<0 else "#304F60"])
        ax[1].set_xticks(range(4), ["ΔReco", "−ΔGPP", "Direct\nfire", "Net ΔF"])
        ax[1].axhline(0, color="#596970", lw=.8); ax[1].set_ylabel("g C m⁻² yr⁻¹")
        ax[1].set_title(f"B  Why is the {year} net effect {'negative' if terms[-1]<0 else 'positive'}?", loc="left")
        ax[1].grid(axis="y", alpha=.18); ax[1].set_axisbelow(True); ax[1].margins(y=.3)
        for x, value in enumerate(terms):
            ax[1].annotate(f"{value:+.2f}", (x, value), xytext=(0, 7 if value>=0 else -16), textcoords="offset points", ha="center", fontsize=10)

        ax[2].plot(d.index, d.direct_fire.cumsum(), color=COLORS[1], marker="s", ms=4, lw=1.8, label="Direct combustion")
        ax[2].plot(d.index, d.net_fire_contribution.cumsum(), color="#304F60", marker="o", ms=4, lw=1.8, label="Net contribution ΔF")
        ax[2].axhline(0, color="#596970", lw=.8); format_time(ax[2]); ax[2].set_ylabel("Accumulated g C m⁻²")
        ax[2].set_title("C  Accumulated contribution, 2015–2023", loc="left")
        ax[2].legend(frameon=False, fontsize=8.8, loc="upper left")
        ax[2].set_xlim(2014.7, 2024.3); ax[2].margins(y=.2)
        for value, color in [(d.direct_fire.sum(), COLORS[1]), (d.net_fire_contribution.sum(), "#304F60")]:
            close = abs(d.direct_fire.sum()-d.net_fire_contribution.sum()) < .08*max(d.direct_fire.sum(),d.net_fire_contribution.sum())
            offset = (7 if value == max(d.direct_fire.sum(),d.net_fire_contribution.sum()) else -7) if close else 0
            ax[2].annotate(f"{value:+.1f}", (2023, value), xytext=(7, offset), textcoords="offset points", va="center", fontsize=10, color=color)
        fig.suptitle(case["title"], x=.065, y=.98, ha="left", fontsize=16, weight="semibold")
        # 'Near' is a geocoded cube label, not an observed station or named fire.
        fig.text(.065, .90, f"{case['place']} · {case['tag']} · owned natural-vegetation area {case['area_m2']/1e6:,.0f} km²\n"
                 f"2015–2023 direct emissions: {case['direct_fire_gC_m2']:.1f} g C m⁻² ({case['direct_fire_ktC']:.1f} kt C over owned area); intensity rank {case['direct_intensity_rank']}/293.", fontsize=10.5)
        fig.text(.065, .12, "ΔF = Ffire + [NEEfire − NEEno-fire] = Ffire + ΔReco − ΔGPP.   Positive = more carbon to the atmosphere than without fire.", fontsize=10, weight="semibold")
        fig.text(.065, .045, "Negative ΔF is a net scenario difference, not negative combustion. Each value averages the cube’s owned runnable natural vegetation, including unburned cells.\n"
                 "Nine complete years; partial 2014 is excluded. Raw daily totals verified. Exploratory extremes among all 293 cubes; these are model findings, not observed emissions.\n"
                 "VIIRS supplies a fire-detection proxy. Carbon mass is shown; CO₂/CO/CH₄ are not separately resolved. Tiny water/ET QA notes do not exclude regions.", fontsize=8.4)
        fig.subplots_adjust(left=.065, right=.98, top=.74, bottom=.28, wspace=.36)
        name = f"{index:02d}_{case['tag']}_high_fire_and_net_contribution"
        save(fig, out, name); pages.append(out / (name + ".pdf"))
    assert merge_pdfs(pages, out / "HIGH_FIRE_AND_NEGATIVE_NET_CASES_EN.pdf") == 3
    return dict(pages=3, cases=[c["tag"] for c in selection["cases"]])


def package(root):
    from pypdf import PdfReader
    root = Path(root); out = folder(root)
    s = json.loads((out / "selection.json").read_text())
    rows = ["# High-fire cases and negative net contributions", "", "All 293 regions are included. Rankings use the nine complete years 2015–2023; partial 2014 is excluded.", "",
            "`Ffire` is direct carbon combustion and is nonnegative in all checked outputs. `ΔF = Ffire + (NEEfire − NEEno-fire)` is the additional net atmospheric carbon contribution relative to no fire; it may be negative.", "",
            "| Cube | Direct 2015–2023 (g C m⁻²) | Highlight year | Direct that year | Net ΔF that year | Cumulative ΔF |", "|---|---:|---:|---:|---:|---:|"]
    for c in s["cases"]:
        q = c["focus"]
        rows.append(f"| {c['place']} ({c['tag']}) | {c['direct_fire_gC_m2']:.3f} | {c['focus_year']} | {q['direct_fire']:.3f} | {q['net_fire_contribution']:+.3f} | {c['net_fire_contribution_gC_m2']:+.3f} |")
    rows += ["", "Annual columns are g C m⁻² yr⁻¹; cumulative columns are g C m⁻². Area totals in kt C use the owned natural-vegetation area only, with no sampling expansion weights.", "",
             "France has the greatest cumulative direct intensity; Bosnia's T2095 has the greatest accumulated direct mass (99.5 kt C), because these are different rankings. Portugal has the largest annual direct pulse (33.44 g C m⁻² in 2016) and the most negative annual net contribution (−2.70 in 2018). Montenegro has the second-most-negative annual net contribution (−2.66 in 2023).", "",
             "In the two negative focus years, respiration declines more than GPP. The reduction in respiration outweighs lost uptake plus the small positive combustion term. This is a budget decomposition, not proof that fire improves ecosystem carbon storage. The cumulative net effect remains positive in all three illustrated cases.", "",
             "The only cumulative negative below −0.01 g C m⁻² is Daraa, Syria (T0359): about −0.046 g C m⁻² over nine years, a weak effect rather than a large negative-emission case. Another sub-microgram residual is not interpreted scientifically.", "",
             "These are selected extremes, not typical sites. The near-place labels identify 128×128 model cubes, not flux-tower stations or named fire events. This analysis does not validate physical causes or observational emission magnitudes.", "",
             "Raw validation independently integrates all nine years, all owned runnable pixels, NEE/GPP/ecosystem respiration in both scenarios and direct fire efflux. Monthly totals also reconcile with annual tables. No simulation outputs or previous reports were modified."]
    (out / "FINDINGS_EN.md").write_text("\n".join(rows) + "\n")
    assert len(PdfReader(out / "HIGH_FIRE_AND_NEGATIVE_NET_CASES_EN.pdf").pages) == 3
    validation = json.loads((out / "raw_validation.json").read_text())
    names = ["HIGH_FIRE_AND_NEGATIVE_NET_CASES_EN.pdf", "FINDINGS_EN.md", "ranked_sites_2015_2023.csv",
             "ranked_annual_net_contributions.csv", "annual_fire_metrics_all293.csv", "selection.json", "raw_validation.json", "raw_annual_validation.csv"]
    names += [p.name for p in sorted(out.glob("0*_high_fire_and_net_contribution.png"))]
    with zipfile.ZipFile(out / "SITE_CASES_EN.zip", "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name in names: z.write(out / name, arcname=name)
    with zipfile.ZipFile(out / "SITE_CASES_EN.zip") as z: assert z.testzip() is None
    record = dict(status="pass", regions=293, pages=3, raw_validation=validation["status"],
                  source_code_sha256=file_hash(Path(__file__)), artifacts={n:file_hash(out/n) for n in names})
    (out / "delivery_validation.json").write_text(json.dumps(record, indent=2) + "\n")
    return dict(status="pass", output=str(out), raw_checks=validation["raw_checks"])


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path); p.add_argument("stage", choices=["screen", "raw", "figures", "package"])
    args = p.parse_args()
    print(json.dumps(globals()[args.stage](args.root)))
