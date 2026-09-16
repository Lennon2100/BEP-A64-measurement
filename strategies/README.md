# Strategy boundary

A probing strategy is currently any deterministic program that writes:

1. an ordered UTF-8 text file containing one unique IPv6 target per line; and
2. a sidecar CSV that maps each target to the strategy's node/action metadata.

The scanner consumes only the text file. This keeps packet I/O independent from
search logic and lets later BEP, random, PaS, SubRecon-adapted, and journal
strategies share exactly the same Linux scanner.

The target order is part of the strategy output and must already be randomized
when required. The strategy must record its seed. Repeated identical targets in
one ZMap invocation are not supported by the current scanner contract; use
distinct IIDs or separate invocations.

No common Python base class exists yet. Add one only when two implemented
strategies demonstrate shared behavior that cannot remain in small standalone
scripts.

