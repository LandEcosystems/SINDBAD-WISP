#!/usr/bin/env julia
"""
Setup script to clone and add all Sindbad ecosystem packages in dev mode.

This script will:
1. Activate the current directory as the Julia environment
2. Clone each package repository from GitHub into the standard workspace folders
3. Add them as dev dependencies

Usage:
    julia setup_dev_packages.jl

Optional per-user configuration:
    Create a `SindbadDevSettings.toml` in this directory to override the
    *git URLs only* (paths remain fixed to the standard layout).
"""

using Pkg
import TOML

struct PackageConfig
    name::String
    default_path::String
    default_git_url::Union{String,Nothing}
end

const DEFAULT_PACKAGES = [
    # Develop leaf packages first (no internal dependencies)
    PackageConfig("ErrorMetrics", "dev/ErrorMetrics.jl",    "https://github.com/LandEcosystems/ErrorMetrics.jl.git"),
    PackageConfig("TimeSamplers", "dev/TimeSamplers.jl",    "https://github.com/LandEcosystems/TimeSamplers.jl.git"),
    PackageConfig("OmniTools",    "dev/OmniTools.jl",      "https://github.com/LandEcosystems/OmniTools.jl.git"),
    # Develop Sindbad and SindbadTEM after their dependencies exist
    PackageConfig("Sindbad",      "dev/Sindbad.jl",         "https://github.com/LandEcosystems/Sindbad.jl.git"),
    # SindbadTEM is shipped inside Sindbad.jl by default; no git URL needed
    PackageConfig("SindbadTEM",   "dev/Sindbad.jl/SindbadTEM",  nothing),
]

const SETTINGS_FILE = "SindbadDevSettings.toml"

function load_settings()
    if isfile(SETTINGS_FILE)
        println("Loading settings from $(abspath(SETTINGS_FILE))")
        return TOML.parsefile(SETTINGS_FILE)
    else
        println("No settings file found ($SETTINGS_FILE); using defaults.")
        return Dict{String,Any}()
    end
end

function get_git_url(settings::Dict{String,Any}, pkg::PackageConfig)
    pkg_settings = get(settings, pkg.name, Dict{String,Any}())
    git_url = get(pkg_settings, "git_url", pkg.default_git_url)
    return git_url === nothing ? nothing : String(git_url)
end

function test_dev_setup()
    println("\n" * "=" ^ 60)
    println("TESTING DEV MODE SETUP")
    println("=" ^ 60)

    println("\n✓ Checking package status...")
    
    all_ok = true
    
    # Check manifest for dev entries
    manifest_path = joinpath("Manifest.toml")
    if isfile(manifest_path)
        manifest_content = read(manifest_path, String)
        
        for pkg in DEFAULT_PACKAGES
            if contains(manifest_content, "path = ") && contains(manifest_content, pkg.name)
                println("  ✅ $(pkg.name): in dev mode")
            else
                println("  ⚠️  $(pkg.name): not found as dev package")
                all_ok = false
            end
        end
    else
        println("  ⚠️  Manifest.toml not found")
        all_ok = false
    end

    println("\n✓ Checking directories exist...")
    for pkg in DEFAULT_PACKAGES
        if isdir(pkg.default_path)
            println("  ✅ $(pkg.default_path) exists")
        else
            println("  ⚠️  $(pkg.default_path) not found")
            all_ok = false
        end
    end

    println("\n✓ Package paths...")
    for pkg in DEFAULT_PACKAGES
        try
            mod = eval(:(using $(Symbol(pkg.name)); $(Symbol(pkg.name))))
            path = pathof(mod)
            if contains(path, pkg.default_path) || (pkg.name != "SindbadTEM" && contains(path, pwd()))
                println("  ✅ $(pkg.name): $path")
            else
                println("  ⚠️  $(pkg.name): $path (unexpected location)")
                all_ok = false
            end
        catch e
            println("  ℹ️  $(pkg.name): not loaded (might need restart)")
        end
    end

    println("\n" * "=" ^ 60)
    if all_ok
        println("✨ DEV SETUP VERIFICATION PASSED")
    else
        println("⚠️  DEV SETUP: Some issues detected (see above)")
    end
    println("=" ^ 60)
end

