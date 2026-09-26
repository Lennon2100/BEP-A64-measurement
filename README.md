# BEP-A64 IPv6 Measurement Toolkit

This repository contains the Linux measurement code used to compare IPv6 /64
discovery strategies over BGP-routed address space. It prepares RIPE RIS
prefixes, generates targets in batches, runs an IPv6-capable ZMap fork, parses
ICMPv6 responses, saves resumable checkpoints, and produces cost versus
discovery tables.

The code sends real IPv6 packets. Use it only from a host and network where you
have permission to conduct active measurements. Start with a low packet rate,
publish contact information for the source address, and maintain an exclusion
list for opt-out requests.

## Included strategies

| Command name | Description | Starting data |
| --- | --- | --- |
| adaptive_bep | BGP-guided, mixed-depth Bayesian search | All unique RIS IPv6 prefixes |
| bep_conference | Fixed /32, /40, /48, /56, /64 BEP traversal | All unique RIS IPv6 prefixes |
| subrecon | BGP-only adaptation using SubRecon's published probe table | Top-level RIS IPv6 prefixes |
| tnet | Reimplementation from the TNet paper description | All unique RIS IPv6 prefixes |

The SubRecon implementation does not use an external active-address hitlist.
TNet had no public implementation available when this repository was prepared,
so its module is a documented reimplementation rather than a source-level
reproduction.

All methods share the same scanner, response parser, /64 discovery rule, batch
archive format, and budget accounting.

## Repository layout

~~~text
config/                 Prefix exclusions
patches/                Small compatibility fixes for the scanner
scripts/                Data preparation, scanning, parsing, and analysis
strategies/             Independent target-generation strategies
vendor/                 Unmodified upstream scanner archive
*.example.json          Example measurement configurations
~~~

Generated data, build products, run configurations, and scan results are
ignored by Git.

## Requirements

The measurement runner is intended for Linux. Windows is suitable for reading
and editing the code, but the packet scanner and shell scripts require Linux.

Recommended environment:

- Ubuntu 22.04 or newer
- Python 3.10 or newer
- Memory sized from a representative pilot run
- A globally routed IPv6 source address
- Root or the required raw-socket capabilities
- Enough storage for the uncompressed working batch and its compressed archive

Install the system packages:

~~~bash
sudo apt-get update
sudo apt-get install -y \
  build-essential cmake libgmp-dev gengetopt libpcap-dev flex byacc \
  libjson-c-dev pkg-config libunistring-dev unzip patch \
  curl gzip bgpdump python3 python3-venv
~~~

Create a Python environment:

