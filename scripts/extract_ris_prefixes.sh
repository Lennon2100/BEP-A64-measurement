#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 <RIS RIB directory> <output.csv>" >&2
    exit 2
fi

rib_dir="$1"
output_file="$2"

for command_name in bgpdump gzip awk sort find; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "missing required command: $command_name" >&2
        exit 1
    }
done

[[ -d "$rib_dir" ]] || {
    echo "RIS RIB directory does not exist: $rib_dir" >&2
    exit 1
}

[[ ! -e "$output_file" ]] || {
    echo "refusing to overwrite existing prefix output: $output_file" >&2
    exit 1
}

mapfile -d '' rib_files < <(
    find "$rib_dir" -maxdepth 1 -type f -name 'rrc*-latest-bview.gz' -print0 | sort -z
)

[[ ${#rib_files[@]} -gt 0 ]] || {
    echo "no rrc*-latest-bview.gz files found in: $rib_dir" >&2
    exit 1
}

mkdir -p "$(dirname -- "$output_file")"
work_dir="$(mktemp -d "$(dirname -- "$output_file")/.extract-ris.XXXXXX")"
trap 'rm -rf -- "$work_dir"' EXIT

all_rows="$work_dir/all-ipv6-prefixes.csv"
: > "$all_rows"

for rib_file in "${rib_files[@]}"; do
    collector="$(basename -- "$rib_file")"
    collector="${collector%%-*}"
    mrt_file="$work_dir/${collector}.mrt"

    printf 'extracting %s\n' "$collector"
    gzip --decompress --stdout "$rib_file" > "$mrt_file"

    # bgpdump -m field 6 is the announced prefix and field 7 is the AS path.
    # A colon in the prefix selects IPv6 and excludes all IPv4 rows. The
    # rightmost AS-path token is retained as origin-AS metadata.
    bgpdump -m "$mrt_file" \
        | awk -F '|' '
            index($6, ":") > 0 {
                prefix = $6
                path = $7
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", path)
                count = split(path, parts, /[[:space:]]+/)
                origin = count > 0 ? parts[count] : ""
                print prefix "," origin
            }
        ' >> "$all_rows"

    rm -- "$mrt_file"
done

{
    printf '# prefix,origin_asn\n'
    LC_ALL=C sort -u "$all_rows"
} > "$output_file"

row_count="$(awk 'END {print NR - 1}' "$output_file")"
printf 'wrote %s unique IPv6 prefix/origin rows to %s\n' "$row_count" "$output_file"
