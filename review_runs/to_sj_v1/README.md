# One accepted Batch1 entity for Sujan's review

This additive entry implements the entity-stage delivery contract in the repository's latest `README_SINDBAD_WISP_OUTPUT.md` (sections 6–7) and `slurm_scripts/README_SLURM.md`. It changes no original script, model configuration, dependency, input cube, or other user's output. The parent revision is `6aa94907392eeb07aec08f6ef99fd166d3d1211f`.

## Scope

One newest successfully accepted Batch1 cube, fixed by its acceptance timestamp when `prepare.py` first runs. Full 128×128 grid and 2014-01-12 through 2023-12-31 (3,641 days). Run exactly two forward scenarios with the already optimized PFT parameters:

- **No_Fire**: `f_burnt_area = 0 × viirs_fire_binary`.
- **VIIRS_Fire**: `f_burnt_area = 0.99 × viirs_fire_binary`.

`site` means FLUXNET site optimization; `pft` means PFT parameter optimization. Neither is run here. WISP is not run. No watcher or continuation of the remaining 400 entities is installed.

## Environment and numerical path

Use the independently hash-verified environment and compiled image under:

`/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/sindbad_fire_nofire_batch1_v1/runtime/`

Julia explicitly receives `--project=<runtime>/environment` and `--sysimage=<runtime>/sindbad_entity.so`. This does not require a new personal `Manifest.toml` or local `dev/` checkout. Source dependencies referenced by the existing Manifest are checked against the saved runtime identity; any change stops admission. The original latest `setups/WISP` files and `plot_forward_WISP_DD.jl` are used directly from this checkout.

The model call sequence is the sequence inside `runExperimentForward`: `prepExperiment`, `runExperiment`, `saveOutCubes`, `saveRestartCubes`, `saveSpinupCubes`. The split allows assertions before running. The original plotting functions `plot_forward_run`, `summarize_forward_run` and `plot_runs_comparison` are called unchanged. Generic visualization is not configured for this entity setup, as documented upstream.

Differences from the generic original entry are explicit and audited:

1. Independent output root; one fixed accepted cube.
2. A small external year adapter preserves intentional 9999 values; excluded cells alone are NaN. The actual model cell set must equal the production acceptance mask, and loaded years must match each expected cell.
3. Model errors, plotting errors, and comparison errors fail the review instead of being swallowed.
4. `Random.seed!(42)` before each scenario; identical PFT parameters and initialization. Effective soil depth follows the unchanged model's `min(raw_depth, 50 m)` policy. Original scientific depths remain untouched.
5. A completion marker is written only after independent saved-file validation, not after Julia exits.

All effective overrides are under `audit/<entity>/`; the native `settings/info.jld2` also records them. Copied setup JSONs alone do not represent effective overrides.

## Execute one review

Run from the frozen checkout on Jena. The Python environment used below already includes NumPy, netCDF4 and Pillow. Place the checkout and all run files in the user-owned shared review root.

```bash
ROOT=/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/sindbad_fire_nofire_batch1_v1/outputs/to_SJ
REPO="$ROOT/code/SINDBAD-WISP"
PYTHON=/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/wisp_eos_q10_med10_v1/env/bin/python
"$PYTHON" "$REPO/review_runs/to_sj_v1/prepare.py" --root "$ROOT" --repo "$REPO"
sbatch --output="$ROOT/logs/review_%j.log" \
  "$REPO/review_runs/to_sj_v1/review.sbatch" "$REPO" "$ROOT"
```

The single job requests 8 CPU, 64 GiB, work partition, and a seven-day ceiling. Actual duration is measured; seven days is not a runtime prediction. No GPU or optimizer is requested. `run.sh` takes an exclusive root lock. It does not submit more jobs or restart the canceled old controller.

The fixed manifest binds the cube, QA masks, code, docs, runtime, parameters and adapters by SHA256. A changed identity is a failure. A completed model stage can be read back to retry figures; full model computation is not repeated just for missing figures. A complete review rerun revalidates artifacts. Data remain in this review root, not in the code repository.

## Required delivery

```text
to_SJ/
  README_FOR_SUJAN.md                  generated after successful validation
  review_manifest.json                frozen inputs and identities
  review_validation.json              complete artifact validation
  documentation/                      original README snapshots
  audit/<entity>/                     input/override checks, inventories, masks
  logs/
  entity/<entity>/
    output_WISP_<entity>-full_No_Fire/{data,figure,settings,code}
    output_WISP_<entity>-full_VIIRS_Fire/{data,figure,settings,code}
    comparison/
      summary_No_Fire.jls
      summary_VIIRS_Fire.jls
      *_mean_comparison_<entity>-full.png
      *_timeseries_comparison_<entity>-full.png
      comparison_metrics_<entity>-full.csv
  entity/done/                        written only after full validation
```

The validator reopens all 13 daily NetCDF files, verifies date/grid/layer/unit/compression contracts, finite values on run cells, excluded missing cells, and strict zero no-fire direct carbon emissions. It compares both restart arrays. It decodes every expected PNG (all variable/layer maps, time series, forcings and comparisons), checks all metric rows and columns, and checks unchanged input/code hashes. A constant field can legitimately have NaN correlation, per the original README; model outputs on run cells cannot.

VIIRS ×0.99 is a forcing proxy, not observed burned area. Output carbon is not CO₂ mass; the conversion is ×44/12. Native figure/CSV means are unweighted grid summaries; Mediterranean extrapolation needs ownership areas and inclusion weights and is outside this one-entity review.
