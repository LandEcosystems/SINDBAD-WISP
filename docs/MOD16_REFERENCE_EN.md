# MOD16A2GF C6.1: independent ET product comparison

The adapter compares **annual ET in 2015–2021**. It reads the local official
500 m MOD16A2GF C6.1 granules, builds a compact annual native-tile cache, and
aggregates it to a documented 0.05-degree comparison grid. This second ET
reference is separate from FLUXCOM-X. Neither product is ground truth.

Set `WISP_ANALYSIS_MOD16_ARCHIVE` to the read-only archive root. Shared caches
belong in the analysis output directory. The adapter extracts only the exact
required HDF member when an expanded file is unavailable, then deletes its own
extracted member; it never unpacks a global archive or modifies source files.

## Granule identity and year selection

Every date/tile must have exactly one processing version. If expanded files and
an archive both exist, their granule names must agree. Multiple versions cause
an explicit reference-source failure; the newest filename is never selected
implicitly.

The bounded Mediterranean inventory found multiple versions for ten tile
records on **2014-01-01**. Direct comparison of two versions confirmed different
ET and QC arrays. The checked Mediterranean tile/date records in **2015–2021**
were unique. This does not make all earlier years ambiguous. A 2021 pilot was
checked end to end before extending to the seven verified years. Every required
tile is still audited, and a missing or ambiguous year is reported separately.
`mod16_source_identity` rechecks the current granule identities before allowing
a completed comparison to resume, so a changed source invalidates that result.

## Physical conversion and QC

For each composite, the official encoded ET valid range is −32767 to 32700;
the scale factor is 0.1 and the offset is zero:

\[
ET_{8d}\,[mm]=0.1\,ET_{encoded}.
\]

The stored value is already the composite water total. It is not multiplied by
eight. All 46 composites must be valid at a native pixel before producing its
annual sum. The last composite ends on December 31 and has its actual calendar
length. Fill and non-terrestrial codes remain missing; valid negative values
are retained and remain visible to diagnostics.

The gap-filled product uses corrected LAI/FPAR inputs. Its inherited input QC
is therefore retained as diagnostic information rather than being used to
discard every numerically valid ET estimate. A separate strict-input-QC
fraction records good MODLAND quality, no dead-detector flag, main-algorithm
SCF flags 0 or 1, and clear or assumed-clear cloud states. Missing inherited QC
is recorded separately. Neither fraction is an ET uncertainty interval.

The annual cache stores annual ET, complete-year validity, strict-input-QC
fraction and missing-QC fraction. It also records all granules, source identity,
decoded ET/QC checksums, actual composite dates and cache checksum.

## Comparison support

The model's complete calendar-year ET daily sum is compared with the annual reference.
Paired records use the stable product identity `MOD16A2GF.061`; their period and
year fields distinguish years, allowing pooled multi-year statistics without
treating each annual file as an independent reference product. Source status
records remain year-specific.
The native equal-area sinusoidal reference is aggregated using GDAL average
resampling to explicit 0.05-degree cells. A reference cell needs at least 90%
complete native annual support. Model-to-reference area overlap then applies
the standard 90% model-support rule and its 80% sensitivity flag. The final
table labels the processed reference scale; it does not claim that aggregation
creates independent observations at the model's finer resolution.

Sources: [NASA MOD16 C6.1 file specification](https://ladsweb.modaps.eosdis.nasa.gov/filespec/MODIS/61/MOD16A2GF_c61)
and [MOD16 C6.1 User Guide](https://www.earthdata.nasa.gov/s3fs-public/2025-04/MOD16_User_Guide_V61.pdf).
