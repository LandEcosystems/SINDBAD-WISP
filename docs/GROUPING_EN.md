# Spatial supports and scientific grouping

The analysis distinguishes three different areas. The **input window** is
128 × 128 model cells. The **runnable support** uses the accepted external run
mask, which excludes water and incomplete inputs even when model outputs are
finite zero. The **owned natural-vegetation support** is the subset assigned
uniquely to this sampled window in the frozen Batch1 design. Overlapping input
windows do not duplicate regional area.

`get_groups(region_id, qa_root)` reads `ownership.npz`, `run_masks.npz`, and
`static/static_inputs.npz`. It never changes these files or invokes the original
feature-card constructor, which writes cache files. Arrays have latitude-row,
longitude-column order; output NetCDF dimensions must be transposed by name.

## Classification and area calculation

The archived sparse crosswalk has one row per model cell and one column per
**local** original-2019 land-cover source cell. Its entries are equal-area
intersection areas in square metres. Each local column is mapped through
`source_global_flat_index` to the original global source array. It is incorrect
to treat sparse column indices directly as global raster indices.

For group `g`, the analysis weight of model pixel `p` is:

\[
A_{p,g}=I_{p,\mathrm{owned}} I_{p,\mathrm{run}}
       \sum_q A_{p,q} I_{q,g}.
\]

The scientific groups are:

| Group | Definition |
|---|---|
| Overall | Original 2019 IGBP classes 1–11 |
| Forest | IGBP 1–5 |
| Shrub | IGBP 6–7 |
| Savanna | IGBP 8–9 |
| Grass | IGBP 10 |
| Wetland | IGBP 11 |
| IGBP | Separate classes 1–11 |
| Pyrome | Unknown, FIL, ICS, RCS, RIL, FCS |
| Cross groups | Each pyrome × each broad vegetation type |
| Model PFT | The actual FLUXNET-compatible model parameter class; diagnostic only |

Formal pyrome labels come from the [Archibald et al. (2013)](https://doi.org/10.1073/pnas.1211466110) historical half-degree
classification. Original land-cover cells are assigned to their containing
native pyrome cell; the approximately 1 km source grid and half-degree grid
have aligned boundaries. Missing classifications remain unknown. A cube can
therefore contribute area to several pyromes and vegetation types. The
dominant pyrome used to allocate sampling strata is not substituted for these
pixel-area groups. The source uncertainty is a classification uncertainty,
not a confidence interval for simulated emissions.

The group-area sums are independently checked against the archived natural
area and against each other. Raw full-window native-model summaries use
`run_area_m2`; original study-area summaries use `group_area_m2`. The separate
`run_group_area_m2` retains the original classification intersections across
all runnable cells without the ownership filter. Thus reference comparisons
can use original forest area divided by full model-cell area, rather than
mistaking forest's share of owned natural area for forest's full-cell fraction.
These
denominators must not be mixed. All group results report their area and support.

## Partial completion and design weights

The original sample consists of 400 regions selected from 2,222 nonzero-area
owned windows. The initially completed 293 regions are a nonrandom completion
subset. Their current results are descriptive interim results. Applying the
original 400-region inverse-probability weights to the 293 completed regions
does not establish an unbiased estimate of the entire study area.

Only a complete, design-consistent sample can use the documented
Horvitz–Thompson sum over owned areas. Uncovered target areas, source-product
uncertainty and model-area denominators remain separate limitations even when
all 400 results exist. Finite model output values alone never identify land,
original vegetation class, statistical ownership or scientific validity.

## Output registry and inventory

`registry.py` documents 13 saved science variables and 50 components. Daily
carbon and water fluxes are integrated through time; carbon and water stocks
are summarized as states and changes, never accumulated as emissions. LAI is
explicitly unavailable. Fire efflux, mortality and internal transfer are
separate quantities and are not added together as atmospheric emissions.

An inventory snapshot freezes region IDs, expected files, source metadata,
saved code and configuration identities, and comparison-CSV row counts. The
large scientific-array fingerprint uses sizes and modification times; small
saved code/settings files and CSVs use SHA256. The inventory explicitly says
that it is not full numerical or physical acceptance. Existing snapshots with
different source identities cannot be overwritten; incremental analyses use
new snapshots.

`provenance.inspect_run` separately extracts the effective forcing scalars,
source variables, parameter-map path, selected processes and compression
settings from each run's `info.jld2`. It checks the saved structure against the
actual process list and saved process code. The PFT parameter map is read in
read-only mode and all parameter-array values and attributes are fingerprinted.
The mapped `fAPAR__k_extinction` values override the default scalar in the model
template. Metadata-read failures are explicit failures, never replaced by
assumed defaults. No Julia function, optimizer or model is executed.

`audit.audit_inventory` combines this evidence with SHA256 identities of the
ownership, run-mask and classification inputs, and metadata identities of each
large original input cube. `validate_dependencies` checks the frozen identities
again before and after region processing. A changed dependency stops reuse; it
does not silently mix versions. The optional `independent_spotcheck` directly
recomputes all 50 saved components' annual values for one predetermined owned
pixel and checks its carbon budget with a separate arithmetic implementation.
This spot check is explicitly narrower than full spatial acceptance.
