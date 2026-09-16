#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 1 ]]; then
    echo "usage: $0 [output-directory]" >&2
    exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "$script_dir/.." && pwd)"
output_dir="${1:-$project_dir/data/raw/ris/latest}"

for command_name in curl find; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "missing required command: $command_name" >&2
        exit 1
    }
done

if [[ -d "$output_dir" && -n "$(find "$output_dir" -mindepth 1 -print -quit)" ]]; then
    echo "refusing to mix a new download with existing files: $output_dir" >&2
    exit 1
fi

mkdir -p "$output_dir"

# Active collectors on the RIPE RIS collector page when this script was added.
# RRC02, RRC08, and RRC09 are historical; RRC17 is not assigned.
collectors=(
    rrc00 rrc01 rrc03 rrc04 rrc05 rrc06 rrc07
    rrc10 rrc11 rrc12 rrc13 rrc14 rrc15 rrc16
    rrc18 rrc19 rrc20 rrc21 rrc22 rrc23 rrc24 rrc25 rrc26
)

partial_file=""
trap '[[ -z "$partial_file" ]] || rm -f -- "$partial_file"' EXIT

for collector in "${collectors[@]}"; do
    output_file="$output_dir/${collector}-latest-bview.gz"
    partial_file="$output_file.part"
    url="https://data.ris.ripe.net/${collector}/latest-bview.gz"

    [[ ! -e "$output_file" && ! -e "$partial_file" ]] || {
        echo "refusing to overwrite existing collector data: $output_file" >&2
        exit 1
    }

    printf 'downloading %s\n' "$collector"
    curl --fail --location --retry 3 --output "$partial_file" "$url"
    mv -- "$partial_file" "$output_file"
    partial_file=""
done

trap - EXIT

printf 'downloaded %s RIS collector RIBs to %s\n' "${#collectors[@]}" "$output_dir"
