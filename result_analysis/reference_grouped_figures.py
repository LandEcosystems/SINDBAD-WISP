"""The approved reference layout, by pyrome, plant form and forest x pyrome."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd

from .reference_grouped_data import destination, area_name, GROUPS, VALUES, MIN_GROUP_AREA_M2
from .report import _set_style
from .findings_report import savefig
from .email_grouped_comparisons import distribution
from .sujan_followup import SERIES_COLORS, SCENARIOS
from .parallel_report import merge_pdfs
from .io import atomic_json, file_hash

LAYOUT=[('gpp','MOD17A2HGF.061','MODIS GPP','g C m⁻² yr⁻¹'),
        ('gpp','FLUXCOM-X_X-BASE_v1.0','FLUXCOM-X GPP','g C m⁻² yr⁻¹'),
        ('nee','FLUXCOM-X_X-BASE_v1.0','FLUXCOM-X NEE','g C m⁻² yr⁻¹'),
        ('evapotranspiration','MOD16A2GF.061','MODIS ET','mm yr⁻¹'),
        ('evapotranspiration','FLUXCOM-X_X-BASE_v1.0','FLUXCOM-X ET','mm yr⁻¹')]
PYROME_ORDER=['FCS','FIL','ICS','RCS','RIL','unknown']
VEG_ORDER=['forest','shrub','savanna','grass','wetland']
VEG_LABEL={'forest':'Forest','shrub':'Shrubland','savanna':'Savanna','grass':'Grassland','wetland':'Wetland'}
PDFS={'pyrome':'REFERENCES_BY_PYROME_EN.pdf','vegetation':'REFERENCES_BY_VEGETATION_EN.pdf',
      'forest_pyrome':'FOREST_REFERENCES_BY_PYROME_EN.pdf'}


@lru_cache(maxsize=1)
def frames(out):
    out=Path(out)
    return (pd.read_parquet(out/'grouped_reference_pairs.parquet'),
            pd.read_csv(out/'grouped_region_means.csv'),
            pd.read_csv(out/'grouped_reference_metrics.csv'),
            json.loads((out/'plot_scales.json').read_text()))


def choose_scales(out):
    """Fixed scales across groups; natural-area bins bound all subgroup bins."""
    pairs=pd.read_parquet(out/'grouped_reference_pairs.parquet')
    summary=pd.read_csv(out/'grouped_region_means.csv')
    plt=_set_style();fig,ax=plt.subplots();scales=[];maximum=0.
    for var,product,title,unit in LAYOUT:
        q=pairs[pairs.variable.eq(var)&pairs['product'].eq(product)]
        values=q[VALUES].to_numpy();low,high=values.min(),values.max();pad=.025*(high-low)
        lim=[float(low-pad),float(high+pad)]
        v=summary[summary.variable.eq(var)&summary['product'].eq(product)][VALUES].to_numpy()
        vlo,vhi=v.min(),v.max();vpad=.08*(vhi-vlo)
        scales.append(dict(variable=var,product=product,scatter=lim,distribution=[float(vlo-vpad),float(vhi+vpad)]))
        for scenario in SCENARIOS:
            h=ax.hexbin(q.reference,q[scenario],C=q.natural_area_m2/1e6,reduce_C_function=np.sum,
                        gridsize=45,mincnt=1,extent=(*lim,*lim))
            a=h.get_array();maximum=max(maximum,float(a.max()));ax.clear()
    plt.close(fig)
    atomic_json(out/'plot_scales.json',dict(products=scales,color_vmin=.01,color_vmax=maximum,
        color_under='Positive bins below 0.01 km2 yr remain visible in the lowest colour',
        color_quantity='Summed class area (km2) over cell-year observations in each hexagon',
        axes='Identical per product across all groups and both scenarios'))


def plot_one(task):
    from matplotlib.colors import LogNorm
    from matplotlib.cm import ScalarMappable
    out,family,group=task;out=Path(out)
    pairs,summary,metrics,scales=frames(str(out))
    col=area_name(family,group)
    d=pairs[pairs[col]>MIN_GROUP_AREA_M2]
    means=summary[summary.group_type.eq(family)&summary.group.eq(group)]
    metric=metrics[metrics.group_type.eq(family)&metrics.group.eq(group)]
    label=('Forest × pyrome '+('Unknown' if group=='unknown' else group) if family=='forest_pyrome'
           else VEG_LABEL[group] if family=='vegetation' else 'Pyrome '+('Unknown' if group=='unknown' else group))
    folder=out/family;folder.mkdir(exist_ok=True)
    plt=_set_style();fig,axes=plt.subplots(3,5,figsize=(19,13))
    norm=LogNorm(vmin=scales['color_vmin'],vmax=scales['color_vmax'])
    plotcounts=[]
    for j,(var,product,title,unit) in enumerate(LAYOUT):
        v=means[means.variable.eq(var)&means['product'].eq(product)]
        q=d[d.variable.eq(var)&d['product'].eq(product)]
        limits=scales['products'][j]
        for x,(name,color) in enumerate(zip(VALUES,SERIES_COLORS)):
            distribution(axes[0,j],v[name],x,color,width=.7)
        axes[0,j].set_xticks([0,1,2],['No fire','VIIRS\nfire',title.replace(' ','\n')],fontsize=9)
        axes[0,j].set_title(title+f'\nn={len(v)} cubes',loc='left',fontsize=11)
        axes[0,j].set_ylabel(unit);axes[0,j].grid(axis='y',alpha=.18)
        axes[0,j].set_ylim(limits['distribution'])
        if v.empty:axes[0,j].text(.5,.5,'No complete seven-year\nregional comparisons',ha='center',transform=axes[0,j].transAxes,fontsize=10)
        lim=limits['scatter']
        for row,(scenario,slabel) in enumerate(zip(SCENARIOS,['No-fire SINDBAD','VIIRS-fire SINDBAD']),start=1):
            ax=axes[row,j];ax.set_xlim(lim);ax.set_ylim(lim)
            ax.set_xlabel(title+'\n('+unit+')');ax.set_ylabel(slabel+'\n('+unit+')')
            ax.plot(lim,lim,color='#66747A',ls='--',lw=.8)
            if q.empty:
                ax.text(.5,.5,'No matched class area',ha='center',transform=ax.transAxes)
                continue
            weights=q[col].to_numpy()/1e6
            h=ax.hexbin(q.reference,q[scenario],C=weights,reduce_C_function=np.sum,
                        gridsize=45,mincnt=1,extent=(*lim,*lim),cmap='cividis',norm=norm,rasterized=True)
            np.testing.assert_allclose(h.get_array().sum(),weights.sum(),rtol=1e-10)
            m=metric[metric.variable.eq(var)&metric['product'].eq(product)&metric.scenario.eq(scenario)].iloc[0]
            corr='undefined' if not np.isfinite(m.pearson_r) else f'{m.pearson_r:.2f}'
            ax.text(.03,.97,f'n={len(q):,} cell-years\nArea-wtd RMSE={m.rmse:.1f}\nArea-wtd r={corr}',
                    transform=ax.transAxes,va='top',fontsize=9,
                    bbox=dict(facecolor='white',alpha=.88,edgecolor='none',pad=3))
        plotcounts.append(dict(variable=var,product=product,distribution_cubes=len(v),
                               paired_cubes=q.region_id.nunique(),cell_years=len(q)))
    colorbar=fig.colorbar(ScalarMappable(norm=norm,cmap='cividis'),cax=fig.add_axes([.77,.095,.21,.012]),orientation='horizontal',extend='min')
    colorbar.set_label('Class-area × years per hexagon (km² yr; log scale)',fontsize=9)
    colorbar.ax.tick_params(labelsize=8)
    fig.suptitle('SINDBAD compared with MODIS and FLUXCOM-X | '+label,x=.055,y=.97,ha='left',fontsize=19,weight='semibold')
    fig.text(.055,.93,'2015–2021 · 293-region cohort · class-area-weighted agreement on 0.05° reference supports',fontsize=12)
    fig.text(.055,.02,
        'Top: one cube’s seven-year mean annual total, class-area weighted within each year. Boxes: median/IQR; violin omitted when n<10.\n'
        'Middle: no fire. Bottom: VIIRS fire. Identical paired cells/years in both scenarios; common axes across groups. Dashed line: 1:1.\n'
        'Whole-cell model and product values are weighted by class overlap area; these are NOT unmixed, class-specific flux observations. Mixed cubes can contribute to several groups.\n'
        'Forest = original 2019 IGBP 1–5; forest × pyrome uses their source-cell intersection, not multiplied marginal fractions. Pyrome unknowns retained.\n'
        'Coverage ≥90%; reference-cell overlaps deduplicated. ET supports differ by product; negative NEE = uptake, excluding combustion. Descriptive completion subset, not regional totals.',fontsize=9)
    fig.subplots_adjust(left=.055,right=.985,top=.865,bottom=.19,wspace=.5,hspace=.6)
    stem=folder/(family+'_'+group)
    savefig(fig,stem)
    atomic_json(stem.with_suffix('.json'),dict(status='pass',group_type=family,group=group,plots=plotcounts,
        pair_weight_column=col,hexbin_weight_conservation='pass',renderer_sha256=file_hash(Path(__file__))))
    print('figure complete:',family,group,flush=True)
    return str(stem.with_suffix('.pdf'))


def build(root,workers=4):
    out=destination(root)
    assert json.loads((out/'data_validation.json').read_text())['status']=='pass'
    choose_scales(out)
    tasks=[(str(out),f,g) for f,groups in [('pyrome',PYROME_ORDER),('vegetation',VEG_ORDER),('forest_pyrome',PYROME_ORDER)] for g in groups]
    with ProcessPoolExecutor(max_workers=workers) as pool:paths=list(pool.map(plot_one,tasks))
    for family,target in PDFS.items():
        selected=[p for p,t in zip(paths,tasks) if t[1]==family]
        assert merge_pdfs(selected,out/target)==len(selected)
    coverage=pd.read_csv(out/'group_coverage.csv')
    lines=['| Group | MODIS GPP | FLUXCOM GPP | FLUXCOM NEE | MODIS ET | FLUXCOM ET |',
           '|---|---:|---:|---:|---:|---:|']
    index=[]
    for _,family,group in tasks:
        counts=[]
        for var,product,_,_ in LAYOUT:
            q=coverage[coverage.group_type.eq(family)&coverage.group.eq(group)&coverage.variable.eq(var)&coverage['product'].eq(product)]
            counts.append(str(int(q.iloc[0].n_regions)) if len(q) else '0')
        lines.append('| '+family+': '+group+' | '+' | '.join(counts)+' |')
        index.append(dict(group_type=family,group=group,pdf=f'{family}/{family}_{group}.pdf',
                          png=f'{family}/{family}_{group}.png',paired_cube_counts=';'.join(counts)))
    pd.DataFrame(index).to_csv(out/'FIGURE_INDEX.csv',index=False)
    (out/'README_EN.md').write_text('''# Reference comparisons by pyrome, vegetation and forest × pyrome

These figures extend the approved overall 293-region comparison without rerunning
models or changing its source pairs. They use complete 2015–2021 years, the same
0.05-degree model/reference support, 90% coverage rule, overlap deduplication and
product identities. Products are named explicitly and both SINDBAD scenarios are
shown against the same reference values. No MODIS NEE or simulated LAI is invented.

## Meaning of a grouped comparison

Each coarse cell can contain several vegetation classes. The plotted values remain
the **whole-cell** model and reference annual fluxes. Original-class intersection
area weights the agreement, not the value itself. This is a class-associated,
area-weighted comparison; it is **not an unmixed forest-only GPP/NEE/ET estimate**.
Selecting a forest-bearing coarse cell does not turn its MODIS/FLUXCOM estimate into
a pure-forest observation. Model-only fine-scale class responses are available in
the separate scenario-group analyses; those have a different spatial support.

Original vegetation: forest IGBP 1–5, shrubland 6–7, savanna 8–9, grassland 10 and
wetland 11, from the frozen 2019 map. Model-compatible recoded PFT is not used here.
Pyrome uses the existing official Archibald classification; unknown remains separate.
Forest × pyrome is the intersection on the SAME original classification cells.
It is never forest fraction multiplied by the marginal pyrome fraction.

For product support j and group g, with model-cell overlap O and class fraction f:

    A[j,g] = sum_p O[j,p] * f[p,g]
    regional_mean[g,t] = sum_j A[j,g] * flux[j,t] / sum_j A[j,g]
    seven_year_mean[g] = sum_t regional_mean[g,t] / 7
    RMSE[g] = sqrt(sum_jt A[j,g]*(model[j,t]-reference[j,t])**2 / sum_jt A[j,g])

The original crosswalk class fraction is assumed uniform within a fine model cell
for its intersection with a reference boundary, matching the existing area adapter.
Regional top-row points are equally represented in the distribution; each is already
area weighted within region/year. Lower-row colour is summed class-area × years,
and correlation/RMSE use the same area weights. Scatter sample sizes count unique
reference-cell-years, not independent fine pixels. No uncertainty interval is implied.
Axes and colour scales are fixed across all groups; two scenarios share exact pairs.

Reference supports are retained when group area exceeds a 0.01 m² numerical
boundary-intersection tolerance; the negligible omitted area is recorded in
`numerical_boundary_slivers.csv`. Raw class weights remain unchanged. No purity threshold
is imposed after seeing results. `group_coverage.csv` also records how many supports
have ≥90% class area and their total class-area share, exposing mixed-support limits.
Rare groups show points and boxes only below ten cubes; absent groups are explicit.

## Files

- `REFERENCES_BY_PYROME_EN.pdf`: five named pyromes plus unknown, six pages.
- `REFERENCES_BY_VEGETATION_EN.pdf`: the five original broad plant forms, five pages.
- `FOREST_REFERENCES_BY_PYROME_EN.pdf`: forest-area-weighted comparisons within each pyrome, six pages.
- Per-group PNG/PDF files are in matching subdirectories; see `FIGURE_INDEX.csv`.
- `grouped_region_means.csv`: the exact top-row regional points.
- `grouped_region_annual.csv`: their annual inputs and effective group areas.
- `grouped_reference_metrics.csv`: both-scenario area-weighted accuracy statistics.
- `grouped_reference_pairs.parquet`: frozen cell-year pairs with all class weights.
- `reference_support_group_areas.parquet`: unique spatial supports and intersections.
- `group_coverage.csv`: areas, paired cubes, support counts and class mixing diagnostics.
- `support_cache_v2/`: per-region source fingerprints, area reconciliation and resumable results.

## Number of cubes contributing paired observations

'''+ '\n'.join(lines)+'''

Counts can overlap between groups. Top-row distributions require all seven years
and therefore can have fewer cubes than paired-cell metrics. The 293-region set is
an interim completion subset; no design-unbiased Mediterranean total is claimed.
Original source model outputs, overall figure and prior extraction tables remain
unchanged. GFAS/pyrome-based original sample selection is not a reference validation.
''')
    atomic_json(out/'delivery.json',dict(status='pass',figures=len(tasks),pdf_collections=PDFS,
        renderer_sha256=file_hash(Path(__file__)),data_validation_sha256=file_hash(out/'data_validation.json'),
        source_pairs_sha256=json.loads((out/'data_validation.json').read_text())['source_sha256']))
    with zipfile.ZipFile(out/'GROUPED_REFERENCE_FIGURES_AND_TABLES.zip','w',compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.rglob('*')):
            if p.is_file() and not any(part.startswith('support_cache') for part in p.parts) and p.suffix in ['.pdf','.png','.csv','.md','.json']:
                z.write(p,str(p.relative_to(out)))
    with zipfile.ZipFile(out/'GROUPED_REFERENCE_FIGURES_AND_TABLES.zip') as z:assert z.testzip() is None
    print('Grouped figures and PDF collections ready:',out,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);p.add_argument('--workers',type=int,default=4)
    a=p.parse_args();build(a.root,a.workers)
