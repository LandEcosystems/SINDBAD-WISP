# Additive, fail-closed entity entry. Original scripts and plotting functions are unchanged.
ENV["GKSwstype"] = "100"
using Sindbad
using Sindbad.Visualization
using Dates, Random, Serialization
const M = Sindbad.Setup.parsefile(ARGS[1])
const ROOT = M["root"]
const RID = M["region_id"]
const ENTITY = joinpath(ROOT, "entity", RID)
const AUDIT = joinpath(ROOT,"audit",RID)
const SCENARIO = ARGS[2]
include(joinpath(M["repo"],"plot_forward_WISP_DD.jl"))

function writejson(path,obj)
    mkpath(dirname(path)); tmp=path*".tmp"
    open(tmp,"w") do f; write(f,Sindbad.Setup.json(obj)); end
    mv(tmp,path;force=true)
end

function expected_cells()
    d=Dict{Tuple{Int,Int},Int}()
    for line in readlines(M["expected_cells"])[2:end]
        r,c,y=parse.(Int,split(line,',')); d[(r,c)]=y
    end
    d
end

function verify_input(info,forcing,expected)
    Tuple(Symbol.(forcing.helpers.dimensions.space))==(:lat,:lon) || error("Unexpected spatial axes")
    actual=Sindbad.Simulation.getSpatialInfo(forcing,info.helpers.run.filter_nan_pixels)
    Set(actual)==Set(keys(expected)) || error("Run mask differs from frozen acceptance")
    fdata=Sindbad.Simulation.namedtuple_from_names_values(forcing.data,forcing.variables)
    unknown=0
    for cell in actual
        v=Sindbad.Simulation.getLocData(fdata,cell).f_dist_year
        y=v isa Number ? v : only(v)
        y==expected[cell] || error("Disturbance year changed at $cell")
        unknown+=y==9999
    end
    writejson(joinpath(AUDIT,SCENARIO*"_input.json"),Dict("passed"=>true,"mask_exact_match"=>true,
        "run_cells"=>length(actual),"unknown_years_preserved_as_9999"=>unknown))
end

function verify_memory(output,vars,expected)
    for (vp,a) in zip(vars,output)
        ndims(a)==4 && size(a,1)==3641 || error("Output dimensions: $vp")
        for c in axes(a,4),r in axes(a,3)
            x=@view a[:,:,r,c]
            if haskey(expected,(r,c))
                all(isfinite,x) || error("Nonfinite output on run cell: $vp, $r, $c")
                SCENARIO=="No_Fire" && last(vp)==:c_fire_efflux && !all(iszero,x) && error("No-fire emissions")
            else
                any(isfinite,x) && error("Excluded cell ran: $r,$c")
            end
        end
    end
end

function plot_inventory(info,forcing,output,vars,tag)
    names=String[]; series=String[]
    for (vp,a) in zip(vars,output), l in 1:size(a,2)
        v=join(string.(vp),"_")*(size(a,2)==1 ? "" : "_$l")
        push!(series,v)
        push!(names,"$(v)_$(tag).png","$(v)_timeseries_$(tag).png")
    end
    for (i,v) in enumerate(forcing.variables)
        a=forcing.data[i]; d=forcing.dims[i]
        n=isempty(d)||d==(:time,) ? 1 : size(a,1)
        for l in 1:n
            suffix=n==1 ? "" : "_$l"
            push!(names,"forc_$(v)$(suffix)_$(tag).png")
        end
    end
    writejson(joinpath(AUDIT,SCENARIO*"_inventory.json"),Dict("figure_files"=>names,
        "series"=>series,"data_files"=>[string(last(v))*".nc" for v in vars],
        "figure_directory"=>info.output.dirs.figure,"data_directory"=>info.output.dirs.data))
end

function restore_output(info)
    # Saved NetCDF dimensions are canonicalized explicitly; no model rerun for figure retries.
    arrays=Array{Float32,4}[]
    for vp in info.output.variables
        ds=Sindbad.DataLoaders.YAXArrays.open_dataset(joinpath(info.output.dirs.data,string(last(vp))*".nc"))
        a=ds[Symbol(last(vp))]
        dims=collect(string.(Sindbad.DataLoaders.YAXArrayBase.dimnames(a)))
        wanted=vcat(["time"],filter(d->d ∉ ("time","lat","lon"),dims),["lat","lon"])
        x=permutedims(Array(a),[findfirst(==(d),dims) for d in wanted])
        ndims(x)==3 && (x=reshape(x,size(x,1),1,size(x,2),size(x,3)))
        push!(arrays,Float32.(x))
    end
    arrays
end

