#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "$script_dir/.." && pwd)"
archive="$project_dir/vendor/aim_zmap_reqnr_single.zip"
compatibility_patch="$project_dir/patches/0001-cmake-json-c-flags.patch"
generated_source_patch="$project_dir/patches/0002-gengetopt-relative-includes.patch"
ip_layer_patch="$project_dir/patches/0003-ipv6-iplayer-ethertype.patch"
work_dir="$project_dir/.build"
source_dir="$work_dir/aim_zmap_reqnr_single"
build_dir="$work_dir/zmap-build"
expected_sha256="c485e38576a0d59adeed7d3e9fcc607dfef95ecb3ca880b340d273099de9d44b"

for command_name in sha256sum unzip patch cmake; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "missing required command: $command_name" >&2
        exit 1
    }
done

[[ -f "$archive" ]] || {
    echo "missing vendored scanner archive: $archive" >&2
    exit 1
}

actual_sha256="$(sha256sum "$archive" | awk '{print $1}')"
[[ "$actual_sha256" == "$expected_sha256" ]] || {
    echo "scanner archive checksum mismatch" >&2
    exit 1
}

mkdir -p "$work_dir"
if [[ ! -d "$source_dir" ]]; then
    unzip -q "$archive" -d "$work_dir"
fi

# The old build appends pkg-config's list-valued JSON_CFLAGS to a string. With
# modern CMake this inserts shell command separators. JSON_INCLUDE_DIRS above it
# already carries the required include path, so remove only the redundant line.
if grep -Fq 'set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} ${JSON_CFLAGS}")' "$source_dir/CMakeLists.txt"; then
    patch --directory="$source_dir" --strip=1 < "$compatibility_patch"
elif ! grep -Fq 'include_directories(${JSON_INCLUDE_DIRS})' "$source_dir/CMakeLists.txt"; then
    echo "unexpected upstream CMakeLists.txt; refusing to apply compatibility patch" >&2
    exit 1
fi

# The ZIP also contains gengetopt output with the author's absolute build path
# embedded in five #include directives. Replace only those includes; the
# generated C and header contents otherwise remain untouched.
if grep -Rq '#include "/home/qwerty/' "$source_dir/src"; then
    patch --directory="$source_dir" --strip=1 < "$generated_source_patch"
elif [[ "$(grep -El '^#include "(zopt|topt|zbopt|zitopt|ztopt)\.h"$' \
        "$source_dir"/src/{zopt,topt,zbopt,zitopt,ztopt}.c | wc -l)" -ne 5 ]]; then
    echo "unexpected generated option sources; refusing to apply path patch" >&2
    exit 1
fi

# In IP-layer mode the upstream sender tags every packet as IPv4. On an
# IPv6-in-IPv4 SIT interface this produces IPIP (4), not IPv6 (41).
if grep -Fq 'sockaddr.sll_protocol = htons(ETHERTYPE_IP);' "$source_dir/src/send-linux.h"; then
    patch --directory="$source_dir" --strip=1 < "$ip_layer_patch"
elif ! grep -Fq 'ETHERTYPE_IPV6 : ETHERTYPE_IP' "$source_dir/src/send-linux.h"; then
    echo "unexpected upstream send-linux.h; refusing to apply IPv6 IP-layer patch" >&2
    exit 1
fi

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
