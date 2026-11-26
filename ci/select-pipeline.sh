#!/bin/sh
# Select the CI engine that runs this repository: github, woodpecker, or none.
#
# ci/pipelines/<engine>/ holds the canonical workflow files. Selecting an
# engine copies its files to the only directory that engine reads, removes
# the other engine's files, and records the choice in ci/pipeline-engine.
# An unselected engine therefore finds no configuration and runs nothing.
#
#   ./ci/select-pipeline.sh                  show the selected engine
#   ./ci/select-pipeline.sh select github    switch engines, then commit the result
#   ./ci/select-pipeline.sh check            verify the installed files (run by CI)
set -eu

cd "$(dirname "$0")/.."
MARKER=ci/pipeline-engine
SOURCES=ci/pipelines
ENGINES="github woodpecker"
# Woodpecker also reads a single root file when its directory is absent.
STRAY_FILES=".woodpecker.yaml .woodpecker.yml"

usage() {
    echo "usage: $0 [status | select github|woodpecker|none | check]" >&2
    exit 2
}

# Directory that each engine reads its workflows from.
active_dir() {
    case "$1" in
        github) echo .github/workflows ;;
        woodpecker) echo .woodpecker ;;
    esac
}

valid_engine() {
    case "$1" in
        github | woodpecker | none) return 0 ;;
        *) return 1 ;;
    esac
}

read_marker() {
    if [ ! -f "$MARKER" ]; then
        echo "Missing $MARKER. Run: $0 select github|woodpecker|none" >&2
        exit 1
    fi
    engine=$(tr -d ' \t\r\n' <"$MARKER")
    if ! valid_engine "$engine"; then
        echo "Invalid engine '$engine' in $MARKER" >&2
        exit 1
    fi
    printf '%s\n' "$engine"
}

select_engine() {
    valid_engine "$1" || usage
    for engine in $ENGINES; do
        dir=$(active_dir "$engine")
        # Remove only the files this script installs, then any emptied directory.
        for source in "$SOURCES/$engine"/*.yaml; do
            rm -f "$dir/$(basename "$source")"
        done
        if [ -d "$dir" ]; then
            rmdir -p "$dir" 2>/dev/null || true
        fi
    done
    if [ "$1" != none ]; then
        dir=$(active_dir "$1")
        mkdir -p "$dir"
        cp "$SOURCES/$1"/*.yaml "$dir/"
    fi
    printf '%s\n' "$1" >"$MARKER"
    echo "Selected $1. Review and commit ci/pipeline-engine and the workflow directories."
    check
}

# Fail when the installed workflows differ from the selection or its sources.
check() {
    selected=$(read_marker)
    failures=0
    for engine in $ENGINES; do
        dir=$(active_dir "$engine")
        if [ "$engine" = "$selected" ]; then
            for source in "$SOURCES/$engine"/*.yaml; do
                target="$dir/$(basename "$source")"
                if [ ! -f "$target" ]; then
                    echo "Missing $target for the selected $engine pipeline" >&2
                    failures=1
                elif ! cmp -s "$source" "$target"; then
                    echo "$target differs from $source. Edit the source and run: $0 select $engine" >&2
                    failures=1
                fi
            done
        fi
        for target in "$dir"/* "$dir"/.[!.]*; do
            [ -e "$target" ] || continue
            if [ "$engine" != "$selected" ]; then
                echo "Unexpected $target: $engine is not the selected pipeline ($selected)" >&2
                failures=1
            elif [ ! -f "$SOURCES/$engine/$(basename "$target")" ]; then
                echo "Unexpected $target: add workflows under $SOURCES/$engine instead" >&2
                failures=1
            fi
        done
    done
    for target in $STRAY_FILES; do
        if [ -e "$target" ]; then
            echo "Unexpected $target: Woodpecker would run it regardless of the selection" >&2
            failures=1
        fi
    done
    if [ "$failures" -ne 0 ]; then
        exit 1
    fi
    echo "Pipeline selection '$selected' matches the installed workflows."
}

case "${1:-status}" in
    status) read_marker ;;
    select)
        [ $# -eq 2 ] || usage
        select_engine "$2"
        ;;
    check) check ;;
    *) usage ;;
esac
