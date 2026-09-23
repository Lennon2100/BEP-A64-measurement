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
The scanner need not know which method produced a target. The journal strategy
reads the full unique RIS prefix CSV; the BGP-only ICNP SubRecon adaptation
reads the separate top-level-prefix CSV. Neither receives historical response labels
or an external Hitlist. `scripts/run_formal.py` runs one method through this boundary;
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
- `scripts/parse_results.py`: joins one calibration ZMap round to its manifest.
- `scripts/parse_formal_results.py`: streams a formal manifest while retaining
  only the current batch's responses and compact node feedback in memory.
  computes RTT, derives the operational response class, and adds timeout rows.
  Formal runs also request matched Type 1 Code 3 AU source addresses before
  same-class multi-response folding.
- `strategies/README.md`: the minimal target-producer contract.
- `strategies/journal.py` and `strategies/subrecon.py`: the implemented
  formal search policies. TNet remains pending because its unpublished code
  and unspecified numerical policy do not determine a unique reproduction.
- `scripts/run_formal.py`: the formal scan/parse loop with streaming target
  generation and compact cursor/node checkpoints (no archive replay).
- `scripts/analyze_formal.py`: derives per-method cost/discovery curves from
  each batch's `probes.csv`; no separate per-target ledger file is stored.

`scripts/analyze_campaign.py` already aggregates the D050 reference labels and
calibration results. The formal runner writes per-method cumulative cost and
discovery curves on demand; it does not combine them into a paper figure.

## Formal journal and SubRecon run

Copy `formal.example.json` to a new configuration on the Linux measurement
host. It records a 5B ceiling for each method and `theta_b=1`, whose root
base quotas fit inside the configured half-budget root allocation by
response-blind arithmetic. Confirm the
source address, interface, finite packet rate, and output directory before
sending probes. The input paths in the example match
`MEASUREMENT_RUNBOOK.md`: the journal method loads
`data/interim/ris_ipv6_prefixes_unique.csv`, and SubRecon loads
`data/interim/ris_ipv6_top_level_prefixes.csv`. Both start with zero formal
probe cost and deduplicate targets within their own run.

```bash
sudo python3 scripts/run_formal.py formal.json journal
sudo python3 scripts/run_formal.py formal.json subrecon
python3 scripts/analyze_formal.py formal.json
```

**Feasibility.** At `rate_pps = 1000`, one probe costs 1 ms, so 5B probes are
about 57.9 days of pure transmit time regardless of batching or cooldown; the
30 s `--cooldown-time` is ZMap's receive tail (needed to capture slow-AU RTTs
up to ~25 s), not a scheduling sleep. One ZMap invocation accepts one fixed
target list and then receives for one tail, so the strategy feedback round and
the scanner file batch are the same thing — there is no mid-scan target
injection. The only lever is `batch_size`. The template's `batch_size` of
1,000,000 keeps the tail overhead near 3% while updating the posterior about
every 17 minutes; the 5B ceiling is therefore a config ceiling, not a promise
that a single continuous run is practical. Size the first real run to days,
not months, and raise `rate_pps` only with host/operator authorization.

Start a method without `--resume` only when its output directory is absent.
For a graceful stop, send SIGINT or SIGTERM once; the current batch finishes,
is compressed, and the runner exits with `status: paused`. Continue it with:

```bash
sudo python3 scripts/run_formal.py formal.json journal --resume
sudo python3 scripts/run_formal.py formal.json subrecon --resume
```

Each completed batch is one `batch-000000001.tar.gz` archive containing a
single `manifest.csv` (the ordered target table plus `node`/`mode`), scanner
command and version, raw ZMap CSV, compact parsed per-probe evidence, node
feedback, matched AU router observations, and the post-batch checkpoint. The temporary `.work`
directory is removed only after the archive has been written and read back.
Compression temporarily needs space for both the working batch and its
archive. A `.tar.gz.part` or a `.work` directory without complete raw scan
evidence blocks automatic resume and requires manual inspection.

If scanning completed and only formal parsing failed, keep the next `.work`
directory and run with `--resume`. The runner restores the previous archive,
regenerates and compares that batch's deterministic manifest, then parses and
archives the existing raw CSV without invoking ZMap again. Formal parsing
charges one target once even when it produced several response classes: any
matched IMC-positive response makes it positive, raw CSV retains every reply,
and every AU source is retained as last-hop-router evidence.

Resume reads the newest archive's `state.json` (per-node aggregates and
deterministic generator cursors). It does **not** replay completed archives or
load an accumulated probe set; startup and resident strategy state are
O(active nodes). The configuration must match the recorded `formal.json`.

Each method writes `last-hop-routers.txt.gz` with distinct observed AU source IPv6
addresses; `summary.json` and `comparison.csv` report that count. These are
candidate last-hop interface addresses, not verified router identities, and
they do not change the discovery metric or seed a strategy. Historical probes and
target lists are not loaded into a formal method.

The conference SubRecon adaptation starts from the top-level BGP prefixes,
uses `thuname/subrecon`'s `src/budget.c` probe table, and refines using AU
source diversity and response coverage. Its external Hitlist expansion phase
is omitted. When present, native comparator prefixes are written to
`native-prefixes.txt.gz` separately from the common positive `/64` count.
`analyze_formal.py` derives `comparison.csv` and `cost-discovery-curve.csv.gz`
from each batch's `probes.csv`.

To add TNet later, add `strategies/tnet.py` with `load_frame(config, base)`,
`Strategy.iter_targets(limit)`, `Strategy.feed_aggregate(...)`,
`Strategy.finish_batch()`, and `snapshot()/restore()`, then add its parameters
under `strategies.tnet` in the configuration. The runner and analyzer select
configured strategy names; TNet's `/48` screen probes must be reported as a
charged stage by that strategy.

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
overwrite an existing raw result. It passes the interface as `-i`, the actual
IPv6 packet source as `--ipv6-source-ip`, and `-S 0.0.0.0` to bypass this fork's
IPv4 interface-address lookup during an IPv6 scan.

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
and snapshot time separately if needed; the formal runner does not require
snapshot metadata. The deduplicated prefix file still contains
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
