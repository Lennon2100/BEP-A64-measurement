#!/usr/bin/env bash
# Collapse a merged multi-collector RIS prefix/origin CSV into one row per
# unique IPv6 prefix, keeping every distinct origin-AS value as an ordered,
# "|"-joined list. Identical (prefix, origin) rows are duplicate observations,
# and a prefix announced by several origin ASes still
# contributes its address space exactly once. We never pick a single origin AS
# and never fabricate one; a blank origin stays blank.
#
# Input  (written by extract_ris_prefixes.sh):
#   # prefix,origin_asn
#   2001:db8::/32,64496
#   2001:db8::/32,64511
#   2001:db8:1::/48,64496
#
# Output:
#   # prefix,origin_count,origin_asns
#   2001:db8::/32,2,64496|64511
#   2001:db8:1::/48,1,64496
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "$script_dir/.." && pwd)"

input_file="${1:-$project_dir/data/interim/ris_ipv6_prefixes.csv}"
output_file="${2:-$project_dir/data/interim/ris_ipv6_prefixes_unique.csv}"

for command_name in awk sort; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "missing required command: $command_name" >&2
        exit 1
    }
done

[[ -f "$input_file" ]] || {
    echo "input file does not exist: $input_file" >&2
    exit 1
}

[[ ! -e "$output_file" ]] || {
    echo "refusing to overwrite existing output: $output_file" >&2
    exit 1
}

mkdir -p "$(dirname -- "$output_file")"

# Skip comment/header lines, strip any Windows CR, sort unique (prefix, origin)
# pairs so equal prefixes are adjacent, then fold each run into one row with a
# sorted, "|"-joined origin list.
{
    printf '# prefix,origin_count,origin_asns\n'
    awk -F',' '
        $1 !~ /^#/ {
            p = $1
            o = $2
            sub(/\r$/, "", p)
            sub(/\r$/, "", o)
            print p "," o
        }
    ' "$input_file" \
        | LC_ALL=C sort -u \
        | awk -F',' '
            {
                prefix = $1
                origin = $2
                if (prefix != cur) {
                    if (cur != "") { emit(cur, cnt, list) }
                    cur = prefix
                    cnt = 0
                    list = ""
                }
                list = (cnt == 0) ? origin : (list "|" origin)
                cnt++
            }
            function emit(p, n, l) {
                print p "," n "," l
            }
            END {
                if (cur != "") { emit(cur, cnt, list) }
            }
        '
} > "$output_file"

input_rows="$(awk '$1 !~ /^#/ { n++ } END { print n + 0 }' "$input_file")"
unique_prefixes="$(awk -F',' '$1 !~ /^#/ { n++ } END { print n + 0 }' "$output_file")"
moas_count="$(awk -F',' '$1 !~ /^#/ && $2 > 1 { n++ } END { print n + 0 }' "$output_file")"

printf 'wrote %s unique IPv6 prefixes (%s input prefix/origin rows; %s with multiple origins) to %s\n' \
    "$unique_prefixes" "$input_rows" "$moas_count" "$output_file"
