#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "usage: $0 <RIS bview URL> [output.gz]" >&2
    exit 2
fi

ris_url="$1"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "$script_dir/.." && pwd)"
output_file="${2:-$project_dir/data/raw/ris/$(basename -- "${ris_url%%\?*}")}"

for command_name in curl gzip sha256sum; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "missing required command: $command_name" >&2
        exit 1
    }
done

[[ "$ris_url" == https://data.ris.ripe.net/* ]] || {
    echo "expected an HTTPS RIPE RIS URL under data.ris.ripe.net" >&2
    exit 1
}

[[ "$output_file" == *.gz ]] || {
    echo "output path must end in .gz: $output_file" >&2
    exit 1
}

for reserved_path in "$output_file" "$output_file.url" "$output_file.sha256"; do
    [[ ! -e "$reserved_path" ]] || {
        echo "refusing to overwrite existing output: $reserved_path" >&2
        exit 1
    }
done

mkdir -p "$(dirname -- "$output_file")"
partial_file="$output_file.part"
trap 'rm -f -- "$partial_file"' EXIT

curl --fail --location --retry 3 --output "$partial_file" "$ris_url"
gzip --test "$partial_file"
mv -- "$partial_file" "$output_file"
trap - EXIT

printf '%s\n' "$ris_url" > "$output_file.url"
sha256sum "$output_file" > "$output_file.sha256"

printf 'downloaded: %s\n' "$output_file"
printf 'source URL: %s\n' "$output_file.url"
printf 'checksum:   %s\n' "$output_file.sha256"
