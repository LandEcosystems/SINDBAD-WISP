**Subject: SINDBAD–WISP update: cube production and preliminary findings**

Hi Nuno, hi Sujan,

Here is a brief update on Batch 1 and the analysis of Sujan's `WISP_V1_202610` experiments.

**1. The 400-cube dataset is nearly complete**

As of the latest check on 5 November, **384 of 400 cubes (96%) have been built, validated and published**, and the remaining 16 production jobs are running. Based on recent progress, I would provisionally allow **another 2–4 days** for the remaining cubes and final checks, depending on I/O and any retries. The cubes cover 12 January 2014–31 December 2023, with the agreed static vegetation and tree-cover backgrounds.

**2. Three preliminary examples from the 293-region analysis**

I have analysed a snapshot of **293 regions**, comparing **No_Fire and VIIRS_Fire**. The current strict QA gate retains 247 regions in the main analysis and flags 46 for review; the magnitude of these numerical flags is discussed below. The three examples are from the QA-passed subset. They need joint interpretation and are not yet Mediterranean-wide conclusions. A genuine WISP-predicted-fire scenario is not included in this comparison.

**Example A — Similar productivity, but a persistent carbon-stock deficit.** Near Sétif, Algeria, the VIIRS-fire scenario has only **0.99% lower mean annual GPP** over 2015–2023, but **13.4% less vegetation carbon at the end of 2023**. This raises a useful question: could a GPP-only evaluation miss an important disturbance response in biomass stocks? I would like to examine the individual pools, allocation and turnover processes next.

![Figure 1. Sétif: annual GPP, annual mean vegetation carbon and final vegetation carbon.](01_Setif_productivity_vs_carbon_stock.png)

**Example B — Direct combustion changes the sink/source diagnosis.** Near Vila Real, Portugal, 2016 NEE in the VIIRS-fire simulation is **−25.3 g C m⁻² yr⁻¹**. Adding **33.4 g C m⁻² yr⁻¹** of direct combustion gives **+8.2 g C m⁻² yr⁻¹**: a net source for that modelled region and year. I independently recomputed the totals from the original daily outputs. This is directly relevant to T9.2: NEE excludes combustion, so we need to report both terms consistently. These are carbon-mass values, not separately resolved CO₂ emissions.

![Figure 2. Portugal: NEE versus NEE plus combustion, with the 2016 carbon budget.](02_Portugal_sink_source_with_combustion.png)

**Example C — A joint carbon–water discrepancy that persists without fire.** In two Nile Delta regions, both scenarios produce much lower GPP and ET than MODIS and FLUXCOM-X on matched reference cells and years. This points to a baseline diagnostic question: water supply, land-cover/support and parameterization should be checked before attributing the discrepancy to fire. The reference products disagree in magnitude, but both are well above the simulations.

![Figure 3. Two Nile Delta regions: matched annual GPP and ET comparisons for 2015–2021.](03_Nile_Delta_carbon_water_diagnostic.png)

**3. Can the fire-related carbon contribution be negative?**

**Yes for the annual net scenario difference; not for direct combustion in the outputs checked.** Define the atmospheric contribution as:

**ΔF = [NEE(VIIRS_Fire) − NEE(No_Fire)] + fire_efflux(VIIRS_Fire)**

For the same Portugal region in **2018**:

| Contribution | g C m⁻² yr⁻¹ |
|---|---:|
| Change in NEE: VIIRS_Fire − No_Fire | −3.331 |
| Direct combustion | +0.632 |
| Net atmospheric contribution, ΔF | **−2.699** |

Respiration declines more than GPP in that year: approximately **−24.34 versus −21.01 g C m⁻² yr⁻¹**, respectively. The resulting NEE change more than offsets the positive combustion term. This means lower net carbon release, or greater net uptake, relative to the no-fire counterfactual in that particular year—not “negative burning.” The cumulative contribution over **2015–2023 remains positive, about +85.7 g C m⁻²**. The annual offset therefore does not imply an overall carbon benefit from fire.

**4. Numerical issues: distinguishing negative fluxes from invalid stocks**

The checks have **not found negative carbon stocks or negative direct fire efflux** in this 293-region snapshot. Negative NEE itself is expected when uptake exceeds respiration.

There are small negative ET values in 27 regions, reaching approximately **−5.6 × 10⁻⁸ mm/day**, and small negative soil-water values in 46 regions, reaching **−3.8 × 10⁻⁶ mm**. Their near-zero magnitude is consistent with floating-point residuals. One plausible propagation path in the saved code is the soil-water-limited evaporation term, whose available-water input is not explicitly clipped at zero.

I will trace representative timesteps and review suitable numerical tolerances before treating these strict-zero flags as physical failures. Original outputs remain unchanged. These tiny numerical negatives should also be distinguished from the much larger model–reference discrepancies in Example C.

**5. Report and next steps**

The full report is being written, including overall results and separate pyrome and vegetation-type comparisons. I will share an online version shortly so that we can review and revise it together. The three figures above are also attached as a compact PDF.

I will continue the analysis tomorrow. Hope to discuss more! :)

Best,

Yuchen
