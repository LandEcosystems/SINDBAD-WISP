
# ================================== using tools ==================================================
# some of the things that will be using... Julia tools, SINDBAD tools, local codes...
using Revise
using Sindbad
using Sindbad.Setup.Dates
using Sindbad.Visualization
using CMAEvolutionStrategy

include("./helpers.jl")
# using CMAEvolutionStrategy
toggle_type_abbrev_in_stacktrace()

# ================================== get data / set paths ========================================= 
path_output         = "./";

# ================================== selecting a site =============================================

# ================================== setting up the experiment ====================================
# experiment is all set up according to a (collection of) json file(s)
experiment_json     = joinpath(@__DIR__,"setups/WROASTED_HB/","experiment_insitu.json");
begin_year          = 1979;
end_year            = 2017;
isfile(experiment_json) ? nothing : println("Hmmm... does not exist : $(experiment_json)");

# setting up the model spinup sequence : can change according to the site...
# spinup_sequence = getSpinupSequenceSite(y_dist, begin_year);
run_lazy = false
experiment_name     = "WROASTED_spatial_FORWARD_lazy_$(run_lazy)";
# default setting in experiment_json will be replaced by the "replace_info"
replace_info = Dict("experiment.basics.time.date_begin" => "$(begin_year)-01-01",
    "experiment.basics.name" => experiment_name,
    "experiment.basics.time.date_end" => "$(end_year)-12-31",
    "experiment.flags.run_lazy" => run_lazy,
    # "experiment.model_spinup.sequence" => spinup_sequence,
    "experiment.model_output.path" => path_output,
    );

# ================================== forward run ================================================== 
# before running the optimization, check a forward run 
@time out_dflt  = runExperimentForward(experiment_json; replace_info=deepcopy(replace_info)); # full default model

# access some of the internals to do some plots with the forward runs...
info            = getExperimentInfo(experiment_json; replace_info=deepcopy(replace_info)); # note that this will modify information from json with the replace_info
forcing         = getForcing(info); 
run_helpers     = prepTEM(forcing, info); # not needed now
# @time outdataset = runTEMYax(info.models.forward, forcing, info)

