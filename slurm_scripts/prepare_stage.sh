#!/bin/bash
# Helpers sourced after env.sh by run.sh, build_image.sh and submit_as_jobs.sh. They
# check the stage before the one in SINDBAD_STAGE, build its output that this stage
# reads, and check the system image. See README_SLURM.md.

# Prints the domains in <output>/domains.txt that have no done marker, one per line.
# Usage: domains_not_done output
domains_not_done() {
    local output="$1" domain rest
    while read -r domain rest; do
        if [ -n "$domain" ] && [ ! -f "${output}/done/${domain}" ]; then
            echo "$domain"
        fi
    done < "${output}/domains.txt"
    return 0
}

# Returns 0 when every domain of the stage with its output in `output` is done, or
# when some are done and SINDBAD_ALLOW_PARTIAL=1. Otherwise it prints how to finish
# the stage and returns 1. Usage: require_stage_done stage output
require_stage_done() {
    local stage="$1" output="$2" missing n_missing n_all
    if [ ! -f "${output}/domains.txt" ]; then
        cat >&2 << EOF
oops... the ${stage} stage of ${SINDBAD_WISP_EXPERIMENT} was never run:
there is no ${output}/domains.txt. Run it first with
    SINDBAD_STAGE=${stage} ./slurm_scripts/build_image.sh
    SINDBAD_STAGE=${stage} ./slurm_scripts/run.sh
or run the stages from it on as dependent jobs with
    ./slurm_scripts/run_all.sh --from ${stage}
EOF
        return 1
    fi
    missing=$(domains_not_done "$output")
    [ -z "$missing" ] && return 0
    n_missing=$(grep -c . <<< "$missing")
    n_all=$(grep -c . "${output}/domains.txt")
    if [ "$SINDBAD_ALLOW_PARTIAL" = "1" ] && [ "$n_missing" -lt "$n_all" ]; then
        echo "warning: ${n_missing} of ${n_all} domains of the ${stage} stage are not done," \
            "going on with the others since SINDBAD_ALLOW_PARTIAL=1:" \
            "$(head -n 20 <<< "$missing" | paste -sd " " -)" >&2
        return 0
    fi
    cat >&2 << EOF
oops... the ${stage} stage of ${SINDBAD_WISP_EXPERIMENT} is not done:
${n_missing} of ${n_all} domains have no done marker in ${output}/done,
first: $(head -n 10 <<< "$missing" | paste -sd " " -).
Finish it with
    SINDBAD_STAGE=${stage} ./slurm_scripts/run.sh
or go on with the finished domains only with SINDBAD_ALLOW_PARTIAL=1 in env.sh.
EOF
    return 1
}

# Makes sure that `input`, the file this stage reads from the stage before, exists.
# When it is the file of this WISP experiment, `own`, and does not exist yet, it is
# combined from the output of the stage before once that stage is done. Any other
# file is used as it is. With prepare_dry_run=1 nothing is combined.
# Usage: prepare_input stage output name input own combine_script [more]
prepare_input() {
    local stage="$1" output="$2" name="$3" input="$4" own="$5"
    shift 5
    if [ "$input" != "$own" ]; then
        if [ -z "$input" ]; then
            echo "input of the ${SINDBAD_STAGE} stage: none, the default parameters"
            return 0
        fi
        # a relative path is taken from the folder of the experiment json
        local path_input="$input"
        [[ "$path_input" == /* ]] ||
            path_input="${SINDBAD_REPO}/setups/$(dirname "$SINDBAD_EXPERIMENT")/${input}"
        if [ ! -e "$path_input" ]; then
            echo "oops... the input of the ${SINDBAD_STAGE} stage does not exist: ${path_input}" >&2
            return 1
        fi
        echo "input of the ${SINDBAD_STAGE} stage: ${path_input}"
        return 0
    fi
    if [ -e "$own" ]; then
        echo "input of the ${SINDBAD_STAGE} stage: ${own}," \
            "delete it to combine the ${stage} stage again"
        return 0
    fi
    require_stage_done "$stage" "$output" || return 1
    if [ "${prepare_dry_run:-0}" = "1" ]; then
        echo "would combine the ${stage} stage into ${own} with $1"
        return 0
    fi
    echo "combining the ${stage} stage into ${own}"
    SINDBAD_STAGE="$stage" "${SINDBAD_REPO}/combine_outputs/run_combine.sh" "$1" \
        "$output" "$name" "${@:2}"
    if [ ! -e "$own" ]; then
        echo "oops... combining the ${stage} stage did not write ${own}" >&2
        return 1
    fi
}

# Builds the input of the stage in SINDBAD_STAGE from the stage before: the pools of
# the sites for the PFT stage and the PFT lookup map for the entity stage.
prepare_stage() {
    case "$SINDBAD_STAGE" in
        site) ;;
        pft)
            prepare_input site "$SINDBAD_SITE_OUTPUT" "$SINDBAD_SITE_NAME" \
                "$SINDBAD_SITE_RESTART" "$SINDBAD_SITE_RESTART_OWN" combine_restarts ;;
        entity)
            prepare_input pft "$SINDBAD_PFT_OUTPUT" "$SINDBAD_PFT_NAME" \
                "$SINDBAD_PFT_PARAMETERS" "$SINDBAD_PFT_PARAMETERS_OWN" pft_parameter_map \
                ${SINDBAD_DEFAULT_PFT:+"$SINDBAD_DEFAULT_PFT"} ;;
    esac
}

# modification time of a file in seconds, on Linux and macOS
mtime() {
    stat -c %Y "$1" 2> /dev/null || stat -f %m "$1"
}

# Prints missing, stale or ok for the system image of the stage. It is stale when it
# is older than Manifest.toml or the last commit of a package checkout in dev/.
image_state() {
    if [ ! -f "$SINDBAD_SYSIMAGE" ]; then
        echo missing
        return 0
    fi
    local t_image t_newest=0 t checkout
    t_image=$(mtime "$SINDBAD_SYSIMAGE")
    [ -f "${SINDBAD_REPO}/Manifest.toml" ] && t_newest=$(mtime "${SINDBAD_REPO}/Manifest.toml")
    for checkout in "${SINDBAD_REPO}"/dev/*/; do
        [ -d "${checkout}.git" ] || continue
        t=$(git -C "$checkout" log -1 --format=%ct 2> /dev/null || echo 0)
        if [ "$t" -gt "$t_newest" ]; then
            t_newest="$t"
        fi
    done
    if [ "$t_newest" -gt "$t_image" ]; then
        echo stale
    else
        echo ok
    fi
}
