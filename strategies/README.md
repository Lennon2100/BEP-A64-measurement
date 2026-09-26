# Strategy interface

Each measurement method is a Python module in this directory. The runner loads
the module whose name is passed on the command line.

A module must provide:

~~~python
def load_frame(config, base_directory):
    ...

class Strategy:
    def __init__(self, frame, targets, config, allowance):
        ...

    def iter_targets(self, limit):
        yield c64_integer, node_name, action_name

    def feed_aggregate(
        self, node_name, action_name, probes, positives, replies, sources
    ):
        ...

    def finish_batch(self):
        ...

    def checkpoint(self):
        ...

    def restore(self, state):
        ...
~~~

## Batch lifecycle

For every batch, the runner:

1. calls iter_targets(limit);
2. streams the yielded targets to the manifest and ZMap target file;
3. saves the generated strategy state;
4. runs ZMap;
5. parses responses into per-target evidence and per-action aggregates;
6. calls feed_aggregate() for each action;
7. calls finish_batch();
8. saves a resumable checkpoint and archives the batch.

A strategy must not update response-dependent state while yielding targets.
Feedback becomes available only after the scanner process finishes.

## Target values

c64_integer is the upper 64 bits of an IPv6 address. The shared Targets class
adds a random 64-bit interface identifier immediately before the target is
written to the scanner input.

Targets maintains a deterministic permutation cursor for each sampled prefix.
A strategy can pass ancestor prefixes when drawing from a child so that a /64
sampled at a broader level is not sent again.

## Feedback fields

The common aggregate contains:

| Field | Meaning |
| --- | --- |
| probes | Targets charged to this action |
| positives | Distinct /64s matching the common discovery rule |
| replies | Targets with at least one matched response |
| sources | Distinct matched ICMPv6 source addresses |

Conference BEP also consumes bep_active, bep_inactive, and bep_null counts
through feed_class_aggregate(). TNet consumes matched last-hop observations
through feed_router_observation().

## Checkpoints

checkpoint() must return only data required to continue the strategy. The
runner serializes the result before scanning and again after applying feedback.

Keep checkpoint state proportional to active search nodes. Do not store raw
responses or one Python object per historical probe; those records already
exist in the batch archive.

## Included modules

| Module | Purpose |
| --- | --- |
| adaptive_bep.py | BGP-guided mixed-depth Bayesian search |
| bep_conference.py | Fixed five-level BEP reproduction |
| subrecon.py | BGP-only SubRecon delimitation |
| tnet.py | TNet paper-description reimplementation |
| common.py | Prefix loading and deterministic /64 target generation |
| journal.py | Compatibility import for older configurations |
