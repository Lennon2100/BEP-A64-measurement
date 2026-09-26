#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "$script_dir/.." && pwd)"
archive="$project_dir/vendor/aim_zmap_reqnr_single.zip"
work_dir="$project_dir/.build"
source_dir="$work_dir/aim_zmap_reqnr_single"
build_dir="$work_dir/zmap-build"

for command_name in unzip cmake python3; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "missing required command: $command_name" >&2
        exit 1
    }
done

[[ -f "$archive" ]] || {
    echo "missing vendored scanner archive: $archive" >&2
    exit 1
}

mkdir -p "$work_dir"
if [[ ! -d "$source_dir" ]]; then
    unzip -q "$archive" -d "$work_dir"
fi

# Apply the source edits required by current toolchains and IPv6 IP-layer use.
python3 - "$source_dir" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])


def replace_once(relative, old, new):
    path = root / relative
    text = path.read_text(encoding="utf-8")
    if new in text:
        return
    if old not in text:
        raise SystemExit(f"unexpected scanner source: {relative}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


cmake = root / "CMakeLists.txt"
cmake_text = cmake.read_text(encoding="utf-8")
old_flag = 'set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} ${JSON_CFLAGS}")\n'
if old_flag in cmake_text:
    cmake.write_text(cmake_text.replace(old_flag, "", 1), encoding="utf-8")
elif "include_directories(${JSON_INCLUDE_DIRS})" not in cmake_text:
    raise SystemExit("unexpected scanner source: CMakeLists.txt")

base = "/home/qwerty/Nextcloud/Arbeit/2023/IPv6_Neighbors/5_Linux/aim_zmap_reqnr_single/src"
for name in ("zopt", "topt", "zbopt", "zitopt", "ztopt"):
    replace_once(
        f"src/{name}.c",
        f'#include "{base}/{name}.h"',
        f'#include "{name}.h"',
    )

replace_once(
    "src/send-linux.h",
    "sockaddr.sll_protocol = htons(ETHERTYPE_IP);",
    "sockaddr.sll_protocol = htons(zconf.ipv6_source_ip ?\n"
    "                                     ETHERTYPE_IPV6 : ETHERTYPE_IP);",
)

module = "src/probe_modules/module_icmp6_echoscan_time.c"
replace_once(module, "4 * sizeof(uint32_t)) > len", "5 * sizeof(uint32_t)) > len")
replace_once(module, "4*sizeof(uint32_t) > len", "5*sizeof(uint32_t) > len")
replace_once(
    module,
    'fs_add_string(fs, "classification", (char*) "echoreply", 0);',
    'fs_add_uint64(fs, "nrsent", (uint64_t)icmp6_hdr->icmp6_data32[5]);\n'
    '\t\tfs_add_string(fs, "classification", (char*) "echoreply", 0);',
)
PY

cmake -S "$source_dir" -B "$build_dir" \
    -DENABLE_DEVELOPMENT=OFF \
    -DENABLE_LOG_TRACE=OFF
cmake --build "$build_dir" --parallel "${BUILD_JOBS:-4}"

zmap_binary="$build_dir/src/zmap"
[[ -x "$zmap_binary" ]] || {
    echo "build completed without expected binary: $zmap_binary" >&2
    exit 1
}

printf '%s\n' "$zmap_binary"
