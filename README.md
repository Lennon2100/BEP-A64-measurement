# BEP-A64 IPv6 Measurement Toolkit

This repository contains the Linux measurement pipeline for comparing IPv6
`/64` discovery strategies over BGP-routed address space. It prepares RIPE RIS
prefixes, generates targets, runs an IPv6-capable ZMap fork, classifies ICMPv6
responses, saves resumable checkpoints, and produces cost-discovery tables.

## Methods

| Command | Search strategy | Input |
| --- | --- | --- |
| `adaptive_bep` | BGP-guided Bayesian search across variable prefix lengths | All unique RIS IPv6 prefixes |
| `bep_conference` | Fixed `/32`, `/40`, `/48`, `/56`, `/64` BEP traversal | All unique RIS IPv6 prefixes |
| `subrecon` | SubRecon probe schedule over non-overlapping routed roots | Top-level RIS IPv6 prefixes |
| `tnet` | TNet workflow reconstructed from the published description | All unique RIS IPv6 prefixes |

Each command runs independently with its own budget, target history, strategy
state, and output directory. The methods share the scanner, parser, discovery
rule, archive format, and budget accounting.

## Related Repositories & Projects

- [ZMap](https://github.com/tumi8/zmap)
- [ICMPv6 Destination Reachable](https://github.com/sbaresearch/icmpv6-destination-reachable)
- [IPv6 Hitlist Service](https://ipv6hitlist.github.io/)
- [SubRecon](https://github.com/THUNAME/SubRecon)

## Requirements

- Linux measurement host
- Python 3.10 or newer
- Globally routed IPv6 source address
- Root access or equivalent raw-socket capabilities
- Storage for one uncompressed batch and its compressed archive

On Ubuntu, install the system packages:

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential cmake libgmp-dev gengetopt libpcap-dev flex byacc \
  libjson-c-dev pkg-config libunistring-dev unzip curl gzip bgpdump \
  python3 python3-venv
```

Create the Python environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
chmod +x scripts/*.sh
```

## Quick start

### 1. Build the scanner

```bash
./scripts/build_zmap.sh
```

The command extracts and builds the scanner at:

```text
.build/zmap-build/src/zmap
```

### 2. Prepare RIPE RIS prefixes

Download the current RIBs:

```bash
./scripts/download_ris.sh data/raw/ris/latest
```

Extract IPv6 prefixes and origin ASNs:

```bash
./scripts/extract_ris_prefixes.sh \
  data/raw/ris/latest \
  data/interim/ris_ipv6_prefixes.csv
```

Merge duplicate prefix rows:

```bash
./scripts/dedup_ris_prefixes.sh \
  data/interim/ris_ipv6_prefixes.csv \
  data/interim/ris_ipv6_prefixes_unique.csv
```

Create the non-overlapping root file used by SubRecon:

```bash
python3 scripts/extract_top_level_prefixes.py \
  data/interim/ris_ipv6_prefixes_unique.csv \
  data/interim/ris_ipv6_top_level_prefixes.csv
```

Keep the downloaded RIB files and record their collection time with the run.

### 3. Configure a run

```bash
cp formal.example.json experiment.json
```

Edit these fields:

| Field | Purpose |
| --- | --- |
| `scanner.source_ipv6` | IPv6 address assigned to the measurement host |
| `scanner.interface` | Outgoing interface |
| `scanner.gateway_mac` | Gateway MAC, or `-` for an IP-layer tunnel |
| `scanner.rate_pps` | Packet rate |
| `scanner.cooldown_seconds` | Receive tail after transmission |
| `budget_total_per_method` | Probe ceiling for each method |
| `batch_size` | Probes sent before strategy feedback |
| `output_root` | New run directory |
| `seed` | Run-specific target seed |

### 4. Run a method

Adaptive BEP:

```bash
sudo .venv/bin/python scripts/run_formal.py experiment.json adaptive_bep
```

SubRecon:

```bash
sudo .venv/bin/python scripts/run_formal.py experiment.json subrecon
```

Conference BEP and TNet use their own examples:

```bash
cp bep_conference.example.json bep-conference.json
cp tnet.example.json tnet.json

sudo .venv/bin/python scripts/run_formal.py bep-conference.json bep_conference
sudo .venv/bin/python scripts/run_formal.py tnet.json tnet
```

### 5. Stop and resume

Send `SIGINT` or `SIGTERM` once for a graceful stop. The runner completes the
current scanner batch, archives its evidence and checkpoint, then exits.

Resume with the same configuration and method:

```bash
sudo .venv/bin/python scripts/run_formal.py \
  experiment.json adaptive_bep --resume
```

An interruption before packet transmission leaves an unscanned work directory;
resume discards it and regenerates the batch. A completed scan is parsed and
archived directly from its existing output.

### 6. Analyze completed runs

```bash
.venv/bin/python scripts/analyze_formal.py \
  experiment.json \
  --methods adaptive_bep,subrecon
```

This writes:

- `comparison.csv`: final probe and discovery totals
- `cost-discovery-curve.csv.gz`: cumulative discoveries by probe cost

Create detailed aggregates for one method:

```bash
.venv/bin/python scripts/summarize_formal.py \
  runs/bep-comparison/adaptive_bep
```

## Discovery rule

Each probe selects one address in one `/64`. The parser counts that `/64` as
observed when the response matches either:

- an ICMPv6 Echo Reply from the target; or
- ICMPv6 Destination Unreachable Type 1 Code 3 with round-trip time at least
  `slow_au_threshold_ms`.

The outer source address of a matched Address Unreachable response is saved as
a last-hop router-interface observation.

## Output and recovery

The active batch is stored as `batch-NNNNNNNNN.work`. After parsing, the runner
compresses it to `batch-NNNNNNNNN.tar.gz` and writes a compact checkpoint.

Each archive contains:

- ordered target manifest;
- raw scanner CSV, command, version, and log;
- per-probe evidence;
- strategy feedback aggregates;
- last-hop router-interface observations; and
- post-batch strategy state.

Tables use Parquet and raw scanner output remains CSV. Target generation and
table writing are streamed. Resume loads the latest checkpoint and continues
from the saved batch boundary.

At rate `R`, transmitting `B` probes takes approximately `B / R` seconds.
Cooldown, parsing, compression, and downtime add to the total. Run a small
pilot to choose a batch size that fits the host's memory and disk.

## Repository layout

```text
config/                 Prefix exclusions
scripts/                BGP preparation, scanning, parsing, and analysis
strategies/             Pluggable target-generation methods
vendor/                 Scanner source archive
*.example.json          Example run configurations
```

Generated data, build products, private configurations, and local research
notes are excluded through `.gitignore`.

## Strategy interface

A strategy module under `strategies/` provides:

- `load_frame(config, base_directory)`
- `Strategy.iter_targets(limit)`
- `Strategy.feed_aggregate(...)`
- `Strategy.finish_batch()`
- `Strategy.checkpoint()`
- `Strategy.restore(state)`

The runner imports the module named on the command line, so new methods plug
into the existing scanner.

## Data Open Source Notice

This notice applies to measurement datasets released with this project.

### Usage Restrictions

The data may be used only for academic research. Commercial use, data resale,
commercial product development, profit-making analysis, and distribution to
commercial institutions require prior written permission.

### Liability Statement

We may terminate data access and pursue legal remedies when a user violates
the academic-use terms. Data users assume responsibility for their use of the
data and any consequences arising from that use. For questions or permission
requests, contact [liaoyuxuan@nudt.edu.cn](mailto:liaoyuxuan@nudt.edu.cn).

## License

The source code is licensed under the [Apache License 2.0](LICENSE). The data
notice above governs measurement datasets released with the project.
