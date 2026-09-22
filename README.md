# BEP journal measurement runtime

This directory contains the Linux-side measurement runtime derived from the
`sbaresearch/icmpv6-destination-reachable` artifact. Development and review can
happen on Windows, but the bundled ZMap source and the scan wrapper are intended
to run on Linux.

The current boundary is intentionally small:

```text
strategy implementation -> ordered target file -> run_scan.sh -> raw ZMap CSV
```

Each search strategy or adapted comparator remains a separate target producer.
The scanner need not know whether a target came from the journal policy,
paper-based TNet reproduction, or BGP-only ICNP SubRecon strategy adaptation.
The three methods will share the same BGP-prefix input, not activity-bearing
target lists; offline prefix-tree construction spends no scan probes. The
SubRecon adaptation must not import an external Hitlist or active-address
seed. `scripts/run_formal.py` runs one adaptive method through this boundary;
`campaign.json` remains the completed fixed-panel D050 configuration.

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
  roots plus response-blind root features; with explicit seeds it also assigns
  intact roots and emits the first calibration C64/IID panel.
- `scripts/run_scan.sh`: a strict, non-interactive wrapper around
  `icmp6_echoscan_time`.
- `scripts/parse_results.py`: joins one raw ZMap round to the target manifest,
  computes RTT, derives the operational response class, and adds timeout rows.
- `strategies/README.md`: the minimal target-producer contract.
- `strategies/journal.py`, `strategies/tnet.py`, and
  `strategies/subrecon.py`: the three independent formal search policies.
- `scripts/run_formal.py`: the formal scan, parse, and cost-ledger loop.

`scripts/analyze_campaign.py` already aggregates the D050 reference labels and
calibration results. The formal runner writes per-method cumulative cost and
discovery ledgers; it does not combine them into a paper figure.

## Formal three-method run

Copy `formal.example.json` to a new configuration on the Linux measurement
host. Replace its proposed budget and coefficients with frozen choices, fill
`input.ris_snapshots` with exact collector RIB identities, and point
`input.bgp_tree_csv` and `input.prior_c64_manifest` at the server's prepared
tree and D050 effective manifest. Check the source address, interface, scan
exclusions, finite packet rate, and output directory. The example has an empty
`ris_snapshots` list, so `run_formal.py` rejects it before sending probes.

```bash
python3 scripts/run_formal.py formal.json journal
python3 scripts/run_formal.py formal.json tnet
python3 scripts/run_formal.py formal.json subrecon
```

Each invocation refuses to overwrite its method directory. Every batch keeps
the target manifest, ordered sent list, scanner command and version, raw ZMap
CSV, parsed per-probe CSV, and strategy metadata. `ledger.csv` records charged
stages, cumulative formal probes, attributed total probes, and distinct newly
observed IMC-positive `/64`s. The journal total includes the 149,652 D050
historical probes; D050 positives are not counted as fresh formal discoveries.
All methods exclude the same D050 search `/64`s from new selection without
using their old responses as search labels.

The TNet adaptation uses a budgeted `/48` screen and `/48` plus `/52` feedback
allocation. It is a paper-based reimplementation because TNet source is not
available here. The conference SubRecon adaptation starts from BGP prefixes,
uses `thuname/subrecon`'s `src/budget.c` probe table, and refines using AU
source diversity and response coverage. Its external Hitlist expansion phase
is omitted. Native comparator regions or prefixes stay in
`native-prefixes.txt` separately from the common positive `/64` count.

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

The default preparation stage does not subdivide a short root or assign
tranches. For the already completed D050 calibration, a response-blind root
assignment selected the sampled roots before probing. That assignment remains
only provenance of the D050 target plan; the final comparison uses the entire
eligible routed tree and has no root-based evaluation split. The D050
calibration C64/IID targets are emitted only when the historical root-assignment
seed and a separate target seed are both supplied.

For the D050 plan, the recorded root-depth strata are `d0`, `d1`, `d2`, and
`d3plus`. Re-running into a new output directory reproduces the historical
one-fifth root assignment by deterministic hash rank:

```bash
python3 scripts/prepare_campaign.py \
  data/interim/ris_ipv6_prefixes_unique.csv \
  runs/frame-root-split-native-v1 \
  --exclude-prefix-file config/frame_exclusions.txt \
  --split-seed 'bep-journal-root-split-v1-20260919' \
  --calibration-root-fraction 1/5
```

Every prefix under one root receives the same historical tranche label. The
summary reports root counts and C64 combinatorial mass for both sides and for
each depth stratum. C64 mass is diagnostic only and does not control the formal
probe budget.

`config/frame_exclusions.txt` currently excludes `2002::/16`. It is IANA
special-purpose 6to4 transition space: native IPv6 routing sends the aggregate
toward a 6to4 relay rather than treating it as ordinary operator-delegated C64
space. The source RIS CSV remains unchanged, and the summary records every
configured exclusion and the number of removed rows.

After verifying the corrected frame and root split, generate the first
calibration plan in a new directory:

```bash
python3 scripts/prepare_campaign.py \
  data/interim/ris_ipv6_prefixes_unique.csv \
  runs/calibration-plan-native-v1 \
  --exclude-prefix-file config/frame_exclusions.txt \
  --split-seed 'bep-journal-root-split-v1-20260919' \
  --calibration-root-fraction 1/5 \
  --calibration-target-seed 'bep-journal-calibration-targets-v1-20260919' \
  --reference-iids 5
```

The plan contains one `root_uniform` C64 per calibration root and, for every
nontrivial BGP tree, one distinct `deepest_bgp_guided` C64 where possible. The
uniform arm records its root, conditional C64, and overall inclusion
probabilities. The guided arm is a purposive prior-enrichment comparison and
has no design-based C64 inclusion probability. `calibration_units.csv` records
the panels; `calibration_targets.csv` records every target; and
`targets-search.txt` plus `targets-reference-1.txt` through
`targets-reference-5.txt` are separately hash-shuffled scan rounds. The same
inputs and seeds reproduce the same files.

## Parse one scan round

Parse each raw round against the complete target manifest and its round name:

```bash
python3 scripts/parse_results.py \
  runs/calibration-plan-native-v1/calibration_targets.csv \
  search \
  runs/calibration-search/raw-search.csv \
  runs/calibration-search/probes-search.csv
```

Valid round names are `search` and `reference-1` through `reference-5`. The
parser uses the validated `orig-dest-ip` field to join a response to its unique
planned target and computes RTT from the integer send and receive timestamps.
It derives `direct`, `slow_au`, `fast_au`, `nr`, `ap`, `rr`, `tx`,
`other_error`, `timeout`, or `unmatched`; only direct Echo Reply and Type 1
Code 3 at RTT greater than or equal to 1000 ms set
`is_observed_positive=1`. A JSON summary is written beside the parsed CSV.
Multiple validated responses of the same class for one planned target are
collapsed to the first arrival and their multiplicity is recorded. Different
response classes for one target stop parsing; the raw CSV is left unchanged.