function main()
    println("Activating environment at: $(pwd())")
    Pkg.activate(".")

    println("\nCloning and adding packages in dev mode...")
    println("=" ^ 60)

    settings = load_settings()

    # First: Clone all packages except SindbadTEM (so all paths exist)
    println("\n" * "=" ^ 60)
    println("STEP 1: Cloning packages...")
    println("=" ^ 60)

    for pkg in DEFAULT_PACKAGES
        pkg.name == "SindbadTEM" && continue

        path = pkg.default_path
        git_url = get_git_url(settings, pkg)

        println("\n📦 $(pkg.name)...")

        if isdir(path)
            println("   ⚠️  Already cloned at `$path`")
            # Check for unstaged changes and prompt for update
            cd(path) do
                try
                    status = read(`git status --porcelain`, String)
                    if !isempty(status)
                        println("   ❌ Unstaged changes detected in $path. Please commit or stash them before updating.")
                        return
                    end
                catch e
                    println("   ❌ Error checking git status: $e")
                    return
                end
                # Prompt user to update
                print("   ❓ Do you want to update (git pull) $path? [y/N]: ")
                answer = readline()
                if lowercase(strip(answer)) in ["y", "yes"]
                    try
                        run(`git pull`)
                        println("   ✅ Updated $path from remote.")
                    catch e
                        println("   ❌ Error pulling updates: $e")
                    end
                else
                    println("   ℹ️  Skipping update for $path.")
                end
            end
        elseif git_url !== nothing
            println("   🔄 Cloning from $git_url...")
            try
                run(`git clone $git_url $path`)
                println("   ✅ Successfully cloned")
            catch e
                println("   ❌ Error cloning: $e")
            end
        else
            println("   ⚠️  No git URL configured, skipping clone")
        end
    end

    # Handle SindbadTEM cloning (nested in Sindbad.jl by default)
    let
        sindbadtem_pkg = first(filter(p -> p.name == "SindbadTEM", DEFAULT_PACKAGES))
        path = sindbadtem_pkg.default_path
        git_url = get_git_url(settings, sindbadtem_pkg)

        println("\n📦 SindbadTEM...")
        
        if isdir(path)
            println("   ⚠️  Already exists at `$path`")
        elseif git_url !== nothing
            parent_dir = dirname(path)
            if !isempty(parent_dir) && !isdir(parent_dir)
                mkpath(parent_dir)
            end
            println("   🔄 Cloning from $git_url...")
            try
                run(`git clone $git_url $path`)
                println("   ✅ Successfully cloned")
            catch e
                println("   ❌ Error cloning: $e")
            end
        else
            println("   ℹ️  Assuming shipped inside Sindbad.jl at `$path`")
        end
    end

    # Second: Develop all packages (now that all paths exist)
    println("\n" * "=" ^ 60)
    println("STEP 2: Developing packages...")
    println("=" ^ 60)

    for pkg in DEFAULT_PACKAGES
        path = pkg.default_path
        if !isdir(path)
            println("\n⚠️  $(pkg.name): path does not exist at `$path`, skipping")
            continue
        end

        println("\n📦 $(pkg.name): developing from `$path`...")
        try
            Pkg.develop(path=path)
            println("   ✅ Successfully developed")
        catch e
            println("   ❌ Error: $e")
        end
    end

    # Ensure SindbadTEM is always developed from local folder if it exists
    sindbadtem_path = "dev/Sindbad.jl/SindbadTEM"
    sindbad_path = "dev/Sindbad.jl"
    if isdir(sindbadtem_path)
        println("\n📦 SindbadTEM: ensuring dev mode from `$sindbadtem_path` in main environment...")
        try
            Pkg.develop(path=sindbadtem_path)
            println("   ✅ SindbadTEM developed from local folder in main environment")
        catch e
            println("   ❌ Error developing SindbadTEM in main environment: $e")
        end

        # Also activate Sindbad.jl's environment and develop SindbadTEM there
        if isfile(joinpath(sindbad_path, "Project.toml"))
            println("\n📦 Activating Sindbad.jl environment to ensure local SindbadTEM dependency...")
            try
                Pkg.activate(sindbad_path)
                Pkg.develop(path=abspath(sindbadtem_path))
                Pkg.instantiate()
                println("   ✅ SindbadTEM developed from local folder in Sindbad.jl environment")
            catch e
                println("   ❌ Error developing SindbadTEM in Sindbad.jl environment: $e")
            end
            # Reactivate main environment
            Pkg.activate(".")
        else
            println("   ⚠️  Sindbad.jl Project.toml not found, skipping dependency update in Sindbad.jl environment.")
        end
    else
        println("\n⚠️  SindbadTEM path not found at `$sindbadtem_path`, skipping dev mode for SindbadTEM")
    end

    # Third: Instantiate the environment
    println("\n" * "=" ^ 60)
    println("Instantiating workspace environment...")
    println("=" ^ 60)
    try
        Pkg.instantiate()
        println("✅ Environment instantiated successfully")
    catch e
        println("❌ Error instantiating environment: $e")
    end

    # Fourth: Run tests
    test_dev_setup()
end

main()
