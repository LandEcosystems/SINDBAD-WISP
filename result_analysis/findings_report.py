"""Absolute scenario distributions and an evidence-led interim research brief.

Uses the frozen, QA-gated analysis snapshot. Does not rerun or alter simulations.
Each plotted distribution observation is one region's 2015–2023 annual average,
not nine independent years. Pyrome/vegetation summaries use each group's owned
area within the region, not the region's dominant class.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from . import report as old
from . import parallel_report as parallel
from .io import file_hash

SCENARIOS = ("No_Fire", "VIIRS_Fire")
COLORS = ("#276A84", "#CE713B")
YEARS = tuple(range(2015, 2024))
PRIMARY = (("gpp", "gpp"), ("nee", "nee"),
           ("evapotranspiration", "evapotranspiration"),
           ("cEco", "cVeg"), ("cEco", "cEcosystem"),
           ("c_fire_efflux", "fire_carbon_emission"))
LABELS = {"gpp": "GPP", "nee": "NEE", "evapotranspiration": "Evapotranspiration",
          "transpiration": "Transpiration", "runoff": "Runoff", "cVeg": "Vegetation carbon",
          "cEcosystem": "Ecosystem carbon", "cLitter": "Litter carbon", "cSoil": "Soil carbon",
          "fire_carbon_emission": "Direct fire carbon emission",
          "auto_respiration": "Autotrophic respiration", "hetero_respiration": "Heterotrophic respiration",
          "eco_respiration": "Ecosystem respiration"}
FAMILIES = {"pyrome": ["FCS", "FIL", "ICS", "RCS", "RIL", "unknown"],
            "vegetation": ["forest", "shrub", "savanna", "grass", "wetland"]}


def destination(root):
    return Path(root) / "report_findings_v2"


def snapshot(root):
    return json.loads((Path(root) / "report_parallel_v1/prepared.json").read_text())


def component_frame(item):
    if file_hash(Path(item["annual"])) != item["annual_sha256"]:
        raise ValueError("Frozen component table changed")
    return pd.read_parquet(item["annual"])


def region_means(frame):
    """One paired region/group value; fail on incomplete or unequal support."""
    frame = frame[frame.complete_period & frame.year.isin(YEARS)].copy()
    pairs = old._paired(frame)
    if not pairs.same_area.all():
        raise ValueError("Scenario spatial supports differ")
    keys = ["region_id", "group_type", "group"]
    count = pairs.groupby(keys, observed=True).year.nunique()
    if not (count == len(YEARS)).all():
        raise ValueError("A region/group does not have all nine matched complete years")
    variation = pairs.groupby(keys, observed=True).area_m2.agg(["min", "max"])
    if not np.allclose(variation["min"], variation["max"], rtol=1e-8, atol=.01):
        raise ValueError("Area support changes between years")
    values = pairs.groupby(keys, observed=True)[[*SCENARIOS, "difference", "area_m2"]].mean().reset_index()
    values["n_years"] = len(YEARS)
    values["relative_change_pct"] = np.divide(100 * values.difference, values.No_Fire.abs(),
        out=np.full(len(values), np.nan), where=values.No_Fire.abs().to_numpy() > 0)
    return values


def weighted_summary(values):
    rows = []
    for (family, group), d in values.groupby(["group_type", "group"], observed=True):
        w = d.area_m2.to_numpy(float)
        nf, ff = (float(np.average(d[s], weights=w)) for s in SCENARIOS)
        rows.append(dict(group_type=str(family), group=str(group), n_regions=len(d),
            area_m2=float(w.sum()), No_Fire=nf, VIIRS_Fire=ff, difference=ff-nf,
            relative_change_pct=100*(ff-nf)/abs(nf) if nf != 0 else np.nan,
            median_region_difference=float(d.difference.median()),
            n_regions_decrease=int((d.difference < 0).sum()), n_regions_increase=int((d.difference > 0).sum())))
    return pd.DataFrame(rows)


def label(item):
    c = item["component"]
    return LABELS.get(c, item["variable"] + " / " + c.replace("_", " "))


def unit(frame):
    u = str(frame.units.iloc[0]).replace("g C m-2", "g C m⁻²")
    if str(frame.aggregation.iloc[0]) == "sum":
        u += " yr⁻¹"
    return u


def draw_violin(ax, values, position, color, width=.33):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return
    if len(values) >= 3 and np.ptp(values) > max(1e-12, abs(values.mean())*1e-12):
        v = ax.violinplot([values], positions=[position], widths=width, showextrema=False, bw_method=.25)
        for body in v["bodies"]:
            body.set_facecolor(color); body.set_edgecolor(color); body.set_alpha(.25)
    else:
        ax.plot([position-width/3, position+width/3], [values[0], values[0]], color=color, lw=2)
    ax.boxplot([values], positions=[position], widths=width*.30, showfliers=False,
        patch_artist=True, manage_ticks=False,
        boxprops=dict(facecolor="white", edgecolor=color, lw=1.2),
        medianprops=dict(color="#24313A", lw=1.6),
        whiskerprops=dict(color=color), capprops=dict(color=color))
    # Matching jitter order across scenarios makes pairing reproducible.
    jitter = np.random.default_rng(42).uniform(-width*.24, width*.24, len(values))
    ax.scatter(position+jitter, values, s=8, color=color, alpha=.30, linewidths=0, rasterized=True)


def absolute_figure(values, item, units, family="pyrome", delta=True):
    from matplotlib.patches import Patch
    plt = old._set_style()
    groups = [("overall", "all")] + [(family, g) for g in FAMILIES.get(family, [])]
    if family == "overall": groups = [("overall", "all")]
    fig, axes = plt.subplots(2 if delta else 1, 1, figsize=(13, 8.0 if delta else 5.6),
        sharex=True, gridspec_kw={"height_ratios": [3, 1.5]} if delta else None,
        squeeze=False)
    ax = axes[0, 0]; xticks = []
    for k, (f, g) in enumerate(groups):
        d = values[values.group_type.eq(f) & values.group.eq(g)].sort_values("region_id")
        name = "All cubes" if f == "overall" else g.upper() if family == "pyrome" and g != "unknown" else g.capitalize()
        xticks.append(f"{name}\nn={len(d)}")
        for s, offset, color in zip(SCENARIOS, [-.20, .20], COLORS):
            draw_violin(ax, d[s], k+offset, color)
        if delta:
            draw_violin(axes[1, 0], d.difference, k, COLORS[1], width=.6)
    ax.set_ylabel(f"{label(item)} ({units})")
    ax.set_title("No fire and VIIRS fire: absolute values", loc="left")
    ax.legend(handles=[Patch(facecolor=c, alpha=.5, label=s.replace("_", " ")) for c,s in zip(COLORS,SCENARIOS)], frameon=False, loc="upper right")
    if len(values) and values[list(SCENARIOS)].min().min() >= 0: ax.set_ylim(bottom=0)
    if delta:
        axes[1, 0].axhline(0, color="#69737A", lw=.8)
        axes[1, 0].set_ylabel(f"Fire − no fire\n({units})")
        axes[1, 0].set_title("Paired response: the same cubes in both scenarios", loc="left", fontsize=10)
    for a in axes[:, 0]: a.grid(axis="y", color="#E1E6E9", lw=.6); a.set_axisbelow(True)
    axes[-1, 0].set_xticks(range(len(groups)), xticks)
    fig.suptitle(f"{label(item)} across {'pyromes' if family=='pyrome' else 'vegetation types' if family=='vegetation' else 'all analysed cubes'}", x=.07, ha="left", fontsize=16, fontweight="semibold")
    fig.text(.07,.018, "Each dot = one cube’s mean of 2015–2023 annual values. Boxes: median/IQR; violins: between-cube distribution, not confidence intervals.\nGroup values use owned group area within each cube; mixed cubes can appear in multiple groups. Overall counts each cube once.", fontsize=8)
    fig.subplots_adjust(left=.09,right=.98,top=.88,bottom=.14,hspace=.32)
    return fig


def savefig(fig, stem):
    import matplotlib.pyplot as plt
    stem.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(stem.with_suffix(".png"), dpi=170, facecolor="white")
    fig.savefig(stem.with_suffix(".pdf"), facecolor="white")
    plt.close(fig)


def component(root, index):
    p = snapshot(root); item=p["components"][index]; frame=component_frame(item)
    out=destination(root)/"components"/item["token"]; out.mkdir(parents=True,exist_ok=True)
    values=region_means(frame); values.to_csv(out/"region_values.csv", index=False)
    stats=weighted_summary(values); stats.to_csv(out/"area_weighted_summary.csv",index=False)
    figures=[]
    for family in ("overall","pyrome","vegetation"):
        stem=out/f"absolute_{family}"
        savefig(absolute_figure(values,item,unit(frame),family),stem)
        figures.append(str(stem.with_suffix(".pdf")))
    record=dict(index=index,component=item["component"],variable=item["variable"],units=unit(frame),
        source_sha256=item["annual_sha256"],snapshot=p["fingerprint"],figures=figures,
        n_regions=int(values[values.group_type.eq("overall")].region_id.nunique()))
    old.atomic_json(out/"complete.json",record)
    return record


def spatial_evidence(root, regions):
    """Independent endpoint/area calculations from saved per-pixel summaries."""
    rows=[]
    for directory in map(Path,regions):
        with np.load(directory/"spatial.npz") as z:
            area=z["owned_area_m2"].astype(float); valid=z["run_valid_mask"].astype(bool)&(area>0)
            fire=z["fire_day_count"]>0
            row=dict(region_id=directory.name,area_m2=float(area[valid].sum()),
                fire_affected_area_m2=float(area[valid&fire].sum()),
                repeat_detection_area_m2=float(area[valid&(z["fire_day_count"]>1)].sum()))
            for cohort,mask in [("all",valid),("detected",valid&fire),("undetected",valid&~fire)]:
                a=area[mask];row[cohort+"_area_m2"]=float(a.sum())
                for v,c in [("gpp","gpp"),("evapotranspiration","evapotranspiration"),("cEco","cVeg")]:
                    for scenario in SCENARIOS:
                        x=z[f"{v}__{c}__{scenario}"][mask]
                        row[f"{cohort}__{c}__{scenario}"]=float(np.average(x,weights=a)) if a.sum() else np.nan
            for c in ["cVeg","cEcosystem","cLitter","cSoil"]:
                for scenario in SCENARIOS:
                    x=z[f"cEco__{c}__final_saved__{scenario}"][valid]
                    row[f"final__{c}__{scenario}"]=float(np.average(x,weights=area[valid]))
            row["final_cVeg_difference"]=row["final__cVeg__VIIRS_Fire"]-row["final__cVeg__No_Fire"]
            row["final_cVeg_relative_pct"]=100*row["final_cVeg_difference"]/row["final__cVeg__No_Fire"] if row["final__cVeg__No_Fire"]>0 else np.nan
            row["fire_affected_fraction"]=row["fire_affected_area_m2"]/row["area_m2"]
            rows.append(row)
    return pd.DataFrame(rows)


def choose_cases(spatial):
    """Exploratory strongest relative cVeg responses, avoiding tiny baselines."""
    good=spatial[(spatial['final__cVeg__No_Fire']>=100)&(spatial.area_m2>=1e8)].copy()
    selected=good.sort_values(["final_cVeg_relative_pct","region_id"]).head(3).copy()
    selected['selection_rule']='Three most negative final cVeg percentages; no-fire cVeg >=100 gC/m2 and owned area >=100 km2; exploratory, not representative'
    return selected


def case_name(rid):
    part=rid.split('_')
    return f"{part[2].replace('-', ' ')} ({part[1]}) | {part[-1]}"


def case_figures(root, cases, components):
    plt=old._set_style(); out=destination(root); files=[]; annuals=[]
    selected_pairs=[("gpp","gpp"),("cEco","cVeg"),("cEco","cEcosystem"),
                    ("evapotranspiration","evapotranspiration"),("nee","nee"),
                    ("c_fire_efflux","fire_carbon_emission")]
    for _,case in cases.iterrows():
        rid=case.region_id; fig,axes=plt.subplots(2,3,figsize=(13.6,8))
        for ax,(v,c) in zip(axes.flat,selected_pairs):
            item=next(x for x in components if (x['variable'],x['component'])==(v,c))
            f=component_frame(item);f=f[f.region_id.eq(rid)&f.group_type.eq('overall')&f.complete_period].copy()
            for scenario,color in zip(SCENARIOS,COLORS):
                d=f[f.scenario.eq(scenario)].sort_values('year')
                ax.plot(d.year,d['mean'],color=color,marker='o' if scenario=='No_Fire' else 's',ms=3.5,lw=1.5,label=scenario.replace('_',' '))
            ax.set_title(label(item),loc='left');ax.set_ylabel(unit(f));ax.set_xticks([2015,2017,2019,2021,2023]);ax.grid(alpha=.18)
            annuals.append(f)
        axes[0,0].legend(frameon=False,fontsize=9)
        fig.suptitle(f"{case_name(rid)}: final vegetation carbon {case.final_cVeg_relative_pct:.1f}% with fire",fontsize=16,x=.07,ha='left')
        fig.text(.07,.025,f"Owned analysed area: {case.area_m2/1e6:.0f} km²; {case.fire_affected_fraction:.1%} had at least one VIIRS forcing day.\nExploratory high-response case, not a typical site. Annual flux totals and annual mean stocks; final-stock percentage uses 31 December 2023.",fontsize=8)
        fig.subplots_adjust(top=.87,bottom=.14,wspace=.35,hspace=.38)
        stem=out/'cases'/rid/'annual_response';savefig(fig,stem);files.append(stem.with_suffix('.pdf'))
        with np.load(Path(root)/'regions'/rid/'spatial.npz') as z:
            valid=z['run_valid_mask'].astype(bool)&(z['owned_area_m2']>0)
            a=z['cEco__cVeg__final_saved__No_Fire'].astype(float);b=z['cEco__cVeg__final_saved__VIIRS_Fire'].astype(float)
            change=np.where(valid,b-a,np.nan); counts=np.where(valid,z['fire_day_count'],np.nan)
            fig,axes=plt.subplots(1,2,figsize=(12.4,5.6))
            vmax=max(float(np.nanmax(abs(change))),1e-9)
            im=axes[0].pcolormesh(z['lon'],z['lat'],change,cmap='RdBu_r',vmin=-vmax,vmax=vmax,shading='nearest',rasterized=True)
            fig.colorbar(im,ax=axes[0],label='Final cVeg difference (g C m⁻²)')
            im=axes[1].pcolormesh(z['lon'],z['lat'],counts,cmap='YlOrBr',vmin=0,shading='nearest',rasterized=True)
            fig.colorbar(im,ax=axes[1],label='VIIRS-positive forcing days (2014–2023)')
            axes[0].set_title('Carbon response is spatially concentrated',loc='left');axes[1].set_title('Repeated detections are not separate fires',loc='left')
            for ax in axes: ax.set_xlabel('Longitude (°E)');ax.set_ylabel('Latitude (°N)');ax.set_aspect(1/np.cos(np.deg2rad(np.mean(z['lat']))))
            fig.suptitle(case_name(rid),x=.07,ha='left',fontsize=15)
            fig.text(.07,.02,'White = outside unique owned/runnable support. VIIRS forcing is a detection proxy; these are model responses, not observed carbon losses.',fontsize=8)
            fig.subplots_adjust(left=.08,right=.96,top=.85,bottom=.15,wspace=.36)
            stem=out/'cases'/rid/'spatial_response';savefig(fig,stem);files.append(stem.with_suffix('.pdf'))
    pd.concat(annuals,ignore_index=True).to_csv(out/'case_annual_values.csv',index=False)
    return files


def discovery(root):
    p=snapshot(root);out=destination(root);out.mkdir(parents=True,exist_ok=True)
    spatial=spatial_evidence(root,p['regions']);spatial.to_csv(out/'spatial_evidence.csv',index=False)
    cases=choose_cases(spatial);cases.to_csv(out/'case_selection.csv',index=False)
    summary=[]
    for item in p['components']:
        if (item['variable'],item['component']) not in PRIMARY+(("eco_respiration","eco_respiration"),): continue
        f=component_frame(item);w=weighted_summary(region_means(f));w['variable']=item['variable'];w['component']=item['component'];w['units']=unit(f);summary.append(w)
    summary=pd.concat(summary,ignore_index=True);summary.to_csv(out/'primary_findings_by_group.csv',index=False)
    cohorts=[]
    for cohort in ['all','detected','undetected']:
        for c in ['gpp','evapotranspiration','cVeg']:
            w=spatial[cohort+'_area_m2'].to_numpy();good=w>0
            nf,ff=(float(np.average(spatial.loc[good,f'{cohort}__{c}__{s}'],weights=w[good])) for s in SCENARIOS)
            cohorts.append(dict(cohort=cohort,variable=c,area_m2=float(w.sum()),No_Fire=nf,VIIRS_Fire=ff,difference=ff-nf,
                relative_change_pct=100*(ff-nf)/abs(nf) if nf else np.nan))
    cohorts=pd.DataFrame(cohorts);cohorts.to_csv(out/'fire_exposure_comparison.csv',index=False)
    # Independently reconcile endpoint evidence to the prior report tables.
    endpoint=pd.read_csv(Path(root)/'report_parallel_v1/tasks/069_science/stock_endpoints.csv')
    existing=endpoint[endpoint.component.eq('cVeg')].set_index('region_id').final_difference
    test=spatial.set_index('region_id').final_cVeg_difference.reindex(existing.index)
    if not np.allclose(test,existing,rtol=1e-9,atol=1e-8):raise ValueError('Endpoint summaries disagree')
    figures=case_figures(root,cases,p['components'])
    plt=old._set_style()
    # Exposure comparison separates a diluted all-area response from affected cells.
    fig,axes=plt.subplots(1,3,figsize=(12.8,4.8))
    for ax,c,title in zip(axes,['gpp','evapotranspiration','cVeg'],['GPP','Evapotranspiration','Mean vegetation carbon']):
        d=cohorts[cohorts.variable.eq(c)].set_index('cohort').loc[['all','detected','undetected']]
        ax.bar(np.arange(3),d.relative_change_pct,color=['#8698A3',COLORS[1],COLORS[0]],width=.6)
        ax.set_xticks(range(3),['All owned\nland','VIIRS-positive\ncells','No-detection\ncells']);ax.set_title(title,loc='left');ax.axhline(0,color='#65717A',lw=.8)
        ax.set_ylabel('VIIRS fire − no fire (%)');ax.grid(axis='y',alpha=.18);ax.set_axisbelow(True)
        for x,y in enumerate(d.relative_change_pct):ax.annotate(f'{y:.3f}%',(x,y),xytext=(0,-13 if y<0 else 5),textcoords='offset points',ha='center',fontsize=9)
        ax.margins(y=.22)
    frac=spatial.fire_affected_area_m2.sum()/spatial.area_m2.sum()
    fig.suptitle(f'Fire responses are concentrated in {frac:.1%} of the analysed area',x=.07,ha='left',fontsize=15)
    fig.text(.07,.025,'Full saved period, 12 January 2014–31 December 2023; ratio of area-weighted scenario means. Detection cohorts fixed over the whole period.\nNo-detection means no qualifying record, not verified absence of fire. Differences in model outputs are not observed losses.',fontsize=8)
    fig.subplots_adjust(top=.82,bottom=.23,wspace=.37)
    stem=out/'figures/fire_exposure';savefig(fig,stem);figures.insert(0,stem.with_suffix('.pdf'))
    # Additive atmospheric carbon response (complete years only).
    fp=pd.read_csv(Path(root)/'report_parallel_v1/tasks/069_science/carbon_flux_response.csv')
    means=fp.groupby('component',sort=False).mean(numeric_only=True).mean_gC_m2
    order=['Direct fire efflux','Reduced carbon uptake (−ΔGPP)','Respiration change (ΔReco)']
    vals=[float(means[x]) for x in order]; vals.append(sum(vals))
    fig,ax=plt.subplots(figsize=(11.5,5.2))
    ax.bar(range(4),vals,color=[COLORS[1],COLORS[1],COLORS[0],'#374D5A'],width=.65)
    ax.set_xticks(range(4),['Direct combustion','Reduced GPP\n(−ΔGPP)','Respiration change\n(ΔReco)','Net atmospheric response\nΔ(NEE + fire)'])
    ax.set_ylabel('Mean annual carbon response (g C m⁻² yr⁻¹)');ax.axhline(0,color='#68737A',lw=.8);ax.grid(axis='y',alpha=.18);ax.set_axisbelow(True)
    for x,y in enumerate(vals):ax.annotate(f'{y:+.3f}',(x,y),xytext=(0,6 if y>=0 else -16),textcoords='offset points',ha='center')
    ax.margins(y=.25);fig.suptitle('Direct combustion does not describe the full carbon response',x=.08,ha='left',fontsize=15)
    fig.text(.08,.025,'2015–2023; owned-area weighted within the 247 QA-passed cubes. Bars are additive except the last, which is their sum.\nNEE excludes direct fire. Mortality and internal transfers are not counted again. Not a Mediterranean emission total.',fontsize=8)
    fig.subplots_adjust(left=.1,right=.98,top=.85,bottom=.23)
    stem=out/'figures/carbon_response_decomposition';savefig(fig,stem);figures.insert(1,stem.with_suffix('.pdf'))
    pd.DataFrame({'component':order+['Net atmospheric response'],'mean_annual_gC_m2':vals}).to_csv(out/'carbon_response_decomposition.csv',index=False)
    record=dict(snapshot=p['fingerprint'],primary_regions=p['primary_regions'],processed_regions=p['processed_regions'],
        fire_affected_fraction=frac,case_ids=cases.region_id.tolist(),figures=list(map(str,figures)),
        endpoint_reconciliation='pass',created_utc=dt.datetime.now(dt.timezone.utc).isoformat())
    old.atomic_json(out/'discovery_complete.json',record)
    return record


def reference_annual(root):
    """Compact complete-year comparisons; reuse audited native-support pairs."""
    p=snapshot(root);out=destination(root)/'references';out.mkdir(parents=True,exist_ok=True)
    cols=['region_id','scenario','product','variable','year','reference_cell_id','reference_lat','reference_lon',
          'model','reference','units','coverage','accepted_90','carbon_fraction','covered_area_m2']
    frames=[];weights=[];source_records=[]
    for directory in map(Path,p['regions']):
        path=directory/'references/annual_reference_pairs.csv.gz'
        source_records.append(dict(path=str(path),sha256=file_hash(path)))
        f=pd.read_csv(path,usecols=cols,dtype={'reference_cell_id':str})
        f=f[f.accepted_90.astype(str).str.lower().isin(['true','1','1.0']) & f.year.between(2015,2021) &
            np.isfinite(f.model)&np.isfinite(f.reference)].copy()
        f['carbon_fraction']=f.carbon_fraction.fillna('not_applicable').astype(str)
        frames.append(f)
        path=directory/'references/reference_group_weights.csv.gz'
        w=pd.read_csv(path,dtype={'reference_cell_id':str});w=w[w.group_type.isin(['pyrome','vegetation'])].copy()
        w['carbon_fraction']=w.carbon_fraction.fillna('not_applicable').astype(str);weights.append(w)
    f=pd.concat(frames,ignore_index=True)
    key=['product','variable','scenario','year','reference_cell_id','carbon_fraction']
    f=f.sort_values(['coverage','region_id'],ascending=[False,True],kind='stable').drop_duplicates(key)
    f.to_parquet(out/'annual_native_pairs.parquet',index=False)
    w=pd.concat(weights,ignore_index=True)
    join=['region_id','product','variable','reference_cell_id','carbon_fraction']
    identity=join+['group_type','group']
    if w.duplicated(identity).any():
        if w.groupby(identity,dropna=False).group_area_m2.nunique().max()>1:raise ValueError('Conflicting reference group areas')
        w=w.drop_duplicates(identity)
    merged=f.merge(w[identity+['group_area_m2']],on=join,how='inner',validate='many_to_many')
    metrics=[]
    for keys,d in f.groupby(['product','variable','scenario','carbon_fraction'],observed=True):
        metrics.append(dict(zip(['product','variable','scenario','carbon_fraction'],keys),group_type='overall',group='all',
            n_regions=d.region_id.nunique(),years=','.join(map(str,sorted(d.year.unique()))),units=d.units.iloc[0],
            weighting='one unique reference cell-year',**old.basic_metrics(d.reference,d.model)))
    for keys,d in merged.groupby(['product','variable','scenario','carbon_fraction','group_type','group'],observed=True):
        weight=d.group_area_m2.to_numpy();x=d.reference.to_numpy();y=d.model.to_numpy();mx=np.average(x,weights=weight);my=np.average(y,weights=weight)
        vx=np.average((x-mx)**2,weights=weight);vy=np.average((y-my)**2,weights=weight);cov=np.average((x-mx)*(y-my),weights=weight)
        metrics.append(dict(zip(['product','variable','scenario','carbon_fraction','group_type','group'],keys),
            n=len(d),n_regions=d.region_id.nunique(),years=','.join(map(str,sorted(d.year.unique()))),units=d.units.iloc[0],
            weighting='original-class overlap area of mixed reference cells',bias=my-mx,mae=np.average(abs(y-x),weights=weight),
            rmse=np.sqrt(np.average((y-x)**2,weights=weight)),r=cov/np.sqrt(vx*vy) if vx*vy>0 else np.nan,
            nse=1-np.average((y-x)**2,weights=weight)/vx if vx>0 else np.nan))
    pd.DataFrame(metrics).to_csv(out/'annual_reference_metrics.csv',index=False)
    # Same coarse geographic cells and complete years for the GPP source comparison.
    g=f[f.variable.eq('gpp')].copy();g['lat_key']=g.reference_lat.round(3);g['lon_key']=g.reference_lon.round(3)
    common_keys=['scenario','year','lat_key','lon_key'];counts=g.groupby(common_keys).product.nunique()
    common_index=counts[counts==2].reset_index()[common_keys]
    common=g.merge(common_index,on=common_keys,validate='many_to_one')
    if common.duplicated(common_keys+['product']).any():raise ValueError('Common GPP grid keys not unique')
    common.to_parquet(out/'common_gpp_annual_pairs.parquet',index=False)
    common_metrics=[]
    for (product,scenario),d in common.groupby(['product','scenario']):
        common_metrics.append(dict(product=product,scenario=scenario,n_regions=d.region_id.nunique(),
            years=','.join(map(str,sorted(d.year.unique()))),**old.basic_metrics(d.reference,d.model)))
    pd.DataFrame(common_metrics).to_csv(out/'common_gpp_metrics.csv',index=False)
    plt=old._set_style();figures=[]
    for variable in ['gpp','nee','evapotranspiration','transpiration','cVegWood']:
        d=common if variable=='gpp' else f[f.variable.eq(variable) & f.carbon_fraction.isin(['not_applicable','0.47'])]
        products=sorted(d['product'].unique());fig,axes=plt.subplots(2,len(products),squeeze=False,figsize=(6.1*len(products),9.3))
        for row,scenario in enumerate(SCENARIOS):
            for col,product in enumerate(products):
                sub=d[d.scenario.eq(scenario)&d['product'].eq(product)];ax=axes[row,col]
                x=sub.reference.to_numpy();y=sub.model.to_numpy();m=old.basic_metrics(x,y)
                hb=ax.hexbin(x,y,gridsize=55,mincnt=1,bins='log',cmap='cividis',linewidths=0,rasterized=True)
                fig.colorbar(hb,ax=ax,label='Cell-year pair count (log scale)',shrink=.78)
                lo=min(x.min(),y.min());hi=max(x.max(),y.max());ax.plot([lo,hi],[lo,hi],color='#67747A',ls='--',lw=.8)
                ax.set_title(f"{product}\n{scenario.replace('_',' ')}",fontsize=11,loc='left')
                ax.set_xlabel('Reference ('+str(sub.units.iloc[0])+')');ax.set_ylabel('SINDBAD ('+str(sub.units.iloc[0])+')')
                ax.text(.03,.97,f"n={len(sub):,}\nbias={m['bias']:.1f}\nRMSE={m['rmse']:.1f}\nr={m['r']:.2f}",transform=ax.transAxes,va='top',fontsize=9)
                ax.set_aspect('equal',adjustable='box')
        fig.suptitle(LABELS.get(variable,variable)+' | annual reference-scale comparisons',x=.08,ha='left',fontsize=16)
        fig.text(.08,.018,'2015–2021 where available; ≥90% reference-cell support; overlapping cube pairs deduplicated. Reference estimates are not ground truth.\nGPP panels share coarse cells and years; product-specific masks can still differ. CCI matches woody carbon, not total vegetation carbon.',fontsize=8)
        fig.subplots_adjust(top=.88,bottom=.11,hspace=.43,wspace=.32)
        stem=out/('annual_'+variable);savefig(fig,stem);figures.append(str(stem.with_suffix('.pdf')))
    old.atomic_json(out/'complete.json',dict(snapshot=p['fingerprint'],figures=figures,sources=source_records,
        n_annual_pairs=len(f),n_common_gpp_pairs=len(common),years='2015–2021 available complete years'))
    return dict(n_pairs=len(f),n_common_gpp=len(common),figures=figures)


def group_atlases(root):
    from matplotlib.backends.backend_pdf import PdfPages
    p=snapshot(root);out=destination(root);plt=old._set_style();records=[]
    for family,groups in FAMILIES.items():
        for group in groups:
            folder=out/'groups'/family/group;folder.mkdir(parents=True,exist_ok=True)
            tables=[]
            with PdfPages(folder/'ABSOLUTE_SCENARIOS_EN.pdf') as pdf:
                for start in range(0,len(p['components']),6):
                    fig,axes=plt.subplots(2,3,figsize=(13.2,8.0))
                    for ax,item in zip(axes.flat,p['components'][start:start+6]):
                        path=out/'components'/item['token']
                        d=pd.read_csv(path/'region_values.csv');d=d[d.group_type.eq(family)&d.group.eq(group)].copy()
                        units=json.loads((path/'complete.json').read_text())['units']
                        for x,s,c in zip([0,1],SCENARIOS,COLORS):draw_violin(ax,d[s],x,c,width=.55)
                        ax.set_xticks([0,1],['No fire','VIIRS fire']);ax.set_title(label(item),fontsize=10,loc='left')
                        ax.set_ylabel(units);ax.grid(axis='y',alpha=.18);ax.text(.97,.98,f'n={len(d)} cubes',transform=ax.transAxes,ha='right',va='top',fontsize=8)
                        d['variable']=item['variable'];d['component']=item['component'];d['units']=units;tables.append(d)
                    for ax in list(axes.flat)[len(p['components'][start:start+6]):]:ax.set_visible(False)
                    fig.suptitle(f'{family.capitalize()}: {group} | absolute scenario values',x=.07,ha='left',fontsize=15)
                    fig.text(.07,.02,'One value per cube/group: 2015–2023 average annual total for fluxes, average annual state for stocks. Distributions, not confidence intervals.\nOriginal group area within each cube; axes differ between physical quantities. All components retained, including constant zero diagnostics.',fontsize=8)
                    fig.subplots_adjust(left=.08,right=.98,top=.88,bottom=.13,hspace=.4,wspace=.38);pdf.savefig(fig);plt.close(fig)
            pd.concat(tables,ignore_index=True).to_csv(folder/'values.csv.gz',compression='gzip',index=False)
            records.append(dict(family=family,group=group,pdf=str(folder/'ABSOLUTE_SCENARIOS_EN.pdf')))
    old.atomic_json(out/'groups_complete.json',dict(snapshot=p['fingerprint'],groups=records))
    return records


def brief(root):
    from matplotlib.backends.backend_pdf import PdfPages
    p=snapshot(root);out=destination(root)
    disc=json.loads((out/'discovery_complete.json').read_text())
    ref=json.loads((out/'references/complete.json').read_text())
    if disc['snapshot']!=p['fingerprint'] or ref['snapshot']!=p['fingerprint']:raise ValueError('Mixed snapshots')
    for item in p['components']:
        state=json.loads((out/'components'/item['token']/'complete.json').read_text())
        if state['snapshot']!=p['fingerprint']:raise ValueError('Mixed component snapshots')
    stats=pd.read_csv(out/'primary_findings_by_group.csv');overall=stats[stats.group_type.eq('overall')].set_index('component')
    exposure=pd.read_csv(out/'fire_exposure_comparison.csv');affected=exposure[exposure.cohort.eq('detected')].set_index('variable')
    cases=pd.read_csv(out/'case_selection.csv');decomp=pd.read_csv(out/'carbon_response_decomposition.csv').set_index('component').mean_annual_gC_m2
    rm=pd.read_csv(out/'references/common_gpp_metrics.csv');rvi=rm[rm.scenario.eq('VIIRS_Fire')]
    native=pd.read_csv(out/'references/annual_reference_metrics.csv')
    ev=native[(native.variable.eq('evapotranspiration'))&native.scenario.eq('VIIRS_Fire')&native.group_type.eq('overall')]
    findings=[
        f"Fire responses are local: {100*disc['fire_affected_fraction']:.2f}% of analysed owned area has at least one VIIRS forcing day. In those cells, mean vegetation carbon is {abs(affected.loc['cVeg','relative_change_pct']):.1f}% lower, compared with {abs(exposure[(exposure.cohort=='all')&(exposure.variable=='cVeg')].iloc[0].relative_change_pct):.2f}% over all analysed land (full saved period).",
        f"Small average GPP changes coexist with larger local stock losses. Mean annual GPP changes from {overall.loc['gpp','No_Fire']:.1f} to {overall.loc['gpp','VIIRS_Fire']:.1f} g C m−2 yr−1 ({overall.loc['gpp','relative_change_pct']:+.3f}%; 2015–2023). The three selected high-response cubes end 2023 with {abs(cases.iloc[0].final_cVeg_relative_pct):.1f}%, {abs(cases.iloc[1].final_cVeg_relative_pct):.1f}% and {abs(cases.iloc[2].final_cVeg_relative_pct):.1f}% less vegetation carbon.",
        f"The model's carbon response extends beyond combustion: direct fire efflux is {decomp['Direct fire efflux']:.3f}, reduced uptake contributes {decomp['Reduced carbon uptake (−ΔGPP)']:.3f}, and reduced respiration offsets {abs(decomp['Respiration change (ΔReco)']):.3f} g C m−2 yr−1. The combined atmospheric response is {decomp['Net atmospheric response']:.3f} ({100*(decomp['Net atmospheric response']/decomp['Direct fire efflux']-1):.0f}% above combustion alone; complete years 2015–2023).",
    ]
    reflines=[f"{x['product']}: annual GPP bias {x['bias']:+.0f}, RMSE {x['rmse']:.0f} g C m−2 yr−1; r={x['r']:.2f}; {int(x['n']):,} common cell-year pairs." for _,x in rvi.iterrows()]
    reflines += [f"{x['product']}: annual ET bias {x['bias']:+.0f}, RMSE {x['rmse']:.0f} mm yr−1; r={x['r']:.2f}; {int(x['n']):,} product-specific cell-year pairs." for _,x in ev.iterrows()]
    opening=out/'FINDINGS_SUMMARY_EN.pdf'
    with PdfPages(opening) as pdf:
        old._text_page(pdf,'Fire effects are concentrated; regional averages hide local losses',findings+[
            'Interim model experiment: 293 completed regions analysed; 247 pass the primary physical QA gate. The other 46 remain flagged and are not silently corrected. These are model-supported findings, not established new observations or a whole-Mediterranean total.',
            'No_Fire and VIIRS_Fire use common weather, initialization and static cover. VIIRS is an active-fire proxy with a 0.99 forcing multiplier, not measured burned-area fraction. No genuine WISP forecast scenario is included.'])
        old._text_page(pdf,'Reference agreement and questions for the next experiment',reflines+[
            'Model outputs are aggregated to reference support. Fine-grid display maps do not add independent observations. Shared coarse cells and years are used for the GPP source comparison; product-specific valid-area masks may still differ.',
            'Research question: how much of the persistent carbon response comes from reduced photosynthetic uptake, and how much is offset by reduced respiration? The present accounting separates these components; it does not identify a universal fire mechanism.',
            'Next verification: test the strongest cases against independent burned-area and biomass changes, and check sensitivity to the 0.99 forcing assumption and repeated VIIRS detections. Do not interpret multiple detection days as separate wildfire events.',
            'LAI was not saved and its module was not enabled; no simulated-LAI validation is reported. Original PFT parameter maps, saved run code, and the output semantics guide take precedence over stale README statements.'])
    chosen=[out/'components/gpp__gpp/absolute_pyrome.pdf',out/'components/gpp__gpp/absolute_vegetation.pdf',
            out/'components/cEco__cVeg/absolute_pyrome.pdf',out/'components/evapotranspiration__evapotranspiration/absolute_pyrome.pdf']
    pages=parallel.merge_pdfs([opening]+chosen+[Path(x) for x in disc['figures']]+[Path(x) for x in ref['figures']],out/'SUPERVISOR_BRIEF_EN.pdf')
    case_table=cases[['region_id','final__cVeg__No_Fire','final__cVeg__VIIRS_Fire','final_cVeg_relative_pct','fire_affected_fraction']].copy()
    lines=['# Fire responses are concentrated; regional averages hide local losses','','## Executive summary','']
    lines += ['- '+x for x in findings]
    lines += ['', '**Scope:** 293 processed cubes; 247 pass primary QA, 46 flagged. Descriptive interim model results, not observations or an unbiased Mediterranean estimate. WISP predictions are not part of these scenarios.', '',
        '[Download the supervisor brief](SUPERVISOR_BRIEF_EN.pdf) · [Numerical findings by group](primary_findings_by_group.csv) · [Fire-exposure comparison](fire_exposure_comparison.csv)', '',
        '## Absolute values, then paired differences','',
        'The upper panel directly answers the requested comparison: vertical physical quantity, horizontal All cubes and pyromes, with No Fire and VIIRS Fire side by side. Each dot is one cube’s 2015–2023 mean annual value, not an independent pixel-day. The lower panel reveals paired responses that can be visually small beside baseline productivity differences.', '',
        '![GPP: paired absolute values](components/gpp__gpp/absolute_pyrome.png)','',
        'Original pyrome fractions are used within each cube, so group counts overlap. FIL has only seven contributing cubes and two 2-degree spatial blocks: do not rank fire-regime mechanisms from this group alone. The distributions are not confidence intervals.', '',
        '## Local response and ecosystem accounting','',
        '![Fire exposure](figures/fire_exposure.png)','',
        'The full-period cohort comparison includes pre-fire days as well as post-fire days. It is not an estimate of immediate post-fire severity. No-detection cells are identical between scenarios in this pixel-independent experiment; this is an internal control, not proof that no real fire occurred.', '',
        '![Carbon response decomposition](figures/carbon_response_decomposition.png)','',
        'The component identity is Δ(NEE + fire) = −ΔGPP + ΔReco + fire. Internal transfers and mortality are not additional atmospheric emissions. The last bar is the sum of the first three, not another additive term.', '',
        '## Three exploratory high-response regions','',
        'Selected by the three largest final relative vegetation-carbon losses among QA-passed cubes with no-fire cVeg ≥100 g C m⁻² and owned area ≥100 km². They illustrate strong responses and are not a representative sample or evidence that all sites respond similarly.', '',
        '| Region | Final No Fire cVeg | Final VIIRS Fire cVeg | Difference | Area with detections |',
        '|---|---:|---:|---:|---:|']
    for _,x in cases.iterrows():
        lines.append(f"| {case_name(x.region_id)} | {x['final__cVeg__No_Fire']:.1f} | {x['final__cVeg__VIIRS_Fire']:.1f} | {x.final_cVeg_relative_pct:.2f}% | {100*x.fire_affected_fraction:.1f}% |")
    lines += ['', 'Stocks are g C m⁻² on 31 December 2023; percentages compare scenarios on the same owned support.']
    for _,x in cases.iterrows():
        lines += ['',f'### {case_name(x.region_id)}','',f'![Annual response](cases/{x.region_id}/annual_response.png)','',f'[Spatial response](cases/{x.region_id}/spatial_response.pdf)']
    lines += ['', '## Reference comparisons','']+['- '+x for x in reflines]
    lines += ['', '![GPP reference comparisons](references/annual_gpp.png)','',
        '[All annual comparison metrics, including groups](references/annual_reference_metrics.csv) · [GPP common-support metrics](references/common_gpp_metrics.csv)', '',
        'MOD17 and FLUXCOM-X are reference estimates, not ground truth. CCI is compared with woody carbon, not total cVeg. Conversion sensitivity (0.47 versus 0.51 carbon fraction) is retained in the metric table and is not a confidence interval. ET references and GPP references do not share exactly the same support. LAI has no model output.', '',
        '## Full output and group coverage','',
        'Every one of the 58 saved/derived components has new absolute-value distributions in `components/<variable>__<component>/`, including overall, pyrome and vegetation panels plus paired values. Existing pixel-wise scenario and reference plots remain in the previous report. Independent pyrome and vegetation absolute-value atlases and values are in `groups/`. All zero components are retained.', '',
        '## Interpretation, provenance and next tests','',
        '- The two scenarios share weather, initialized pools and static fractions. Fire location is a VIIRS detection proxy; severity, mortality and combustion parameters remain modelling assumptions.',
        '- Population completion is uneven. Unique owned areas prevent overlap double counting; no full-design weights are applied to this incomplete subset.',
        '- File completion is distinct from physical QA. Negative water/ET checks flagged 46 regions, recorded in the prior report; original outputs are unchanged.',
        '- NEE excludes direct fire efflux; carbon pools are states and cannot be accumulated as emissions. Annual fluxes are summed, stocks averaged; 2014 is partial and excluded from complete-year comparisons.',
        '- Saved `restart.nc` is not the first forward state. Existing scenario CSV r² is Pearson r squared; it is not predictive R² or observational validation.',
        '- Recheck the three cases against observed burned area, biomass change and forcing history. Separate uncertainty in fire forcing from uncertainty in fuel/combustion parameters before presenting a mechanistic generalisation.',
        '- Longer-lived carbon deficits and uptake/respiration compensation are testable model hypotheses here. Their novelty relative to the literature has not been established.', '',
        'Source interpretation: [output dictionary](../OUTPUT_SEMANTICS_EN.md); [code and parameter provenance](../dependency_audit.json). Figure values and source fingerprints are saved alongside each component and in `references/complete.json`.', '',
        'Reference methods: [FLUXCOM-X X-BASE, Nelson et al.](https://bg.copernicus.org/articles/21/5079/2024/); [MOD17 C6.1](https://doi.org/10.5067/MODIS/MOD17A2HGF.061); [CCI Biomass v6 manual](https://climate.esa.int/media/documents/D4.3_CCI_PUG_V6.0_20250606.pdf).']
    (out/'SUPERVISOR_BRIEF_EN.md').write_text('\n'.join(lines)+'\n')
    state=dict(snapshot=p['fingerprint'],status='complete',pages=pages,primary_regions=p['primary_regions'],
        component_count=len(p['components']),case_ids=cases.region_id.tolist(),findings=findings,
        reference_summary=reflines,scope='Interim model-based evidence; novelty and observational causality not established')
    old.atomic_json(out/'brief_complete.json',state)
    return state


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('root',type=Path)
    parser.add_argument('stage',choices=['component','discovery','reference','groups','brief']);parser.add_argument('--index',type=int)
    a=parser.parse_args()
    result=component(a.root,a.index) if a.stage=='component' else {'discovery':discovery,'reference':reference_annual,'groups':group_atlases,'brief':brief}[a.stage](a.root)
    print(json.dumps(result))


if __name__=='__main__':main()
