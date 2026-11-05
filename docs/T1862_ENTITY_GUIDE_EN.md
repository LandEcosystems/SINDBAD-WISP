# T1862 output guide and download package

The Chinese guide follows `README_SINDBAD_WISP_OUTPUT.md`, section 6, with
the corrections in `docs/OUTPUT_SEMANTICS_EN.md`. The selected entity is
`B1_FRA_Pyrenees-Atlantics_near-Biarritz_43.1892N_001.1351W_T1862`.
Its footprint crosses the France–Spain border. It ranks first in cumulative
direct fire **intensity** over 2015–2023 among the frozen 293 regions, but
second in direct mass. Carbon output does not resolve CO2/CO/CH4 separately.

## Delivery

Directory:
`/Net/Groups/BGI/tscratch/ybai/sindbad_wisp_result_analyse_v1/entity_case_studies/T1862_v1`

- `T1862_QUICK_GUIDE_CN.pdf`: three-page orientation and scientific summary.
- `T1862_FIGURE_GUIDE_CN.pdf`: 92 pages, including the 100 comparison plots
  and 78 forcing plots, with physical interpretation for all 50 output components.
- `guide/T1862_EVERY_FIGURE_CN.md`: all 378 original images, including the
  200 single-scenario output plots, with explanations.
- `tables/all_378_figures_explained.csv`: one record per original PNG.
- `tables/all_50_components.csv`: complete-year flux integrals / mean stocks,
  paired effects, and final stocks where applicable.
- `T1862_GUIDE_AND_FIGURES.zip`: approximately 63 MiB, portable reading package.
- `T1862_INPUT_AND_ALL_OUTPUTS.zip`: approximately 4.02 GiB, including the
  input NetCDF, both scenarios, original code/settings/figures/CSV/Julia summaries,
  input masks/ownership, and the saved PFT parameter lookup.
- `MANIFEST_SHA256.csv` inventories the full-package sources, including items
  intentionally absent from the light package. `ARCHIVES.json` supplies ZIP hashes.

All 29 NetCDF payloads were compared against source SHA256 after packaging;
both ZIPs passed CRC verification. The 378 image links are unique, exhaustive,
and decodable. Source metadata still matches the previous scientific QA.
No simulations were run and no original scientific files were changed.

## Reproduction

From this branch with the existing analysis environment:

```bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 python -m result_analysis.entity_case_guide
```

The program requires the existing analysis summaries, source outputs, Natural
Earth country boundaries, and Droid Sans Fallback. `--no-package` renders the
guide only; `--package-only` packages an already rendered guide. It never writes
into the source entity or input cube directories.

Scientific values use owned runnable natural-vegetation area, including unburned
cells. Original PNG averages are unweighted native finite-pixel means; they are
not numerically interchangeable. In particular, the annual panels in original
flux figures are average daily rates, not annual totals. The 2014 partial year
is kept in the source data but excluded from nine-full-year comparisons.
