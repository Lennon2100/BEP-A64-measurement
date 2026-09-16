#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 <bview.gz> <output.csv>" >&2
    exit 2
fi

rib_file="$1"
output_file="$2"

for command_name in bgpdump gzip awk sort; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "missing required command: $command_name" >&2
        exit 1
    }
done

[[ -f "$rib_file" ]] || {
    echo "RIB file does not exist: $rib_file" >&2
    exit 1
}

gzip --test "$rib_file"

[[ ! -e "$output_file" ]] || {
    echo "refusing to overwrite existing prefix output: $output_file" >&2
    exit 1
}

mkdir -p "$(dirname -- "$output_file")"
work_dir="$(mktemp -d "$(dirname -- "$output_file")/.extract-ris.XXXXXX")"
trap 'rm -rf -- "$work_dir"' EXIT

mrt_file="$work_dir/bview.mrt"
prefix_file="$work_dir/ipv6-prefixes.csv"

gzip --decompress --stdout "$rib_file" > "$mrt_file"

# bgpdump -m uses | separated fields. Field 6 is the announced prefix and
# field 7 is the AS path. Preserve the rightmost AS-path token as origin
# metadata; routed-union normalization and the /64 cutoff belong to the later
# frame-preparation step.
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
    ' \
    | LC_ALL=C sort -u > "$prefix_file"

{
    printf '# prefix,origin_asn\n'
    cat "$prefix_file"
} > "$output_file"

printf 'extracted IPv6 prefix/origin rows: %s\n' "$(wc -l < "$prefix_file")"
printf 'output: %s\n' "$output_file"
