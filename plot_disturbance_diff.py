import xarray as xr
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns

sns.set_theme(style="white", context="notebook")

DIST_PATH = "/Users/skoirala/research/RnD/SINDBAD-WISP/output_WISP_WISP_AU-WISP-daily-disturbance-true_FORWARD_lazy_false/data/WISP_AU-WISP-daily-disturbance-true_FORWARD_lazy_false_WISP_cEco.nc"
NODIST_PATH = "/Users/skoirala/research/RnD/SINDBAD-WISP/output_WISP_WISP_AU-WISP-daily-disturbance-false_FORWARD_lazy_false/data/WISP_AU-WISP-daily-disturbance-false_FORWARD_lazy_false_WISP_cEco.nc"
OUT_PATH = "/Users/skoirala/research/RnD/SINDBAD-WISP/output_WISP_WISP_AU-WISP-daily-disturbance-true_FORWARD_lazy_false/figure/AU-WISP_pools_cEco_disturbance_vs_no_disturbance.png"

ds_dist = xr.open_dataset(DIST_PATH)
ds_nodist = xr.open_dataset(NODIST_PATH)

var = "cEco"
n_layers = ds_dist.sizes["d_cEco"]
pool_names = ["cVegRoot", "cVegWood", "cVegLeaf", "cVegReserve", "cLitFast", "cLitSlow", "cSoilSlow", "cSoilOld"]

# time-mean map per layer, oriented (lat, lon) for imshow
dist_maps = [ds_dist[var].isel(d_cEco=i).mean(dim="time").transpose("lat", "lon").values for i in range(n_layers)]
nodist_maps = [ds_nodist[var].isel(d_cEco=i).mean(dim="time").transpose("lat", "lon").values for i in range(n_layers)]
diff_maps = [d - n for d, n in zip(dist_maps, nodist_maps)]

lat = ds_dist.lat.values
lon = ds_dist.lon.values
extent = [lon.min(), lon.max(), lat.min(), lat.max()]

fig, axes = plt.subplots(n_layers, 3, figsize=(13, 3.1 * n_layers))

col_titles = ["with disturbance", "without disturbance", "difference (with - without)"]

for i in range(n_layers):
    row_vmin = min(dist_maps[i].min(), nodist_maps[i].min())
    row_vmax = max(dist_maps[i].max(), nodist_maps[i].max())

    diff_absmax = np.abs(diff_maps[i]).max()
    diff_absmax = diff_absmax if diff_absmax > 0 else 1.0

    ax = axes[i, 0]
    im0 = ax.imshow(dist_maps[i], origin="lower", extent=extent, aspect="auto",
                     cmap="magma", vmin=row_vmin, vmax=row_vmax)
    ax.set_ylabel(f"{pool_names[i]}\nlat", fontsize=10)
    if i == 0:
        ax.set_title(col_titles[0])
    cb0 = fig.colorbar(im0, ax=ax, fraction=0.046, pad=0.04)

    ax = axes[i, 1]
    im1 = ax.imshow(nodist_maps[i], origin="lower", extent=extent, aspect="auto",
                     cmap="magma", vmin=row_vmin, vmax=row_vmax)
    if i == 0:
        ax.set_title(col_titles[1])
    cb1 = fig.colorbar(im1, ax=ax, fraction=0.046, pad=0.04)

    ax = axes[i, 2]
    im2 = ax.imshow(diff_maps[i], origin="lower", extent=extent, aspect="auto",
                     cmap="RdBu_r", vmin=-diff_absmax, vmax=diff_absmax)
    if i == 0:
        ax.set_title(col_titles[2])
    cb2 = fig.colorbar(im2, ax=ax, fraction=0.046, pad=0.04)

    for ax in axes[i, :]:
        ax.set_xlabel("lon" if i == n_layers - 1 else "")
        ax.tick_params(labelsize=8)

fig.suptitle("WISP cEco pools: time-mean maps, with vs. without disturbance", fontsize=14, y=1.0)
fig.tight_layout()
fig.savefig(OUT_PATH, dpi=150, bbox_inches="tight")
print("saved:", OUT_PATH)
