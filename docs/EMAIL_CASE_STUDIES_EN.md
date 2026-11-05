# Three case studies for the SINDBAD–WISP progress email

Prepared: 5 November 2026. This is an exploratory, evidence-linked case selection, not a claim of established scientific novelty. The email is a draft and has not been sent.

## Scope and relevance

The frozen analysis snapshot contains 293 processed regions and 247 regions passing the primary physical QA gate. The other 46 remain flagged for investigation. These counts describe the analysis snapshot, not the current completion status of all simulation jobs. All four regions shown in the three figures belong to the QA-passed subset. Only `No_Fire` and `VIIRS_Fire` are included; the name `WISP_V1_202610` is a deployment name, not evidence that a genuine WISP-predicted-fire scenario has run.

The research emphasis is inferred from public publications and research profiles, rather than from assumed personal preferences:

- **Nuno Carvalhais:** model–data integration using carbon and water states and fluxes, ecosystem carbon sink/source strength, biomass dynamics and carbon turnover. The Sétif stock–flux contrast and the Portugal sink/source accounting are closely related to these questions. [MPI Model–Data Integration group](https://www.bgc-jena.mpg.de/bgi/mdi).
- **Sujan Koirala:** carbon–water model–data integration, process-based diagnosis of structural and observational uncertainties, and land carbon/water budgets. The joint GPP–ET discrepancy and the explicit combustion budget provide concrete diagnostic questions. [MPI researcher profile](https://www.bgc-jena.mpg.de/person/skoirala/6332698).
- Both coauthored **Wang et al. (2024), Understanding Disturbance Regimes From Patterns in Modeled Forest Biomass**, which investigates information about disturbance regimes contained in biomass patterns. This supports asking whether stock information reveals responses that flux information alone misses; it does not validate our present simulations. [Paper DOI](https://doi.org/10.1029/2023MS004099), [institutional full text](https://research.unl.pt/ws/portalfiles/portal/100161043/Understanding_Disturbance_Regimes_From_Patterns_in.pdf).

## Figure 1 — Similar GPP, different vegetation carbon

**Region:** `B1_DZA_Setif_near-Setif_36.4595N_005.3514E_T0765`.

**Caption:** Paired No_Fire and VIIRS_Fire simulations near Sétif, Algeria. Panel A shows complete-year GPP totals; panel B shows annual mean vegetation carbon; panel C shows vegetation carbon on 31 December 2023. Means use the same owned, runnable natural-vegetation area (approximately 797 km²), preventing overlapping cubes from being counted twice. The 2014 partial year is excluded from annual summaries.

| Quantity | No_Fire | VIIRS_Fire | Difference |
|---|---:|---:|---:|
| Mean annual GPP, 2015–2023 (g C m⁻² yr⁻¹) | 1094.76 | 1083.88 | −0.99% |
| Final vegetation carbon, 31 Dec 2023 (g C m⁻²) | 747.64 | 647.51 | −13.39% |

`cVeg = cVegRoot + cVegWood + cVegLeaf + cVegReserve`. The final-state difference is not the annual-mean stock difference. The final states were independently checked against the original NetCDF across all owned runnable cells.

**Interpretation:** production and carbon storage need not exhibit similar relative responses. Stock information may therefore supply a disturbance constraint that GPP alone cannot. This case was selected for a strong final vegetation-carbon response among QA-passed regions with baseline vegetation carbon at least 100 g C m⁻² and owned area at least 100 km²; it is not a typical effect estimate.

**Next test:** separate leaf, wood, root and reserve responses, allocation and turnover. The active fAPAR formulation depends on leaf carbon; static cover does not remove all vegetation feedbacks. Do not infer a missing feedback, rapid recovery or a specific mechanism from these three panels alone.

## Figure 2 — Combustion changes the sink/source diagnosis

**Region:** `B1_PRT_Viseu_near-Vila-Real_41.0270N_008.0541W_T1494`.

**Caption:** Annual ecosystem carbon exchange over the same approximately 1,444 km² of owned runnable natural vegetation. Negative values indicate uptake. Panel A separates NEE from NEE plus direct combustion; panel B shows the 2016 accounting in the VIIRS-fire scenario.

\[
NEE=R_{eco}-GPP,\qquad
F_{combustion}=\sum_{k=1}^{8}c_{fire\_efflux,k},\qquad
F_{atmosphere}=NEE+F_{combustion}.
\]

For 2016:

\[
-25.28+33.44=+8.17\quad\mathrm{g\ C\ m^{-2}\ yr^{-1}}.
\]

No_Fire NEE is −55.45 g C m⁻² yr⁻¹. These are complete 366-day integrals, independently recomputed from the original NEE and fire-efflux NetCDF files. The maximum discrepancy against the cached annual summaries was approximately 5.4×10⁻⁷ g C m⁻².

**Interpretation:** negative NEE alone does not establish a carbon sink when combustion is separate. Direct combustion changes the diagnosis in this particular year and region. Mortality and internal transfers must not be added again as atmospheric emissions. Carbon is not gas-speciated CO₂; these numbers do not independently quantify CO₂, CO or CH₄. This is a selected model outcome, not an observed estimate for Portugal.

**Next test:** examine event-year combustion alongside subsequent GPP, respiration and stock differences across all eligible regions. Distinguish direct-emission comparison with an inventory from comparison of the complete ecosystem carbon balance.

## Figure 3 — Joint GPP and ET mismatch in two Nile Delta regions

**Regions:** `B1_EGY_Kafr-el-Sheikh_near-Kafr-el-Sheikh_31.0811N_030.8649E_T0125` and `B1_EGY_Beheira_near-Damanhur_31.0811N_030.4324E_T0124`.

**Caption:** Annual GPP and ET for 2015–2021 from both model scenarios, MODIS and FLUXCOM-X. Within each variable, products use the same reference cells and complete years, with model aggregation to reference support and the existing minimum 90% support rule. GPP and ET have different matched-cell sets. These are equal-weighted reference-cell averages, not owned-area regional totals or weighted Mediterranean estimates.

| Region | Variable | VIIRS-fire model | FLUXCOM-X | MODIS | Cells × years |
|---|---|---:|---:|---:|---:|
| T0125 | GPP (g C m⁻² yr⁻¹) | 39.4 | 1713.9 | 1230.9 | 64 × 7 |
| T0124 | GPP (g C m⁻² yr⁻¹) | 89.7 | 1837.8 | 1326.8 | 63 × 7 |
| T0125 | ET (mm yr⁻¹) | 84.4 | 883.2 | 484.3 | 31 × 7 |
| T0124 | ET (mm yr⁻¹) | 98.4 | 899.5 | 488.2 | 34 × 7 |

MODIS is MOD17A2HGF C6.1 for GPP and MOD16A2GF C6.1 for ET. The two model scenarios are nearly coincident. The cases were selected through a large GPP-discrepancy screen, then evaluated for the additional ET evidence. They are not a random validation sample.

**Interpretation:** a large discrepancy in both carbon and water fluxes, present without fire, is a baseline diagnostic priority. Agreement in the direction of the mismatch does not mean the two references agree on its magnitude or are independent truth.

**Next test:** inspect precipitation and water supply (including whether irrigation is relevant and represented), land-cover classification, vegetation fractions, effective reference support and process parameters. None of these causes is established by this figure. Additional tuning should not replace checking the inputs and support first.

## Shared limitations and existing comparison figures

- Existing per-cube time-series comparisons plot mean daily rates at monthly/annual aggregation. They cannot be read as accumulated annual carbon emissions. The new plots explicitly integrate fluxes and distinguish mean stocks from endpoint stocks.
- The model uses the VIIRS-based disturbance proxy (`0.99 × binary detection`); it is not an observed 99% burned-area fraction.
- Both scenarios share meteorology, initialization, parameters and static vegetation/tree-cover backgrounds. The paired model contrast supports diagnosis under this experimental design, not independent real-world causal attribution.
- No measured fire CO₂ reference or genuine WISP prediction is shown here. Reference products are estimates and can share underlying observations with model calibration.
- No sampling confidence interval or population-wide novelty claim is attached to the selected cases. The incomplete, compositionally uneven analysis subset cannot support an unqualified Mediterranean total.

## Reproduction and evidence

From the `result-analyse` checkout, use the audited analysis Python environment and the existing PDF dependency path:

```bash
python -m result_analysis.email_case_studies /Net/Groups/BGI/tscratch/ybai/sindbad_wisp_result_analyse_v1 raw-check
python -m result_analysis.email_case_studies /Net/Groups/BGI/tscratch/ybai/sindbad_wisp_result_analyse_v1 figures
python -m result_analysis.email_case_studies /Net/Groups/BGI/tscratch/ybai/sindbad_wisp_result_analyse_v1 package
```

Outputs are under `report_findings_v2/email_three_findings_v1/`: three PNG/PDF figures, a three-page PDF, source tables, matched reference pairs, raw-integral verification and an evidence manifest. The evidence manifest binds the figures to the existing snapshot and source checksums. The English email draft is in `docs/EMAIL_DRAFT_THREE_FINDINGS_EN.txt`; it is copied into the delivery directory without sending it.
