# Strategy boundary

A strategy module implements `load_frame(config, base)` and a `Strategy` with
`next_batch(limit)` and `feedback(parsed_rows)`.
`scripts/run_formal.py` turns each proposed batch into:

1. an ordered UTF-8 text file containing one unique IPv6 target per line; and
2. a sidecar CSV that maps each target to the strategy's node/action metadata.

The scanner consumes only the text file. The two implemented strategies share packet
I/O and the parsed response contract without sharing search state. None
receives an external active seed list; SubRecon uses top-level BGP prefixes and its own
charged feedback.

The runner shuffles each proposed batch before ZMap using the configured seed.
The strategy holds one cumulative set of targeted `/64`s for its method, so a
later refinement does not send a second probe to the same `/64`.

`common.py` holds the frame and target-selection code used by both. Adding a
third strategy means adding its module and configuration entry; the scan loop
selects modules by the configured strategy name.
