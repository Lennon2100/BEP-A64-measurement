#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd -- "$script_dir/.." && pwd)"
archive="$project_dir/vendor/aim_zmap_reqnr_single.zip"
work_dir="$project_dir/.build"
source_dir="$work_dir/aim_zmap_reqnr_single"
build_dir="$work_dir/zmap-build"
expected_sha256="c485e38576a0d59adeed7d3e9fcc607dfef95ecb3ca880b340d273099de9d44b"

for command_name in sha256sum unzip cmake; do
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