~~~bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
chmod +x scripts/*.sh
~~~

## Quick start

### 1. Build the scanner

The repository includes the upstream scanner archive required by the
measurement pipeline. The build script verifies that archive and applies the
four patches under patches/ to the extracted build tree.

~~~bash
./scripts/build_zmap.sh
~~~

A successful build prints the ZMap binary path:

~~~text
.build/zmap-build/src/zmap
~~~

See UPSTREAM_AUDIT.md for the source identity and the reason for each patch.

### 2. Prepare the BGP input

Download the current RIB from each configured RIPE RIS collector:

~~~bash
./scripts/download_ris.sh data/raw/ris/latest
~~~

Extract IPv6 prefix and origin-AS rows:

~~~bash
./scripts/extract_ris_prefixes.sh \
  data/raw/ris/latest \
  data/interim/ris_ipv6_prefixes.csv
~~~

Collapse duplicate prefixes while retaining all reported origin ASNs:

~~~bash
./scripts/dedup_ris_prefixes.sh \
  data/interim/ris_ipv6_prefixes.csv \
  data/interim/ris_ipv6_prefixes_unique.csv
~~~

Create the non-overlapping top-level prefix file required by SubRecon:

~~~bash
python3 scripts/extract_top_level_prefixes.py \
  data/interim/ris_ipv6_prefixes_unique.csv \
  data/interim/ris_ipv6_top_level_prefixes.csv
~~~

The two inputs used by the strategies are now:

~~~text
data/interim/ris_ipv6_prefixes_unique.csv
data/interim/ris_ipv6_top_level_prefixes.csv
~~~

The download command retrieves moving latest snapshots. Record the collection
time and keep the downloaded files if the experiment must be reproducible.

### 3. Create a run configuration

For Adaptive BEP and SubRecon:

~~~bash
cp formal.example.json experiment.json
~~~

For the other methods:

~~~bash
cp bep_conference.example.json bep-conference.json
cp tnet.example.json tnet.json
~~~

Edit the copied file before running it. At minimum, replace:

| Field | Meaning |
| --- | --- |
| scanner.source_ipv6 | Source IPv6 address assigned to the measurement host |
| scanner.interface | Outgoing interface |
| scanner.gateway_mac | IPv6 gateway MAC, or a dash for an IP-layer tunnel |
| scanner.rate_pps | Global packet rate for this process |
| scanner.cooldown_seconds | Receive tail after the last transmitted packet |
| budget_total_per_method | Maximum probes charged to one method |
| batch_size | Targets sent before the strategy receives feedback |
| output_root | New directory for this experiment |
| seed | Run-specific deterministic target seed |

The example IPv6 address and MAC address are documentation values and must be
replaced. Never reuse an output directory from a different configuration.

Check that the JSON is valid:

~~~bash
python3 -m json.tool experiment.json >/dev/null
~~~

### 4. Run one strategy

Adaptive BEP:

~~~bash
sudo .venv/bin/python scripts/run_formal.py experiment.json adaptive_bep
~~~

SubRecon:

~~~bash
sudo .venv/bin/python scripts/run_formal.py experiment.json subrecon
~~~

Conference BEP and TNet use their own example configurations:

~~~bash
sudo .venv/bin/python scripts/run_formal.py \
  bep-conference.json bep_conference

sudo .venv/bin/python scripts/run_formal.py tnet.json tnet
~~~

Each command runs one independent method. Probe budgets and target history are
not shared between methods.

### 5. Stop and resume

Send SIGINT or SIGTERM once for a graceful stop. The current scanner batch
finishes, its evidence and checkpoint are archived, and the runner exits.

Resume with the same configuration file and method name:

~~~bash
sudo .venv/bin/python scripts/run_formal.py \
  experiment.json adaptive_bep --resume
~~~

The saved configuration must match exactly. A completed run cannot be resumed.

If the process stopped before ZMap started, the next resume discards the
unscanned work directory and regenerates that batch. If raw scan output exists,
keep the work directory: resume parses and archives the existing result without
sending the batch again.

### 6. Analyze completed runs

After every selected method has written summary.json:

~~~bash
.venv/bin/python scripts/analyze_formal.py \
  experiment.json \
  --methods adaptive_bep,subrecon
~~~

This creates:

- comparison.csv: final cost and discovery totals
- cost-discovery-curve.csv.gz: cumulative discoveries by probe cost

Create detailed aggregates for one method:

~~~bash
.venv/bin/python scripts/summarize_formal.py \
  runs/bep-comparison/adaptive_bep
~~~

The detailed summary includes response composition and budget by prefix length.

## What counts as a discovery

One probe targets one randomly selected address inside one /64. A /64 is
counted as observed when the parser matches either:

- an ICMPv6 Echo Reply from the target, or
- an ICMPv6 Address Unreachable response whose measured round-trip time is at
  least slow_au_threshold_ms.

The matched outer source address of an Address Unreachable response is retained
as a candidate last-hop router interface. It is an observed interface address,
not a verified count of physical routers.

## Batch files and checkpoints

While a batch is running, its files are stored in a directory named
batch-NNNNNNNNN.work. After parsing succeeds, the runner creates
batch-NNNNNNNNN.tar.gz, verifies the archive, and removes the work directory.

Each archive contains:

- the ordered target manifest
- the raw ZMap CSV
- the exact scanner command and scanner version
- compact per-probe evidence
- strategy feedback aggregates
- candidate last-hop router observations
- the post-batch checkpoint
- the scanner log

Tables are stored as Parquet. Raw ZMap output remains CSV. Target generation and
table writing are streamed so a complete batch is not duplicated in Python
lists.

The runner refuses to overwrite an existing run or result file.

## Resource planning

The configured budget is a ceiling, not an estimate of how long the strategy
will remain productive.

At rate R packets per second, transmission time for B probes is approximately:

~~~text
B / R seconds
~~~

For example, 5 billion probes at 1,000 packets per second require about 57.9
days of transmission for one method. Batch cooldowns, parsing, compression, and
downtime add to that duration.

Adaptive BEP retains a BGP tree and a mixed-depth search frontier. Large runs
need more memory than SubRecon even at the same probe count. Monitor resident
memory and free disk space during pilot runs before selecting a full budget.

## Adding a strategy

A module under strategies/ provides:

- load_frame(config, base_directory)
- Strategy.iter_targets(limit)
- Strategy.feed_aggregate(...)
- Strategy.finish_batch()
- Strategy.checkpoint()
- Strategy.restore(state)

See strategies/README.md for the complete contract. The runner imports the
module named on the command line, so a new module does not require changes to
the scanner.

## Scanner provenance

The vendored scanner archive comes from the public
sbaresearch/icmpv6-destination-reachable artifact and is kept unchanged. Local
build and IPv6 output fixes are applied as separate patch files. The archive
identity, embedded revision, output fields, and patch rationale are recorded in
UPSTREAM_AUDIT.md.

## Known limits

- The scanner build and packet transmission path are Linux-only.
- TNet is implemented from the paper description because its source was not
  available.
- SubRecon uses a BGP-only starting set and therefore omits its external
  hitlist-driven expansion input.
- Internet-wide measurements can take days or months at conservative rates.
- Strategy checkpoints are Python pickle files. Load checkpoints only from a
  run directory you control.
