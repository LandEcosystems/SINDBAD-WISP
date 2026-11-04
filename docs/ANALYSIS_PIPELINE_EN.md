# SINDBAD–WISP analysis pipeline

## Purpose and experiment

The workflow analyses the saved `WISP_V1_202610` paired experiments without
changing or rerunning them. It distinguishes three questions: numerical and
physical consistency, agreement with reference estimates, and the response to
the prescribed fire forcing. Passing one does not establish the other two.

The initial inventory freezes 293 completed regions, two scenarios (`No_Fire`
and `VIIRS_Fire`), and 3,641 days from 12 January 2014 through 31 December 2023.
All 13 saved variables and 50 layer components are registered. LAI is absent;
the WISP-copy placeholder is not a prediction scenario. New results must enter
a new versioned inventory rather than silently changing this snapshot.

## Processing sequence

| Stage | Operation | Inspectable evidence |
|---|---|---|
| Inventory | Freeze region IDs, source files, saved code and configuration | `inventory.json`, `region_ids.txt`, `variable_registry.json` |
| Dependency audit | Decode effective JLD2 settings and PFT parameters; verify masks and unique spatial ownership | `dependency_audit.json`, per-region provenance |
| Scientific analysis | Read native NetCDF chunks; profile every component; test identities and budgets with propagated rounding bounds | `qa.json`, `profile.csv`, `qa_maps.npz`, source checksums |
| Compact summaries | Integrate complete flux periods; preserve stocks, endpoints, seasonal and event-aligned responses | `annual.csv`, `monthly.csv`, `compact.nc`, `spatial.npz`, `recovery.csv` |
| Reference preparation | Verify source identity, units, dates, quality fields and version uniqueness | Shared reference-cache manifests and source receipts |
| Reference comparison | Aggregate model values by actual overlap area; match exact periods; retain excluded pairs | Native-support paired tables, metrics, coverage and exclusion reasons |
| Synthesis | Separate overall, original pyrome, original vegetation and model-PFT diagnostics; quantify paired responses | Group tables, block/year intervals, independent figures and English reports |

Each region has a unique processing lock. Outputs are completed atomically and
bound to their source, configuration and numerical-code identities. Recorded
checksums are verified before downstream use or reuse. The raw scientific
results remain read-only; no second full daily simulation dataset is produced.

## Scientific contracts

Carbon bookkeeping uses

\[
R_{eco}=R_{auto}+R_{hetero},\qquad NEE=R_{eco}-GPP,
\]
\[
\Delta C_{eco,t}+NEE_t+\sum_k F_{fire,k,t}\approx0.
\]

The first saved forward state is the budget baseline, not `restart.nc` from an
earlier spinup stage. Carbon stocks are not integrated as emissions. Fire
mortality, internal carbon transfer and direct efflux are distinct quantities.
Multiplication by 44/12 expresses emitted carbon as CO₂ mass under the
all-carbon-as-CO₂ assumption. It is neither gas speciation nor a CO₂e
climate-impact estimate.

Area summaries use the accepted run mask and unique statistical ownership.
Finite zeros do not establish that a pixel is land. Original 2019 vegetation
classes and mixed pyrome area fractions define scientific groups. Re-encoded
PFTs define a separate parameter diagnostic. Native model per-m² values are not
rescaled by vegetation fraction without an established area convention.

The 293 regions are an incomplete, compositionally imbalanced completion subset.
This pipeline reports their supported area and descriptive responses, **not a
design-unbiased Mediterranean total**. Completing all 400 regions still does not
remove the original sampling-frame coverage limitations.

## Reference support and interpretation

MOD17 GPP is compared over complete eight-day composites, including the actual
five- or six-day terminal interval. FLUXCOM-X GPP, NEE, ET and transpiration use
complete common months. CCI woody biomass is matched to model woody carbon on
original forest support; carbon fractions 0.47 and 0.51 are separate conversion
cases. MOD16 supplies a second ET estimate using verified unique source
granules. Product-specific availability and quality limitations remain explicit.

Formal statistics use reference-scale cells and matching time intervals, with
90% supported-area acceptance and an 80% sensitivity. Coarse reference values
mapped to model cells are a separate display product and never new independent
observations. Duplicate reference supports across cubes are resolved by coverage
then stable region ID, never by smaller error. Common-period multi-product GPP
panels use identical cells and complete years where available.

Reported metrics include bias, MAE, RMSE, Pearson r, r² and NSE. Undefined
constant-series statistics remain undefined. Product disagreement, carbon-factor
sensitivity and sampling uncertainty are separate. MODIS and FLUXCOM estimates
share some remote-sensing or training lineage with the model and are not
independent ground truth.

## Figures and discovery

The output atlas covers scenario scatter plots, reference density scatters,
violin/box distributions, geographic differences, seasonal cycles, annual
anomaly heatmaps and grouped effects. Additional panels separate direct fire
efflux from changes in GPP, respiration and carbon stocks, align responses to
first fire, distinguish repeated positive fire days, and relate responses to
initial stocks and climate. Each figure names its support, units and source
table. Zero components remain visible without fabricated smooth distributions.

Intervals resample spatial blocks and years (2,000 replicates; seed 42), rather
than treating neighboring pixel-days as independent. Environmental associations
are exploratory model-supported hypotheses; they are not proof of real-world
causation. Alternative explanations and unavailable checks remain in the report.
Exploratory centered-bootstrap tests require at least five spatial blocks,
three years and nonconstant bootstrap draws. Benjamini–Hochberg q-values cover
the predefined seven-response by overall/pyrome/vegetation family, retaining
non-significant results. Percentile intervals are not multiplicity-adjusted;
these approximate tests do not establish observational causation.

## Execution and acceptance

Three IDs selected before reading results provide the first complete trial.
The frozen regional workload then uses at most eight CPU jobs, each requesting
four cores, 32 GiB and 24 hours. Reference comparison and rendering are separate
dependent stages. No GPU or model-controller restart is required.

Acceptance separates file integrity, numerical/physical consistency, and
reference agreement. Failed checks retain their evidence and quarantine the
affected region from primary scientific summaries. A completion marker means a
stage finished, not that the model has been proven correct. Source tables and
standalone PDF/PNG figures accompany the offline English Markdown/PDF report.

See the [run instructions](../result_analysis/README.md),
[physical output dictionary](OUTPUT_SEMANTICS_EN.md),
[reference contracts](REFERENCES_EN.md), and [grouping rules](GROUPING_EN.md).
