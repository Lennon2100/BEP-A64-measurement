# BEP journal measurement runtime

This directory contains the Linux-side measurement runtime derived from the
`sbaresearch/icmpv6-destination-reachable` artifact. Development and review can
happen on Windows, but the bundled ZMap source and the scan wrapper are intended
to run on Linux.

The current boundary is intentionally small:

```text
strategy implementation -> ordered target file -> run_scan.sh -> raw ZMap CSV
```

Each search or baseline strategy remains a separate target producer. The scanner
does not know whether a target came from BEP, PaS, SubRecon, random sampling, or
the journal policy. This file boundary is the strategy plug-in point; no generic
plug-in framework is built before two real strategies require shared code.

## Included now

- `UPSTREAM_AUDIT.md`: source-level reuse audit and known limitations.
- `vendor/aim_zmap_reqnr_single.zip`: the exact single-instance ZMap archive
  published inside the paper artifact.
- `scripts/build_zmap.sh`: verifies and builds that archive on Linux.
- `scripts/download_ris.sh`: downloads `latest-bview.gz` from every active RIPE
  RIS collector.
- `scripts/extract_ris_prefixes.sh`: calls `bgpdump` for every downloaded RIB,
  drops IPv4, and merges deduplicated IPv6 `prefix,origin_asn` rows.
- `scripts/run_scan.sh`: a strict, non-interactive wrapper around
  `icmp6_echoscan_time`.
- `strategies/README.md`: the minimal target-producer contract.

Benchmark sampling, reference-label aggregation, and policy evaluation are not
implemented in this first slice. They consume saved raw measurement data later.

## Linux build

On Debian/Ubuntu, install the upstream build dependencies:

```bash
sudo apt-get install build-essential cmake libgmp-dev gengetopt \
  libpcap-dev flex byacc libjson-c-dev pkg-config libunistring-dev unzip patch
chmod +x scripts/*.sh
./scripts/build_zmap.sh
```

The script prints the resulting `zmap` path. It does not install system-wide.
It also applies the single compatibility patch under `patches/` to the extracted
build tree; the vendored ZIP remains byte-for-byte unchanged.

## Scan wrapper

```bash
sudo ./scripts/run_scan.sh \
  .build/zmap-build/src/zmap \
  targets.txt \
  runs/example/raw_zmap.csv \
  2001:db8::1 \
  eth0 \
  00:11:22:33:44:55 \
  1000 \
  10
```

Arguments are, in order: ZMap binary, ordered target file, output CSV, source
IPv6 address, interface, gateway MAC, packets per second, and cooldown seconds.
Use real authorized values on the Linux measurement host. The wrapper refuses to
overwrite an existing raw result.

The Windows checkout cannot compile or execute this packet engine. Final build
and runtime verification therefore happens after this directory is copied to the
Linux measurement server.

## RIPE RIS input

Install the small extraction toolchain on Debian/Ubuntu:

```bash
sudo apt-get install -y curl gzip bgpdump
chmod +x scripts/*.sh
```

Download the latest RIB from every currently active RIPE RIS collector:

```bash
./scripts/download_ris.sh data/raw/ris/latest
```

Decompress each RIB, run `bgpdump`, discard IPv4 rows, then merge and deduplicate
IPv6 prefix/origin pairs:

```bash
./scripts/extract_ris_prefixes.sh \
  data/raw/ris/latest \
  data/interim/ris_ipv6_prefixes.csv
```

The scripts intentionally match the original BValue collection style: they use
the moving `latest-bview.gz` files from all collectors and do not create URL or
checksum sidecars. The extractor does not yet compute the routed union, discard
prefixes longer than `/64`, create PSUs, or sample A64s. Those remain
responsibilities of the later `prepare_pilot.py`.
