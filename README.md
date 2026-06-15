## Sindbad Development Workspace

This repository (`Sindbad-Dev-Template`) is a template repo for local development of the
Sindbad suite of packages. It keeps a shared Julia environment at the root while each
package lives in its own Git repository under `dev` directory.

### Layout

- `dev/Sindbad.jl/`
- `dev/Sindbad.jl/SindbadTEM/`
- `dev/ErrorMetrics.jl/`
- `dev/TimeSamplers.jl/`
- `dev/OmniTools.jl/`

### Git model

This workspace repo is intentionally **minimal**. The `.gitignore` is configured to track:

- `.gitignore`

and exclude:
- `dev/`
- `.dev`
- `*.dev`
- `dev.*`
- `tmp*`
- `**/Manifest.toml`

All package checkouts (`Sindbad.jl/`, `ErrorMetrics.jl/`, etc.), the
`Manifest.toml`, and any other files you create are **local-only** and never
committed here.

The recommended way to start developing is:

- **Copy** `LandEcosystems/Sindbad-Dev-Template` to your own namespace by clicking `Use this template` on the repo page and generating a new repository from it.
- **Clone the copy** locally and work in that repo.

You still contribute **directly to the package repositories** (`Sindbad.jl`,
`ErrorMetrics.jl`, `TimeSamplers.jl`, `OmniTools.jl`, `SindbadTEM`) by committing
inside those package directories and opening PRs on their respective repos.

### Quick start

From the workspace root of your fork:

```bash
julia setup_dev_packages.jl
```

This will:

- Activate the root environment
- Clone `Sindbad.jl`, `ErrorMetrics.jl`, `TimeSamplers.jl`, and `OmniTools.jl` into the `dev/` folder
- Add all of them in `dev` mode, plus `SindbadTEM` from `dev/Sindbad.jl/SindbadTEM`
- Instantiate the environment with all dependencies
- Run verification tests

You can verify the environment with:

```bash
julia -e 'using Pkg; Pkg.status()'
```

### Using your own forks

To point the workspace at your own GitHub forks while keeping the same folder
layout under `dev/`, use the `SindbadDevSettings.toml` file. In that, override the `git_url` entries, e.g.:

```toml
[Sindbad]
git_url = "https://github.com/your-user/Sindbad.jl.git"
```

Then re-run:

```bash
julia setup_dev_packages.jl
```

**Existing checkouts in the `dev/` folder are never overwritten; if a
directory already exists, it is used as-is and no clone is attempted.**

Each package remains a standalone Git repository in the `dev/` folder, so you can 
commit and push changes directly within the individual package directories.