function run_case()
    mkpath(ENTITY); mkpath(AUDIT)
    domain=RID*"-full"; tag=domain*"-"*SCENARIO
    overrides=Dict{String,Any}(
        "experiment.basics.name"=>domain*"_"*SCENARIO,
        "experiment.model_output.path"=>ENTITY,
        "experiment.basics.time.date_begin"=>"2014-01-12T00:00:00",
        "experiment.basics.time.date_end"=>"2023-12-31T00:00:00",
        "experiment.basics.config_files.parameters"=>M["parameters"],
        "experiment.flags.catch_model_errors"=>false,
        "experiment.flags.run_lazy"=>false,
        "experiment.flags.filter_nan_pixels"=>true,
        "forcing.default_forcing.data_path"=>M["cube"],
        "forcing.variables.f_dist_year.data_path"=>M["adapter"],
        "forcing.variables.f_dist_year.source_variable"=>"disturbance_year_model",
        "forcing.variables.f_burnt_area.source_variable"=>"viirs_fire_binary",
        "forcing.variables.f_burnt_area.source_to_sindbad_unit"=>M["scenarios"][SCENARIO])
    writejson(joinpath(AUDIT,SCENARIO*"_overrides.json"),overrides)
    Random.seed!(42)
    Sindbad.Experiment.setExperimentMode!(overrides,:forward)
    info,forcing=prepExperiment(joinpath(M["repo"],"setups/WISP/experiment_WISP_DD.json");replace_info=overrides)
    expected=expected_cells(); verify_input(info,forcing,expected)
    println("INPUT_VALIDATION_OK $SCENARIO $(length(expected)) cells"); flush(stdout)
    marker=joinpath(AUDIT,SCENARIO*"_model_saved.json")
    if isfile(marker)
        arrays=restore_output(info)
        verify_memory(arrays,info.output.variables,expected)
    else
        started=time()
        out=runExperiment(info,forcing,info.helpers.run.run_forward)
        arrays=collect(values(out.output))
        verify_memory(arrays,info.output.variables,expected)
        dims=Sindbad.Experiment.getOutDims(info,forcing.helpers)
        saveOutCubes(info,arrays,dims,info.output.variables)
        saveRestartCubes(info,out.restart); saveSpinupCubes(info,out.spinup)
        writejson(marker,Dict("saved"=>true,"simulation_seconds"=>time()-started,"threads"=>Threads.nthreads()))
        out=nothing; GC.gc()
    end
    comparison=joinpath(ENTITY,"comparison"); mkpath(comparison)
    serialize(joinpath(comparison,"summary_$(SCENARIO).jls"),
        (dates=Date.(collect(info.helpers.dates.range)), summary=summarize_forward_run(arrays,info.output.variables)))
    plot_inventory(info,forcing,arrays,info.output.variables,tag)
    plot_forward_run(info,forcing,arrays,info.output.variables,tag) # Exceptions must fail this review job.
    writejson(joinpath(AUDIT,SCENARIO*"_plots_finished.json"),Dict("finished"=>true))
    println("SCENARIO_SAVED_AND_PLOTTED $SCENARIO"); flush(stdout)
end

function comparison()
    folder=joinpath(ENTITY,"comparison"); names=["No_Fire","VIIRS_Fire"]
    saved=[deserialize(joinpath(folder,"summary_$s.jls")) for s in names]
    saved[1].dates==saved[2].dates || error("Comparison dates differ")
    plot_runs_comparison([s.summary for s in saved],names,saved[1].dates,folder,RID*"-full")
end
function preflight()
    ds=Sindbad.DataLoaders.YAXArrays.open_dataset(joinpath(AUDIT,"adapter_test.nc"))
    x=Array(ds[:disturbance_year_model])
    count(==(9999),x)==1 && count(isnan,x)==1 || error("Julia adapter decoding failed")
    source=Sindbad.DataLoaders.YAXArrays.open_dataset(M["cube"])
    y=Array(source[:disturbance_year])
    n=sum(v->ismissing(v)||(v isa Number && isnan(v)),y)
    writejson(joinpath(AUDIT,"julia_preflight.json"),Dict("passed"=>true,"julia_version"=>string(VERSION),
       "adapter_unknown_9999_preserved"=>true,"adapter_excluded_nan_preserved"=>true,
       "native_year_missing_count_before_adapter"=>n,"actual_project"=>Base.active_project(),
       "sindbad_source"=>pathof(Sindbad)))
    println("JULIA_PREFLIGHT_OK")
end
if SCENARIO=="comparison"
    comparison()
elseif SCENARIO=="preflight"
    preflight()
else
    run_case()
end
