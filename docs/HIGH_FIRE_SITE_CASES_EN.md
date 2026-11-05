# High-fire and negative-net-contribution case studies

This is an exploratory screen of the frozen **293-region** Sujan
`WISP_V1_202610` analysis, including the 46 regions retained with near-zero
water/ET numerical notes. It does not rerun or modify simulations.

The three-page PDF distinguishes two quantities:

\[
F_{\mathrm{fire}}=\sum_k c_{\mathrm{fire\,efflux},k}\geq0
\]

\[
\Delta F = F_{\mathrm{fire}} + NEE_{\mathrm{VIIRS}}-NEE_{\mathrm{NoFire}}
          = F_{\mathrm{fire}}+\Delta R_{\mathrm{eco}}-\Delta GPP.
\]

There are no negative direct fire emissions in the checked snapshot. A negative
annual net contribution means less net carbon release (or greater net uptake)
than in the no-fire counterfactual that year. It does not imply negative burning,
a negative absolute atmospheric flux, or a cumulative carbon benefit.

## Selection

Rank complete-year results (2015–2023) over owned runnable natural vegetation.
Keep both emission intensity and regional carbon mass rankings; do not confuse
them. Do not apply sampling expansion weights to a site example.

1. **T1862, France:** greatest accumulated direct-emission intensity.
2. **T1494, Portugal:** greatest annual direct-emission pulse, and the most negative
   annual net contribution in a later year.
3. **T1771, Montenegro:** second-most-negative annual net contribution, with
   relatively high accumulated direct emissions (intensity rank 10/293).

Each page shows annual direct emissions and net contribution, a signed budget
decomposition for one focus year, and accumulated direct/net contributions.
The two negative focus years remain within positive accumulated contributions.
These selected extremes generate hypotheses; they do not estimate a typical
effect or prove a physical mechanism. A site here is a cube, not a tower station.

## Verification and delivery

Independently integrate all nine complete years from the original daily NetCDFs
for GPP, NEE and ecosystem respiration in both scenarios, plus direct combustion
in the fire scenario. Sum all eight combustion pools, use the owned natural-area
mask, verify coordinates and dates, and reconcile against annual summaries.
Also reconcile monthly and annual summaries. The partial 2014 year is excluded.

Outputs are under
`/Net/Groups/BGI/tscratch/ybai/sindbad_wisp_result_analyse_v1/report_findings_v3_all293/fire_site_cases_v1/`:

- `HIGH_FIRE_AND_NEGATIVE_NET_CASES_EN.pdf`, three pages, plus individual PNG/PDFs.
- `ranked_sites_2015_2023.csv` and `ranked_annual_net_contributions.csv`.
- `annual_fire_metrics_all293.csv`, selection/source identity and raw validation.
- `FINDINGS_EN.md` and `SITE_CASES_EN.zip`.

Run the frozen analysis environment with pypdf available:

```sh
python -m result_analysis.fire_site_cases ROOT screen
python -m result_analysis.fire_site_cases ROOT raw
python -m result_analysis.fire_site_cases ROOT figures
python -m result_analysis.fire_site_cases ROOT package
```

All values are carbon mass. The model does not separately resolve emitted CO₂,
CO and CH₄; multiplying carbon by 44/12 would be an explicit mass-equivalent
conversion, not a gas-speciation result.
