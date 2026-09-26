# Scanner source and patches

## Upstream source

The file vendor/aim_zmap_reqnr_single.zip was copied without modification from
the public repository:

https://github.com/sbaresearch/icmpv6-destination-reachable

Upstream location:

~~~text
measurements/zmap_versions/aim_zmap_reqnr_single.zip
~~~

Archive SHA-256:

~~~text
c485e38576a0d59adeed7d3e9fcc607dfef95ecb3ca880b340d273099de9d44b
~~~

Embedded Git revision:

~~~text
30864522fe4f072744c54224791c2ac66a2a9261
~~~

The archive contains request-number changes that are not represented by the
embedded revision alone. The archive hash is therefore the reproducible source
identity used by this repository.

## Why this scanner is used

The ICMPv6 Echo measurement module provides the fields required to associate a
response with its original target and calculate round-trip time:

| Measurement fact | Scanner field |
| --- | --- |
| Original destination | orig-dest-ip |
| Outer response source | saddr |
| ICMPv6 type and code | type, code |
| Send time | sent_timestamp_ts, sent_timestamp_us |
| Receive time | timestamp_ts, timestamp_us |
| Probe request number | nrsent |

The parser keeps raw output unchanged and derives response classes in a
separate step.

## Local patches

The build script extracts the archive under .build/ and applies four patches.
The vendored ZIP is never modified.

### 0001-cmake-json-c-flags.patch

Modern CMake exposes JSON_CFLAGS as a list. The upstream build appends that list
to a string, which turns separators into shell commands. The patch removes the
redundant assignment; the existing include and link directives already provide
the json-c paths.

### 0002-gengetopt-relative-includes.patch

Five generated C files contain absolute header paths from the upstream build
machine. The patch changes only those includes to local header names.

### 0003-ipv6-iplayer-ethertype.patch

In IP-layer mode the upstream sender marks all packets as IPv4. On an IPv6
tunnel this emits protocol 4 instead of protocol 41. The patch selects the IPv6
EtherType for IPv6 probes and leaves the IPv4 path unchanged.

### 0004-icmp6-echo-field-alignment.patch

The Echo Reply output path omits nrsent even though the field is declared. That
shifts later CSV values into the wrong columns. The patch emits nrsent for Echo
Replies and checks that a quoted ICMPv6 packet contains the complete request
payload before reading it.

## Build behavior

scripts/build_zmap.sh:

1. verifies the archive SHA-256;
2. extracts it under .build/;
3. applies each patch only when the expected upstream line is present;
4. refuses to continue when the source does not match the expected form;
5. builds the scanner with CMake.

This keeps the upstream archive intact and makes every local source change
reviewable as a small patch.
