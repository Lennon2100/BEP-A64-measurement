# Strategy boundary

A strategy module implements `load_frame(config, base)` and a `Strategy` with
the following streaming contract:

- `iter_targets(limit)` — a generator yielding `(c64, node, mode)` one target
  at a time. It decides the batch's `(node, count)` action plan in one frontier
  pass, then streams targets; it does not commit aggregate state.
- `feed_aggregate(node, mode, probes, positives, replies, sources)` — commit
  one batch aggregate for a node/action class.
- `finish_batch()` — finalise the round (re-arm the frontier, bump the epoch).
- `checkpoint()` / `restore(state)` — compact node-aggregate state for resume; `snapshot()` remains for legacy JSON checkpoints.

`scripts/run_formal.py` turns streamed targets into a manifest and temporary
`sent-targets.txt`, then runs ZMap and the streaming formal parser. The scanner consumes only `sent-targets.txt`; it need not know which
method produced the targets. The strategies share packet I/O and the
parsed response contract without sharing search state. None receives an
external active seed list; SubRecon uses top-level BGP prefixes and its own
charged feedback. TNet constructs its candidate `/48` set from charged AU
router observations before beginning its uniform and adaptive rounds.

The conference BEP strategy uses the same contract but consumes the additional
`bep_active`, `bep_inactive`, and `bep_null` node aggregates. These preserve the
paper's native likelihood without changing the common IMC discovery count.

Every probe is drawn uniformly within exactly one node. Base and adaptive
node-local samples are recorded separately and both are valid for that node's
Beta likelihood. Exact deduplication uses deterministic per-node permutation
cursors and ancestor cursor tests. A split screens each of its at most 256
sibling partitions once as one complete action before they enter the mixed-depth priority frontier.
Regions containing deeper or more numerous BGP more-specifics receive a
bounded priority bonus. State is O(screened nodes), independent of repeated
probes within those nodes, and never stores one object per historical `/64`.

Additional strategies need only a module and configuration entry; the scan
loop selects modules by the configured strategy name.
