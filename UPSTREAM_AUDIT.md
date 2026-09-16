# BValue upstream reuse audit

## Source identity

- Artifact repository: `https://github.com/sbaresearch/icmpv6-destination-reachable`
- Local read-only snapshot: `tmp/repo_snapshot/icmpv6-destination-reachable-main`
- Selected scanner archive:
  `measurements/zmap_versions/aim_zmap_reqnr_single.zip`
- Archive SHA-256:
  `c485e38576a0d59adeed7d3e9fcc607dfef95ecb3ca880b340d273099de9d44b`
- Embedded AIM ZMap HEAD:
  `30864522fe4f072744c54224791c2ac66a2a9261`

The ZIP contains a dirty Git working tree. RTT payload support is committed in
the embedded history (`6f85c303e116d2af75e7e1b332237731402a6ff9`), while the
request-number changes in `module_icmp6_echoscan_time.c` and `send.c` are
uncommitted. Therefore the ZIP hash, not HEAD alone, identifies the scanner used
by this project.

## Reuse unchanged

| Upstream component | Decision | Reason |
| --- | --- | --- |
| `aim_zmap_reqnr_single.zip` | Reuse exact archive | It contains the single-instance IPv6 scanner used by the artifact, including timestamp and quoted-packet recovery. |
| `icmp6_echoscan_time` | Reuse initially | It embeds validation and send time, validates Echo Replies and ICMPv6 errors, and recovers the original destination from quoted inner headers. |
| Generic ZMap CSV output | Reuse | It already supplies receive time and outer source address. |
| CMake build | Reuse through a thin script | No separate packet engine is needed. |

The selected scanner already exposes the facts needed for the first measurement
slice:

| Required fact | Existing field or behavior |
| --- | --- |
| Original/quoted target | `orig-dest-ip` |
| Probe identity | internal validation plus a unique target per scan; `nrsent` is also recovered for quoted ICMPv6 errors |
| Send timestamp | `sent_timestamp_ts`, `sent_timestamp_us` |
| Receive timestamp | generic `timestamp_str` |
| ICMPv6 type/code | module fields `type`, `code` |
| Outer source address | generic `saddr` |

No C patch is required while a scan contains each target at most once. If a
future strategy sends repeated probes to the identical target in one invocation,
the accepted limit must be revisited: the module should emit `nrsent` for Echo
Replies as well as errors and validate that the quoted payload is long enough
before reading it.

## Reuse as ideas, not as executable project code

- Use a fixed RIPE RIS RIB/MRT snapshot as the routed frame.
- Generate targets within the BGP boundary using an explicit bit/IID policy.
- Recover the original target from quoted ICMPv6 error packets.
- Preserve send/receive timestamps and derive RTT offline.

## Do not reuse directly

| Upstream file or workflow | Reason |
| --- | --- |
| `bvalues.ipynb` | It orchestrates reproduction and plotting, not a deployable measurement runtime. |
| `bvalues/tools/bgp/extract_bgp.sh` | It downloads mutable `latest-bview` files, merges 25 collectors, prompts interactively, and deletes working directories. |
| `filter_addr_list_on_bgp.py` | It implements Hitlist-seeded one-address-per-BGP-prefix selection, which is the opposite direction from the journal search. |
| `gen_bvalues.py` | It expands outward from known responsive `/128` seeds and uses unrecorded Python randomness. |
| `gen_48_subs.py` | It materializes huge target strings and uses one fixed IID globally. |
| Original `scan_zmap.sh` files | They contain hard-coded paths/rates, interactive overwrite prompts, incomplete output fields, and no strict shell error handling. |
| `rtt.py` | It depends on pandas for a two-column subtraction and rewrites the raw scan file in place. Raw scanner output must remain immutable. |
| BValue classification/plotting scripts | They encode the paper's Hitlist experiment and its labels, not the journal A64 search/reference separation. |
| Parallel ZMap archive | Its pacing changes serve concurrent rate-limit experiments, which are outside the current measurement design. |

## Minimal additions

1. A reproducible Linux build wrapper for the exact scanner ZIP.
2. A non-interactive, low-rate scan wrapper requesting all required raw fields.
3. Independent strategy programs that emit the common target-file contract.
4. Later, after data collection requires them, separate parsing and evaluation
   scripts that never modify raw ZMap output.

### Modern CMake compatibility patch

Ubuntu with modern CMake/pkg-config exposes `JSON_CFLAGS` as a semicolon-separated
CMake list. The old upstream line that appends this list to the string-valued
`CMAKE_C_FLAGS` turns those semicolons into shell command separators, producing
`cc: fatal error: no input files` and `-I/usr/include/json-c: not found`.

`patches/0001-cmake-json-c-flags.patch` removes only that redundant assignment.
The preceding `include_directories(${JSON_INCLUDE_DIRS})` and existing
`${JSON_LIBRARIES}` linkage already provide the required json-c build settings.
The build wrapper applies the patch to the extracted working copy and never
modifies the archived upstream source.

The ceiling is deliberate: no package framework, generic plug-in loader,
database, workflow engine, or policy state machine is introduced in this slice.
