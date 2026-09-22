# Strategy boundary

A strategy implements `next_batch(limit)` and `feedback(parsed_rows)`.
`scripts/run_formal.py` turns each proposed batch into:

1. an ordered UTF-8 text file containing one unique IPv6 target per line; and
2. a sidecar CSV that maps each target to the strategy's node/action metadata.

The scanner consumes only the text file. The three strategy files share packet
I/O and the parsed response contract without sharing search state. None
receives an external active seed list; SubRecon uses BGP prefixes and its own
charged feedback.

The target order is part of the strategy output and must already be randomized
when required. The strategy must record its seed. Repeated identical targets in
one ZMap invocation are not supported by the current scanner contract; use
distinct IIDs or separate invocations.

`common.py` holds the BGP-frame and target-selection code used by all three.
