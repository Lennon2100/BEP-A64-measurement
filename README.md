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
- `scripts/dedup_ris_prefixes.sh`: folds the multi-collector output to one row
  per IPv6 prefix while retaining all reported origins as metadata.
- `scripts/prepare_campaign.py`: validates the three-column unique-prefix file,
  builds its immediate-parent BGP tree, and derives the nonoverlapping top-level
  roots plus response-blind root features.
- `scripts/run_scan.sh`: a strict, non-interactive wrapper around
  `icmp6_echoscan_time`.
- `strategies/README.md`: the minimal target-producer contract.

Direct C64 sampling, IID target generation, reference-label aggregation, and
policy evaluation are not implemented in this first slice. They consume the
tree/split output and saved raw measurement data later.

## Linux build

On Debian/Ubuntu, install the upstream build dependencies:

```bash
sudo apt-get install build-essential cmake libgmp-dev gengetopt \
  libpcap-dev flex byacc libjson-c-dev pkg-config libunistring-dev unzip patch
chmod +x scripts/*.sh
./scripts/build_zmap.sh
```

The script prints the resulting `zmap` path. It does not install system-wide.
It applies the build, IPv6 SIT, and Echo CSV field-alignment patches under
`patches/` to the extracted build tree; the vendored ZIP remains unchanged.

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

For a point-to-point IPv6 tunnel such as `ipv6net` (SIT/NOARP), pass `-` instead
of a gateway MAC. The wrapper then uses ZMap's `--iplayer` mode. The build script
applies the IPv6 protocol-tag fix to the extracted source; the upstream ZIP is
unchanged. An Ethernet interface still uses its real gateway MAC as before.

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

The multi-collector prefix union is the selected campaign input. The scripts
download the moving `latest-bview.gz` files; record each actual input identity
and snapshot time for a campaign. The deduplicated prefix file still contains
overlapping announcements and may contain prefixes longer than `/64`; it is not
a target file.

After curating known bad input rows, build the BGP prior tree and top-level-root
summary before defining response-blind root strata:

```bash
python3 scripts/prepare_campaign.py \
  data/interim/ris_ipv6_prefixes_unique.csv \
  runs/frame-preparation
```

The command refuses to overwrite its three outputs:

- `bgp_tree.csv` retains every BGP prefix, origin set, immediate parent, root,
  bit distance, true BGP tree depth, child count, and descendant count;
- `frame_roots.csv` lists every intact nonoverlapping top-level root, its origin
  metadata, tree features, and routed C64 count;
- `summary.json` records structural counts, excluded rows, total routed C64
  mass, root-length counts/C64 mass, and the root maximum-tree-depth histogram.

The preparation stage does not subdivide a short root or assign tranches. After
inspecting the full root-feature distribution, define a small set of
response-blind root strata and assign whole roots within strata to calibration
or held-out using a recorded seed. This preserves the top-level search start
and prevents response state from crossing the evaluation split. Direct C64
sampling and IID targets are added after those strata and budgets are fixed.

For the current frame, the recorded root-depth strata are `d0`, `d1`, `d2`,
and `d3plus`. Re-run into a new output directory to assign exactly one fifth of
the roots in each stratum to calibration by deterministic hash rank:

```bash
python3 scripts/prepare_campaign.py \
  data/interim/ris_ipv6_prefixes_unique.csv \
  runs/frame-root-split-v1 \
  --split-seed 'bep-journal-root-split-v1-20260919' \
  --calibration-root-fraction 1/5
```

Every prefix under one root receives the same tranche. The summary reports root
counts and C64 combinatorial mass for both sides and for each depth stratum.
C64 mass is diagnostic only and does not control the split or probe budget.
