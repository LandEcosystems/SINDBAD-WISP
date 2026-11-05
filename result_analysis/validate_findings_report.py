"""Independent arithmetic and completeness audit of the interim findings.

This checks compact reporting tables, not every original daily NetCDF value.
The separately recorded raw endpoint audit covers the three highlighted cases.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .io import file_hash


def validate(root):
    root = Path(root)
    out = root / "report_findings_v2"
    manifest = json.loads((root / "report_parallel_v1/prepared.json").read_text())
    checks = []
    for item in manifest["components"]:
        folder = out / "components" / item["token"]
        state = json.loads((folder / "complete.json").read_text())
        assert state["snapshot"] == manifest["fingerprint"]
        values = pd.read_csv(folder / "region_values.csv")
        summary = pd.read_csv(folder / "area_weighted_summary.csv")
        assert not values.duplicated(["region_id", "group_type", "group"]).any()
        assert (values.n_years == 9).all()
        assert values.group_type.eq("overall").sum() == manifest["primary_regions"]
        for _, row in summary.iterrows():
            d = values[values.group_type.eq(row.group_type) & values.group.eq(row.group)]
            assert len(d) == row.n_regions
            for scenario in ["No_Fire", "VIIRS_Fire"]:
                independently_weighted = (d[scenario] * d.area_m2).sum() / d.area_m2.sum()
                np.testing.assert_allclose(independently_weighted, row[scenario], rtol=1e-10, atol=1e-8)
            np.testing.assert_allclose(row.VIIRS_Fire - row.No_Fire, row.difference, rtol=1e-8, atol=1e-8)
        for family in ["overall", "pyrome", "vegetation"]:
            for suffix in ["pdf", "png"]:
                assert (folder / f"absolute_{family}.{suffix}").stat().st_size > 1000
        checks.append({"component": item["token"], "groups": len(summary), "status": "pass"})

    metrics_checked = 0
    for data_name, metric_name, common in [
        ("annual_native_pairs.parquet", "annual_reference_metrics.csv", False),
        ("common_gpp_annual_pairs.parquet", "common_gpp_metrics.csv", True),
    ]:
        data = pd.read_parquet(out / "references" / data_name)
        metrics = pd.read_csv(out / "references" / metric_name)
        key = ["product", "variable", "scenario", "year", "reference_cell_id", "carbon_fraction"]
        assert not data.duplicated(key).any()
        assert data.year.between(2015, 2021).all()
        assert (data.coverage >= .9 - 1e-12).all()
        if not common:
            metrics = metrics[metrics.group_type.eq("overall")]
        else:
            common_key = ["scenario", "year", "lat_key", "lon_key"]
            assert (data.groupby(common_key).size() == 2).all()
        for _, row in metrics.iterrows():
            d = data[data["product"].eq(row["product"]) & data.scenario.eq(row.scenario)]
            if not common:
                d = d[d.variable.eq(row.variable) & d.carbon_fraction.astype(str).eq(str(row.carbon_fraction))]
            observed = d.reference.to_numpy(float)
            simulated = d.model.to_numpy(float)
            residual = simulated - observed
            assert len(d) == row.n
            for name, expected in {
                "bias": residual.mean(), "mae": np.abs(residual).mean(),
                "rmse": np.sqrt(np.dot(residual, residual) / len(residual)),
                "r": np.corrcoef(observed, simulated)[0, 1],
                "nse": 1 - np.dot(residual, residual) / np.sum((observed - observed.mean()) ** 2),
            }.items():
                np.testing.assert_allclose(row[name], expected, rtol=1e-9, atol=1e-9)
            metrics_checked += 1

    carbon = pd.read_csv(out / "carbon_response_decomposition.csv").set_index("component").mean_annual_gC_m2
    np.testing.assert_allclose(carbon.iloc[:3].sum(), carbon["Net atmospheric response"], atol=1e-12)
    primary = pd.read_csv(out / "primary_findings_by_group.csv")
    primary = primary[primary.group_type.eq("overall")].set_index("component")
    np.testing.assert_allclose(carbon["Reduced carbon uptake (−ΔGPP)"], -primary.loc["gpp", "difference"], atol=1e-7)
    np.testing.assert_allclose(carbon["Net atmospheric response"],
        primary.loc["nee", "difference"] + primary.loc["fire_carbon_emission", "VIIRS_Fire"], atol=1e-7)
    spatial = pd.read_csv(out / "spatial_evidence.csv")
    discovery = json.loads((out / "discovery_complete.json").read_text())
    np.testing.assert_allclose(spatial.fire_affected_area_m2.sum() / spatial.area_m2.sum(),
        discovery["fire_affected_fraction"], atol=1e-12)
    cases = json.loads((out / "raw_case_endpoint_verification.json").read_text())
    assert cases["status"] == "pass" and len(cases["checks"]) == 6
    assert all(x["max_abs_difference"] == 0 for x in cases["checks"])
    groups = json.loads((out / "groups_complete.json").read_text())
    assert groups["snapshot"] == manifest["fingerprint"] and len(groups["groups"]) == 11
    for g in groups["groups"]:
        assert Path(g["pdf"]).stat().st_size > 1000
    brief = json.loads((out / "brief_complete.json").read_text())
    assert brief["snapshot"] == manifest["fingerprint"] and brief["component_count"] == 58
    anomaly = json.loads((out / "reference_anomalies_complete.json").read_text())
    assert anomaly["snapshot"] == manifest["fingerprint"]
    assert anomaly["source_sha256"] == file_hash(out / "references/common_gpp_annual_pairs.parquet")
    state = dict(status="pass", snapshot=manifest["fingerprint"],
        checked_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(), component_checks=checks,
        reference_overall_metrics_independently_recomputed=metrics_checked,
        group_atlases=11, raw_endpoint_checks=6, brief_pages=brief["pages"],
        brief_sha256=file_hash(out / "SUPERVISOR_BRIEF_EN.pdf"),
        scope="Reporting-table arithmetic, common support, artifact completeness, carbon identity and raw-case endpoints. Not a revalidation of every daily simulation or proof of observational accuracy.")
    (out / "validation_findings_v2.json").write_text(json.dumps(state, indent=2) + "\n")
    return state


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    state = validate(p.parse_args().root)
    print(json.dumps({k: v for k, v in state.items() if k != "component_checks"}))
