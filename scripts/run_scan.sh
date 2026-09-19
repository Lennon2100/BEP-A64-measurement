#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 8 ]]; then
    echo "usage: $0 ZMAP TARGETS OUTPUT SOURCE_IPV6 INTERFACE GATEWAY_MAC_OR_DASH RATE_PPS COOLDOWN_SECONDS" >&2
    exit 2
fi

zmap_binary="$1"
targets="$2"
output="$3"
source_ipv6="$4"
interface="$5"
gateway_mac="$6"
rate_pps="$7"
cooldown_seconds="$8"

[[ -x "$zmap_binary" ]] || { echo "ZMap is not executable: $zmap_binary" >&2; exit 1; }
[[ -s "$targets" ]] || { echo "target file is missing or empty: $targets" >&2; exit 1; }
[[ ! -e "$output" ]] || { echo "refusing to overwrite existing output: $output" >&2; exit 1; }
[[ "$rate_pps" =~ ^[1-9][0-9]*$ ]] || { echo "RATE_PPS must be a positive integer" >&2; exit 1; }
[[ "$cooldown_seconds" =~ ^[0-9]+$ ]] || { echo "COOLDOWN_SECONDS must be a non-negative integer" >&2; exit 1; }

output_dir="$(dirname -- "$output")"
mkdir -p "$output_dir"
command_log="${output}.command.txt"
version_log="${output}.scanner-version.txt"

fields="orig-dest-ip,classification,success,type,code,saddr,ttl,original_ttl,sent_timestamp_ts,sent_timestamp_us,nrsent,timestamp_str,timestamp_ts,timestamp_us"
command=(
    "$zmap_binary"
    -M icmp6_echoscan_time
    --ipv6-source-ip "$source_ipv6"
    --ipv6-target-file "$targets"
    --rate "$rate_pps"
    --cooldown-time "$cooldown_seconds"
    --interface "$interface"
    --output-module csv
    --output-fields "$fields"
    --output-filter "success = 0 || success = 1"
    --output-file "$output"
    --disable-syslog
)

if [[ "$gateway_mac" == "-" ]]; then
    command+=(--iplayer)
else
    command+=(--gateway-mac "$gateway_mac")
fi

printf '%q ' "${command[@]}" > "$command_log"
printf '\n' >> "$command_log"
"$zmap_binary" --version > "$version_log" 2>&1 || true
"${command[@]}"
