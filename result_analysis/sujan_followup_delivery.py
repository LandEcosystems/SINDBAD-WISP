"""Compact, inspected delivery for Sujan's reference and pre-fire-stock requests."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd

from .sujan_followup import destination, SERIES_COLORS, SCENARIOS
from .report import _set_style, basic_metrics
from .findings_report import savefig
from .email_grouped_comparisons import distribution
from .parallel_report import merge_pdfs
from .io import file_hash


def reference_figure(out):
    pairs=pd.read_csv(out/'reference_annual_pairs_293.csv.gz')
    summary=pd.read_csv(out/'reference_region_means_293.csv')
    layout=[('gpp','MOD17A2HGF.061','GPP · MODIS','g C m⁻² yr⁻¹'),
            ('gpp','FLUXCOM-X_X-BASE_v1.0','GPP · FLUXCOM-X','g C m⁻² yr⁻¹'),
            ('nee','FLUXCOM-X_X-BASE_v1.0','NEE · FLUXCOM-X','g C m⁻² yr⁻¹'),
            ('evapotranspiration','MOD16A2GF.061','ET · MODIS','mm yr⁻¹'),
            ('evapotranspiration','FLUXCOM-X_X-BASE_v1.0','ET · FLUXCOM-X','mm yr⁻¹')]
    plt=_set_style();fig,axes=plt.subplots(2,5,figsize=(19,9))
    for j,(var,product,title,unit) in enumerate(layout):
        v=summary[summary.variable.eq(var)&summary['product'].eq(product)]
        for x,(name,color) in enumerate(zip(SCENARIOS+['reference'],SERIES_COLORS)):
            distribution(axes[0,j],v[name],x,color,width=.7)
        axes[0,j].set_xticks([0,1,2],['No fire','VIIRS\nfire','Reference'],fontsize=9)
        axes[0,j].set_title(title+f'\nn={len(v)} cubes',loc='left',fontsize=11)
        axes[0,j].set_ylabel(unit);axes[0,j].grid(axis='y',alpha=.18)
        q=pairs[pairs.variable.eq(var)&pairs['product'].eq(product)]
        x=q.reference.to_numpy();y=q.VIIRS_Fire.to_numpy();ax=axes[1,j]
        ax.hexbin(x,y,gridsize=45,mincnt=1,bins='log',cmap='cividis',rasterized=True)
        lim=[min(x.min(),y.min()),max(x.max(),y.max())];ax.plot(lim,lim,color='#66747A',ls='--',lw=.8)
        m=basic_metrics(x,y)
        ax.text(.03,.97,f'n={len(q):,} cell-years\nRMSE={m["rmse"]:.1f}\nr={m["r"]:.2f}',transform=ax.transAxes,va='top',fontsize=9,
                bbox=dict(facecolor='white',alpha=.85,edgecolor='none',pad=3))
        ax.set_xlabel('Reference ('+unit+')');ax.set_ylabel('VIIRS-fire SINDBAD ('+unit+')')
    fig.suptitle('Reference products alongside the two SINDBAD scenarios',x=.055,y=.97,ha='left',fontsize=20,weight='semibold')
    fig.text(.055,.91,'2015–2021 complete years · frozen 293-region cohort · reference-grid comparisons',fontsize=12)
    fig.text(.055,.02,'Top: each point is a cube’s seven-year mean annual total on matched reference support (distribution, not a confidence interval).\n'
             'Bottom: one unique reference cell-year; density colour indicates point count. Model aggregated to 0.05° reference support, coverage ≥90%; overlap duplicates removed.\n'
             'GPP products share coarse cells/years; masks can differ. ET products use their own valid supports. Negative NEE = uptake; direct fire is excluded from NEE.\n'
             'MODIS supplies GPP and ET here, not NEE. ET is water flux, not LE in W m⁻². References are estimates, not ground truth; completion subset is not a Mediterranean total.',fontsize=9)
    fig.subplots_adjust(left=.055,right=.985,top=.84,bottom=.20,wspace=.5,hspace=.5)
    savefig(fig,out/'01_reference_comparisons')


def pft_figure(out):
    d=pd.read_csv(out/'prefire_stock_fire_by_region_group.csv')
    d=d[d.group_type.eq('model_pft')].copy();d['group']=d['group'].astype(int)
    codes=sorted(d.group.unique())
    names={1:'Evergreen needleleaf',2:'Evergreen broadleaf',3:'Deciduous needleleaf',
           4:'Deciduous broadleaf',5:'Mixed forest',6:'Closed shrubland',7:'Open shrubland',
           8:'Woody savanna',9:'Savanna',10:'Grassland',11:'Wetland',12:'Cropland',15:'Snow / ice'}
    plt=_set_style();fig,axes=plt.subplots(3,4,figsize=(15,10),sharex=True,sharey=True)
    for ax,g in zip(axes.flat,codes):
        q=d[d.group.eq(g)];ax.scatter(q.prefire_cVeg_gC_m2,q.fire_day_cVeg_gC_m2,color='#276A84',s=12,alpha=.65)
        ax.set_title(f'PFT {g}: {names.get(g,str(g))}\nn={len(q)} cubes',loc='left',fontsize=10)
        ax.set_xscale('symlog',linthresh=1);ax.set_yscale('symlog',linthresh=1);ax.grid(alpha=.15)
        ax.tick_params(labelbottom=True)
    for ax in list(axes.flat)[len(codes):]:ax.set_visible(False)
    fig.suptitle('Model PFT diagnostic: pre-fire stock versus same-day vegetation combustion',x=.07,y=.97,ha='left',fontsize=17)
    fig.supxlabel('Previous-day cVeg (g C m⁻² of affected natural vegetation)',y=.08)
    fig.supylabel('Same-day vegetation combustion (g C m⁻²)',x=.02)
    fig.text(.07,.025,'Model PFT is a parameter diagnostic and may differ from original vegetation; one point per cube/PFT. Only owned natural vegetation is analysed.\n'
             'First recorded VIIRS-positive day per pixel; not observed burned-area efficiency. Common axes: logarithmic beyond 1, linear near zero; zeros retained.',fontsize=9)
    fig.subplots_adjust(left=.08,right=.98,top=.88,bottom=.15,hspace=.55,wspace=.22)
    savefig(fig,out/'03_prefire_stock_by_model_pft')


def negative_figure(out):
    d=pd.read_csv(out/'annual_net_carbon_decomposition_293.csv')
    chosen=d.sort_values('net_fire_contribution').drop_duplicates('region_id').head(3)
    values=chosen[['direct_fire','minus_delta_gpp','delta_eco_respiration','net_fire_contribution']].to_numpy()
    low,high=values.min()*1.3,values.max()*1.3
    plt=_set_style();fig,axes=plt.subplots(1,3,figsize=(15,6),sharey=True)
    for ax,row in zip(axes,chosen.itertuples()):
        vals=[row.direct_fire,row.minus_delta_gpp,row.delta_eco_respiration,row.net_fire_contribution]
        ax.bar(range(4),vals,color=['#CE713B','#B59B42','#276A84','#344957'],width=.65)
        ax.axhline(0,color='#66747A',lw=.7);ax.grid(axis='y',alpha=.18);ax.set_axisbelow(True)
        ax.set_xticks(range(4),['Direct\ncombustion','Reduced GPP\n(−ΔGPP)','Respiration\nchange','Net\ncontribution'],fontsize=9)
        for x,y in enumerate(vals):ax.annotate(f'{y:+.2f}',(x,y),xytext=(0,5 if y>=0 else -15),textcoords='offset points',ha='center',fontsize=10)
        ax.set_ylim(low,high)
        short=row.region_id.split('_');ax.set_title(f'{short[1]} · {short[-1]} · {row.year}',loc='left',fontsize=13)
    axes[0].set_ylabel('g C m⁻² yr⁻¹ (owned natural vegetation)')
    fig.suptitle('Why a negative net fire contribution is not negative combustion',x=.07,y=.97,ha='left',fontsize=18,weight='semibold')
    fig.text(.07,.88,'Net contribution = direct combustion + ΔReco − ΔGPP     (Δ = VIIRS fire − no fire)',fontsize=12)
    fig.text(.07,.025,f'Exploratory cases: most negative annual contribution from three distinct cubes. {d.negative_net_below_minus_001.sum()} of {len(d)} cube-years are below −0.01 g C m⁻² yr⁻¹.\n'
             'Reduced respiration can exceed combustion plus lost uptake in an individual year. This arithmetic does not establish the biological cause.\n'
             'Denominator is all owned natural vegetation, including unburned cells. Annual negative differences do not imply negative direct emissions or a beneficial cumulative fire effect.',fontsize=9)
    fig.subplots_adjust(left=.07,right=.98,top=.78,bottom=.25,wspace=.25)
    savefig(fig,out/'04_negative_net_decomposition')


def package(root):
    out=destination(root)
    states={k:json.loads((out/(k+'_validation.json')).read_text()) for k in ['references','prefire','negative']}
    assert all(s['status']=='pass' for s in states.values())
    reference_figure(out)
    pft_figure(out)
    negative_figure(out)
    files=[out/(n+'.pdf') for n in ['01_reference_comparisons','02_prefire_stock_by_vegetation','04_negative_net_decomposition']]
    assert merge_pdfs(files,out/'SUJAN_FOLLOWUP_THREE_FIGURES_EN.pdf')==3
    pd.DataFrame([
        dict(file='reference_region_means_293.csv',grain='cube × reference product',meaning='Seven-year mean annual totals, paired No_Fire/VIIRS_Fire/reference; matched reference-cell support'),
        dict(file='reference_region_annual_293.csv',grain='cube × year × reference product',meaning='Complete-year totals on paired reference support; 2015–2021'),
        dict(file='reference_annual_pairs_293.csv.gz',grain='unique reference pixel × year × product',meaning='Model aggregated to product support; both scenarios and reference, paired support area; overlap duplicates removed'),
        dict(file='reference_metrics_293.csv',grain='product × variable × scenario',meaning='Bias, RMSE, Pearson r, r² and NSE; each reference-cell-year equally weighted'),
        dict(file='prefire_pixel_pairs_293.csv.gz',grain='owned model pixel × first recorded fire',meaning='Previous-day eight carbon pools, same-day eight pool emissions, original-class overlap areas, model PFT; first calendar-year paired responses also retained'),
        dict(file='prefire_stock_fire_by_region_group.csv',grain='cube × class',meaning='Affected-area-weighted stocks and first-day emissions; original vegetation/IGBP and model PFT are distinct groupings'),
        dict(file='annual_net_carbon_decomposition_293.csv',grain='cube × year',meaning='Direct combustion + ΔReco − ΔGPP = net scenario carbon contribution; denominator is ALL owned natural vegetation')
    ]).to_csv(out/'CSV_INDEX_EN.csv',index=False)
    metrics=pd.read_csv(out/'reference_metrics_293.csv'); vi=metrics[metrics.scenario.eq('VIIRS_Fire')]
    table='\n'.join(f'| {r.variable} | {r.product} | {r.n_regions} | {r.bias:.2f} | {r.rmse:.2f} | {r.r:.3f} |' for r in vi.itertuples())
    pre=states['prefire'];neg=states['negative']
    text=f'''# Response to Sujan: reference comparisons, pre-fire stocks and signed carbon impacts

All source model results remain unchanged. The frozen cohort is 293 completed cubes,
including the previously noted tiny negative water/ET values. Three figures address
the requested questions; an additional model-PFT diagnostic is separate.

## 1. Which requested comparisons already existed?

MOD17 GPP, MOD16 ET and FLUXCOM-X GPP/NEE/ET pairs were already present for all 293
analysed cubes. The earlier combined reference report used the older 247-region
inclusion set; this delivery rebuilds the plots from all 293 regional pair files.
Reference coverage filters still matter: common MODIS/FLUXCOM GPP supports 283
cubes, MODIS ET 254, and FLUXCOM NEE/ET 293. MODIS NEE is not available in this
pipeline. ET is plotted in water units; no LE energy-flux comparison is claimed.

Model estimates are aggregated to the 0.05-degree reference grid, not compared
with replicated coarse reference values as independent fine pixels. Comparisons
use complete years 2015–2021 and at least 90% reference-cell support. Overlaps are
deduplicated by highest coverage and stable region ID, not by model error. GPP
products share geographic reference cells and years; product masks can differ.

VIIRS-fire scenario, annual reference-cell statistics:

| Quantity | Reference | Cubes | Bias | RMSE | Pearson r |
|---|---|---:|---:|---:|---:|
{table}

Carbon units: g C m−2 yr−1; ET: mm yr−1. These metrics describe comparison with
reference estimates, not error against ground truth. Product support differs for
ET, so its two RMSE values are not a controlled product ranking. NEE excludes
direct combustion. The figures show much less NEE spread in SINDBAD and lower ET
than the references on these supports; the source of these differences is not
established by the plots alone.

## 2. What was newly extracted?

The previous analysis had initial/simulation-average stocks and event-aligned
responses, but no exact previous-day-stock/first-day-combustion table. This
delivery reads the saved daily NetCDF output for {pre['first_fire_pixels']:,}
eligible pixels in {pre['regions_with_events']} cubes. For each pixel, the event
is its first VIIRS-positive forcing day within the simulation, not its first-ever
wildfire. {pre['excluded_first_saved_day']} pixels with fire on the first saved
day have no saved previous state and are explicitly excluded.

For pixel p with first positive forcing at t_p:

    prefire_stock[p,k] = cEco[p,k,t_p−1]
    first_day_emission[p,k] = c_fire_efflux[p,k,t_p]
    group_mean = sum(value × owned affected class area) / sum(owned affected class area)

Vegetation emission is paired with vegetation stock; litter with litter; total
emission with total ecosystem stock. A total ecosystem stock is not necessarily
all combustible fuel. The previous day's saved state differs from the exact
within-day state at the fire process. Original 2019 IGBP plant forms use fractional
class overlap, whereas model PFTs reflect the parameter-compatible recoding.
Neither mixed cubes nor savannas are relabelled as a generic mixed category.

The figure normalizes to VIIRS-affected class area. That is a detection/forcing
proxy, NOT independently measured burned area. It is also not adjusted by the
0.99 forcing multiplier to manufacture observed burned area. Relationships
partly reflect the model's explicit stock, mortality and combustion dependence;
they do not independently validate fuel-to-emission causality or efficiency.

## 3. Why can a net contribution be negative?

    ΔF_atmosphere = F_direct + (NEE_VIIRS − NEE_NoFire)
                 = F_direct + ΔReco − ΔGPP

All {neg['n_cube_years']} complete cube-years (2015–2023) have nonnegative direct
combustion. {neg['negative_net_below_minus_001']} have a net contribution below
−0.01 g C m−2 yr−1, a descriptive threshold that avoids interpreting near-zero
differences as substantive. The maximum decomposition residual is
{neg['max_decomposition_residual']:.3g} g C m−2 yr−1, consistent with the separately
rounded saved components. The three selected cases show a reduction in respiration
larger than combustion plus lost uptake in those years. This explains the
arithmetic; whether it reflects reduced live/dead substrates, initialization,
turnover or other model mechanisms requires further process-level diagnosis.
An annual negative difference is not negative combustion and does not establish
a cumulative carbon benefit from fire.

## Files

- `SUJAN_FOLLOWUP_THREE_FIGURES_EN.pdf`: reference comparisons; pre-fire stock by
  plant form; annual negative-net decomposition.
- `03_prefire_stock_by_model_pft.pdf`: additional parameter-group diagnostic.
- `CSV_INDEX_EN.csv`: exact grain and meaning for each data table.
- `*_validation.json`: provenance, reconciliation and coverage checks.

The 293-region completion subset is not an unbiased Mediterranean total.
All figures are descriptive model/reference analyses; no new model run is made.
'''
    (out/'README_EN.md').write_text(text)
    with zipfile.ZipFile(out/'SUJAN_FOLLOWUP_FIGURES_AND_CSV.zip','w',compression=zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.iterdir()):
            if f.is_file() and f.suffix in ['.pdf','.png','.csv','.gz','.md','.json']:z.write(f,f.name)
    with zipfile.ZipFile(out/'SUJAN_FOLLOWUP_FIGURES_AND_CSV.zip') as z:assert z.testzip() is None
    (out/'delivery.json').write_text(json.dumps(dict(status='pass',figures=4,overview_pdf_pages=3,
        renderer_sha256=file_hash(Path(__file__)),extraction_sha256=file_hash(Path(__file__).with_name('sujan_followup.py'))),indent=2)+'\n')
    print(str(out/'SUJAN_FOLLOWUP_THREE_FIGURES_EN.pdf'))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);a=p.parse_args();package(a.root)
