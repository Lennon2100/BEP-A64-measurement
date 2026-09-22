# Strategy boundary

A strategy module implements `load_frame(config, base)` and a `Strategy` with
the following streaming contract:

- `iter_targets(limit)` — a generator yielding `(c64, node, mode)` one target
  at a time. It decides the batch's `(node, count)` action plan in one frontier
  pass, then streams targets; it does not commit aggregate state.
- `feed_aggregate(node, mode, probes, positives, replies, sources)` — commit
  one batch aggregate for a node/action class.
- `finish_batch()` — finalise the round (re-arm the frontier, bump the epoch).
- `snapshot()` / `restore(state)` — compact node-aggregate state for resume.

`scripts/run_formal.py` turns streamed targets into a manifest and temporary
`sent-targets.txt`, then runs ZMap and the streaming formal parser. The scanner consumes only `sent-targets.txt`; it need not know which
method produced the targets. The two strategies share packet I/O and the parsed
response contract without sharing search state. Neither receives an external
active seed list; SubRecon uses top-level BGP prefixes and its own charged
feedback.

Every probe is drawn uniformly within exactly one node. Base and adaptive
node-local samples are recorded separately and both are valid for that node's
Beta likelihood. Exact deduplication uses deterministic per-node permutation
cursors and ancestor cursor tests. It needs O(active nodes) state and never
stores one Python object per historical `/64`.

Adding a third strategy means adding its module and configuration entry; the
scan loop selects modules by the configured strategy name.
