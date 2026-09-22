# Measurement Runbook — 实网测量交接文档

本文档是给接手"正式实网测量"的实现者（人或 agent）的交接说明。它把当前已完成的校准管线、服务器环境、ZMap 参数、数据契约、以及**当前（2026-09-22）研究方向和下一步要写什么**一次性讲清楚，使接手者不需要再翻代码就能动手。

**2026-09-22 状态**：`strategies/journal.py`、`strategies/subrecon.py`、`scripts/run_formal.py` 与 `scripts/analyze_formal.py` 已写入 Windows 工作区，尚未在 Linux 测量主机执行。两种策略各有独立状态，共用发包、解析和记账流程，也都记录末跳路由器候选地址；正式 runner 现按批压缩归档，并支持从完整归档续跑。TNet 暂缓；后续增加 `strategies/tnet.py` 和配置项即可接入。当前示例配置为**每种方法各自 5B 总预算**，原先讨论的 100B 是上限。正式 runner 不读取 scan 排除表；下文涉及 `scan_exclusions.txt` 的段落只记录 D050 校准历史。用户纠正前曾执行三项本机模拟反馈测试；相关测试文件及缓存已删除，纠正后未再运行模拟或验证。本轮没有公网探测。

### RIS 输入与末跳路由器副产品

`scripts/download_ris.sh` 曾逐个下载 `https://data.ris.ripe.net/<rrc>/latest-bview.gz` 并生成当前前缀 CSV。正式 runner 只读取冻结的前缀 CSV，不读取 RIB 或 RIS 快照身份；当前 `formal.example.json` 已移除 `input.ris_snapshots`，运行时无须补填。

正式 parser 现在可把**匹配目标的 ICMPv6 Type 1 Code 3 Address Unreachable 响应源 IPv6**另存为每批 `last-hop-router-observations.csv`，包括慢 AU、快 AU 和同目标的多条同类响应。方法结束时，runner 写出按地址去重的 `last-hop-routers.txt.gz`，并在 `summary.json` / `comparison.csv` 中报告地址数。这个判据参照 SubRecon 公开代码的 delimitation 接收分支；它提供的是**候选末跳路由器接口地址**，不是经过独立拓扑核验的设备数。原始 CSV 和逐探针结果仍保留；该副产品不触发额外探针、不作为外部种子，也不改变 IMC 阳性 `/64` 的计分。

### 正式扫描能否直接开始

**当前可在具备数据和 ZMap 的 Linux 主机上调用真实扫描代码，但本轮尚未核实该主机状态。** 本机工作区没有以下运行数据；它们据历史记录位于 Linux 主机：`data/interim/ris_ipv6_prefixes_unique.csv`、`data/interim/ris_ipv6_top_level_prefixes.csv`、`runs/calibration-native-v1/effective_calibration_targets.csv`。本机也没有 `.build/zmap-build/src/zmap`；Linux 构建状态未在本轮核实。RIS 快照身份不是运行条件。

运行规模仍有限制：`Targets.used`、策略观测与前缀状态均常驻内存；续跑时必须从已压缩批次的真实解析结果重建这些状态，启动时间随已完成探针数增长。单次 SIGINT/SIGTERM 会在当前批次归档后暂停；若进程在批次中途被强制终止，可能已有探针发出，不能自动重发该批。在示例 `rate_pps=1000`、`batch_size=8192`、每批 `cooldown_seconds=30` 下，即使降至 5B，仅发送与冷却的理论时间仍约 **270 天/方法**，还不包括计算、解析和主机故障时间。`budget_total_per_method` 是独立上限；策略若没有下一批候选，会提前结束，代码不保证每种方法恰好用满 5B。TNet 尚未实现，三方比较也不能执行。

### Linux 上的调用方式（完成上述运行条件后）

从 `/root/BEP-A64-measurement/` 执行；配置中的相对路径均相对于配置文件所在目录解析。先将工作区的 `strategies/`、`scripts/run_formal.py`、`scripts/analyze_formal.py`、更新后的 `scripts/parse_results.py` 和 `formal.example.json` 同步到 Linux 项目根目录，保留已有的 `scripts/run_scan.sh`、`scripts/prepare_campaign.py` 和 `scripts/count_prefix_nesting.py`。Windows PowerShell 的文件传输命令如下：

```powershell
Set-Location D:\codex_workplace\kuokan\measurement
scp -r .\strategies root@meta-codes-1:/root/BEP-A64-measurement/
scp .\scripts\run_formal.py .\scripts\analyze_formal.py .\scripts\parse_results.py root@meta-codes-1:/root/BEP-A64-measurement/scripts/
scp .\formal.example.json root@meta-codes-1:/root/BEP-A64-measurement/
```

若 ZMap 尚未构建，按下文第 3.1 节在 Linux 构建。复制示例配置为 `formal.json`，按测量主机现状设置源 IPv6、接口、有限速率和新的 `output_root`。不要沿用已存在的方法输出目录。

```bash
cd /root/BEP-A64-measurement
cp formal.example.json formal.json
# 编辑 formal.json：扫描接口/源地址/速率、独立输出目录
sudo python3 scripts/run_formal.py formal.json journal
sudo python3 scripts/run_formal.py formal.json subrecon
python3 scripts/analyze_formal.py formal.json
```

需要有序暂停时，对 runner 发送一次 SIGINT（终端 Ctrl+C）或 SIGTERM，等待当前批次完成压缩，输出 `status: paused` 后再退出。只从已归档批次续跑，命令为：

```bash
sudo python3 scripts/run_formal.py formal.json journal --resume
sudo python3 scripts/run_formal.py formal.json subrecon --resume
```

续跑使用与首次运行内容相同的 `formal.json`；已存在 `summary.json` 表示方法已完成，不能续跑。每批工作目录为 `batch-<九位序号>.work/`，成功归档为同名 `.tar.gz` 后才删除工作目录。压缩期间需同时容纳本批原文件和压缩包。若遗留 `.work/` 或 `.tar.gz.part`，程序拒绝自动续跑：先检查该批的 ZMap 命令、日志及原始结果，确认实际发包范围后人工处理，避免重复探测。

这三个命令分别运行期刊方法、运行 SubRecon、汇总已完成的方法；前两个命令会**实际向公网发包**，不是预览。分析器按 `formal.json` 中 `strategies` 的全部方法读取 `summary.json`，任一方法未完成时不应执行汇总。期刊方法读取完整 `ris_ipv6_prefixes_unique.csv` 并在内存构树；SubRecon 只读取 `ris_ipv6_top_level_prefixes.csv`。两者应用 `config/frame_exclusions.txt` 的 6to4 帧排除，共用 D050 search `/64` 清单仅作新目标去重，不读取旧响应标签。每方法 5B 独立记账；期刊方法的历史 149,652 探针占其总预算，正式阶段上限为 4,999,850,348。每批压缩包保留目标清单、实际命令与 ZMap 版本、原始与解析结果、该批 `ledger.csv` 及末跳源地址证据；方法结束后目录留下 `summary.json`、按需生成的 `native-prefixes.txt.gz` 和去重的 `last-hop-routers.txt.gz`。分析器另写 `comparison.csv` 和 `cost-discovery-curve.csv.gz`。

开始实现前，先按顺序读共享记忆（`project_memory/` 下）：`AGENTS.md` → `PROJECT_CONTEXT.md` → `DECISIONS.md` → `PROGRESS.md` → `TODO.md`。本 runbook 是这些记忆的"测量工程化"摘录，不替代它们。

---

## 0. 一句话现状

**已完成**：从 BGP 输入到逐轮解析的整条校准管线（prepare → run → parse → analyze），并在服务器上跑完了 D050 六轮校准（149,652 个探针，24,942 个 panel）。

**待办**：正式的**全树等总预算三方对比**（journal 方法 vs 论文版 TNet vs ICNP 会议版 SubRecon）尚未完成。`campaign.json` / `analyze_campaign.py` 是校准专用；正式 runner 目前只接入 journal 与 SubRecon。示例配置现为每方法 5B；批次边界可续跑，但 5B 规模的内存与重放成本、批次中途意外中断的恢复，以及 TNet 实现仍待解决。

---

## 1. 科学框架（这次测量到底在测什么）

- **搜索单元 = IPv6 `/64`**。`C64`（候选 `/64`）、`A64`（操作性阳性 `/64`）只是实现缩写，**不是论文贡献**。
- **操作性阳性规则（直接采用 *Destination Reachable* 的 IMC 规则，D058）**：对某个目标 `/64` 发一个随机 IID 的 ICMPv6 Echo Request，若收到
  - 正确匹配的 **Echo Reply**（Type 129），或
  - 正确匹配的 **ICMPv6 Destination Unreachable Type 1 Code 3**，且 **RTT ≥ 1000 ms**，
  则记该 `/64` 为一次"活跃子网发现（active-subnet discovery）"。其它结果（含 timeout）都不是发现。
- **正式任务**：在**固定总探针预算**下，最大化发现的**不同 IMC 阳性 `/64` 数量**。
- **对比对象（D066/D068）**：journal 方法、**论文版 TNet**（本地重实现，`/48` 候选筛选计入其预算）、**ICNP 会议版 SubRecon 的 BGP-only 策略改编**（无外部 Hitlist / 活跃种子 / 末跳路由器种子）。PaS、原始 BEP、random **不是**必跑基线。
- **三方法共同起点（D068，按本轮约束实施）**：同一份冻结的去重 RIS BGP 前缀帧、相同的 6to4 帧排除和 IMC 响应规则；正式运行不使用 scan 排除表。从 BGP 建前缀树是离线计算，**不花探针**。
- **D050 的 149,652 探针归属**：是本项目方法自己的**历史校准/设计成本**，只记到我们这边，**不共享给 TNet/SubRecon，也不用其响应初始化正式搜索**。
- **明确的非目标（D058-D061）**：不做 terminal audit、不做 missed-mass/Horvitz-Thompson/Neyman 估计、不做 parent-mass/完整性理论证明、不做 held-out 评价、不做 action trace、不做 soft-pruning 对比、不做 response-homogeneous block。这些都不是前置门槛。

---

## 2. 服务器环境与关键路径

D050 记录的服务器为 `root@meta-codes-1`，工作根目录 `/root/BEP-A64-measurement/`（下文相对路径都以它为根）；本轮未连接主机核实其当前状态。

| 对象 | 路径 |
|---|---|
| BGP 前缀输入（三列 CSV） | `data/interim/ris_ipv6_prefixes_unique.csv` |
| frame 排除（6to4） | `config/frame_exclusions.txt`（内容 `2002::/16`） |
| D050 校准历史使用的 scan 排除 | `config/scan_exclusions.txt`（正式 runner 不读取） |
| **已生成的 bgp_tree.csv** | `runs/calibration-plan-native-v1/bgp_tree.csv` |
| frame_roots.csv | `runs/calibration-plan-native-v1/frame_roots.csv` |
| calibration_units.csv | `runs/calibration-plan-native-v1/calibration_units.csv` |
| calibration_targets.csv（目标 manifest） | `runs/calibration-plan-native-v1/calibration_targets.csv` |
| 各轮洗牌目标文件 | `runs/calibration-plan-native-v1/targets-search.txt`、`targets-reference-1.txt` … `targets-reference-5.txt` |
| **ZMap 二进制** | `.build/zmap-build/src/zmap`（由 `build_zmap.sh` 产出） |
| 校准运行根 | `runs/calibration-native-v1/` |
| effective manifest（过滤后） | `runs/calibration-native-v1/effective_calibration_targets.csv` |
| 被排除 panel 表 | `runs/calibration-native-v1/excluded_panels.csv` |
| 每轮原始 ZMap 输出 | `runs/calibration-native-v1/<round>/raw-<round>.csv` |
| 每轮解析结果 | `runs/calibration-native-v1/<round>/probes-<round>.csv` |
| 分析输出 | `runs/calibration-native-v1/analysis/panel_labels.csv`、`summary.json` |

**测量主机身份（D050 校准使用，正式测量需重新确认并冻结）**：

- 源 IPv6：`2607:8700:5500:3959::2`
- 网卡：`ipv6net`（SIT/NOARP 点到点 IPv6-in-IPv4 隧道，故用 `--iplayer`，gateway MAC 用 `-`）

---

## 3. ZMap：构建 + 全部参数

### 3.1 构建（`scripts/build_zmap.sh`）

- 源：`vendor/aim_zmap_reqnr_single.zip`（ZIP 校验和 `c485e38576a0d59adeed7d3e9fcc607dfef95ecb3ca880b340d273099de9d44b`）。
- 依赖：`sha256sum unzip patch cmake`（另需 `libgmp-dev gengetopt libpcap-dev flex byacc libjson-c-dev pkg-config libunistring-dev`）。
- 打 **4 个定点补丁**（`patches/` 下，ZIP 本体不动）：
  1. `0001`：CMake 里 `JSON_CFLAGS` 列表拼接修复；
  2. `0002`：gengetopt 生成源码里的绝对 include 路径改相对；
  3. `0003`：**IPv6 IP-layer 协议标签修复**——原 `--iplayer` 把包标成 IPv4（IPIP=4），SIT 隧道需要协议 41，改为 `ETHERTYPE_IPV6`；
  4. `0004`：Echo Reply 分支补齐 `nrsent` 字段，修复 CSV 字段左移错位。
- 构建后二进制：`.build/zmap-build/src/zmap`，模块名 `icmp6_echoscan_time`。

### 3.2 扫描包装（`scripts/run_scan.sh`）

用法（8 个位置参数）：

```text
run_scan.sh ZMAP TARGETS OUTPUT SOURCE_IPV6 INTERFACE GATEWAY_MAC_OR_DASH RATE_PPS COOLDOWN_SECONDS
```

它拼出并执行的实际 ZMap 命令：

```text
<zmap>
  -M icmp6_echoscan_time
  --ipv6-source-ip <source_ipv6>
  --ipv6-target-file <targets>          # 每行一个 IPv6 目标地址
  --probes 1
  --rate <rate_pps>                     # 0 = 不限速（as fast as possible）
  --cooldown-time <cooldown_seconds>    # 发完最后一包后继续收包的秒数
  --interface <interface>
  --output-module csv
  --output-fields <15 个字段，见下>
  --output-filter "success = 0 || success = 1"
  --output-file <output>
  --disable-syslog
  [--iplayer]                            # gateway_mac = "-" 时
  [--gateway-mac <mac>]                  # 以太网口时
```

**ZMap 原始输出 15 字段（顺序固定）**：

```text
orig-dest-ip, classification, success, type, code, saddr, ttl, original_ttl,
sent_timestamp_ts, sent_timestamp_us, nrsent, timestamp_str, timestamp_ts, timestamp_us
```

（`orig-dest-ip` 是从 quoted inner packet 恢复的原目标；`type`/`code` 是 ICMPv6 type/code；`saddr` 是 ICMP 源路由器；`sent_timestamp_*` 与 `timestamp_*` 是发送/接收时刻；RTT 由二者相减得出。）

`run_scan.sh` 还会在输出旁写 `<output>.command.txt`（实际命令）和 `<output>.scanner-version.txt`（ZMap 版本），并**拒绝覆盖已存在的输出文件**。

---

## 4. 管线脚本与运行逻辑

六个脚本（`scripts/` 下），分工各一个：

### 4.1 `prepare_campaign.py` — 准备（已完成）

```bash
python3 scripts/prepare_campaign.py \
  data/interim/ris_ipv6_prefixes_unique.csv \
  runs/<out-dir> \
  --exclude-prefix-file config/frame_exclusions.txt \
  --split-seed 'bep-journal-root-split-v1-20260919' \
  --calibration-root-fraction 1/5 \
  --calibration-target-seed 'bep-journal-calibration-targets-v1-20260919' \
  --reference-iids 5
```

逻辑：解析三列 `prefix,origin_count,origins` → 构造 BGP immediate-parent 树与顶层非重叠根 → 应用 frame 排除（去 `2002::/16`）→ 按 `d0/d1/d2/d3plus` 整根分配 calibration（1/5）→ 用 target seed 生成每 calibration 根 1 个 `root_uniform` C64 +（非平凡树）1 个 `deepest_bgp_guided` C64 → 每 C64 生成 1 search + 5 reference IID → 输出六个全局 hash 洗牌轮次。

输出（都写到 `<out-dir>/`）：
- `bgp_tree.csv`（每 BGP 前缀一行，含 parent/root/depth/children/descendants）
- `frame_roots.csv`（每顶层根一行）
- `summary.json`（结构计数）
- `calibration_units.csv`（每 panel 一行，含选择 arm、入样概率）
- `calibration_targets.csv`（每目标一行，6 轮 manifest）
- `targets-search.txt`、`targets-reference-1..5.txt`（每轮已洗牌的目标列表，一行一个地址）

**IID 生成**：`IID = first_64_bits(SHA256(target_seed || c64 || role || index))`，search/reference IID 互斥，六轮分别按 hash 全局重排。

### 4.2 `run_campaign.py` — 校准专用 runner（已完成）

```bash
sudo python3 scripts/run_campaign.py campaign.json search   # 每轮一次
```

逻辑：读 `campaign.json` → 读 `calibration_targets.csv` → 加载 `scan_exclusions.txt`（denylist）→ **若某 panel 六个目标中任一命中 denylist，整个 panel 排除** → 写 `effective_calibration_targets.csv` + `excluded_panels.csv` → 复用 `prepare` 已洗牌的 `targets-<round>.txt` 过滤出 `sent-targets-<round>.txt` → 复制 `campaign.json` 快照 → `bash run_scan.sh …` 发包。

**关键账目**：`planned panels = effective sent panels + policy-excluded panels`；parser 只读 effective manifest，因此**只有真正发出的目标才可能补成 timeout**。

### 4.3 `parse_results.py` — 逐轮解析（已完成）

```bash
python3 scripts/parse_results.py \
  runs/calibration-native-v1/effective_calibration_targets.csv \
  search \
  runs/calibration-native-v1/search/raw-search.csv \
  runs/calibration-native-v1/search/probes-search.csv
```

逻辑：把单轮原始 ZMap 行用 `orig-dest-ip` 对回唯一计划目标 → 用整数时间戳算 RTT → 派生响应类别 → 补 timeout 行 → 保留 unmatched 行 → **多响应规则**：同类别多响应折叠为"最先到达"并单独计数；跨类别才停止。输出 `probes-<round>.csv` + 同目录 `probes-<round>.csv.summary.json`。

### 4.4 `analyze_campaign.py` — 校准专用分析（已完成）

```bash
python3 scripts/analyze_campaign.py campaign.json
```

读六轮 probes + effective manifest + units + config，产出 `panel_labels.csv`（每 panel 一行，含 search 响应类、search 阳性、`reference_positive_m1..m5`、首个阳性 reference 下标、响应模式、source 数）和 `summary.json`（六类结果 + validation）。

---

## 5. 数据契约（CSV 列）

**`calibration_targets.csv`（plan 字段）**：

```text
probe_id, panel_id, tranche, root_stratum, root_prefix, selection_arm,
c64, target_ipv6, role, iid_index, round
```

**`calibration_units.csv`**：

```text
panel_id, root_stratum, root_prefix, selection_arm, selection_prefix,
selection_prefix_length, selection_bgp_tree_depth, deepest_candidate_count,
c64, root_calibration_pi, conditional_c64_pi, c64_inclusion_pi
```

**`probes-<round>.csv`（plan 字段 + 响应字段）**：

```text
probe_id, panel_id, tranche, root_stratum, root_prefix, selection_arm, c64,
target_ipv6, role, iid_index, round,
match_status, response_class, is_observed_positive, slow_au_threshold_ms,
zmap_classification, zmap_success, icmp_type, icmp_code, icmp_source, ttl,
original_ttl, quoted_target, sent_timestamp_ts, sent_timestamp_us, nrsent,
recv_timestamp_str, recv_timestamp_ts, recv_timestamp_us, rtt_ms, raw_row_number
```

**`bgp_tree.csv`**：

```text
prefix, prefix_length, origin_count, origins, parent_prefix, root_prefix,
bit_depth_from_root, bgp_tree_depth, direct_child_count,
descendant_prefix_count, is_top_level, root_stratum, tranche
```

**`frame_roots.csv`**：

```text
root_prefix, prefix_length, origin_count, origins, direct_child_count,
descendant_prefix_count, max_bgp_tree_depth, routed_c64_count,
root_stratum, tranche
```

---

## 6. 响应模型（标签）

| 响应类别 | 定义 | 阳性？ |
|---|---|---|
| `direct` | ICMPv6 Type 129（Echo Reply） | ✅ |
| `slow_au` | Type 1 Code 3，RTT ≥ 1000 ms | ✅ |
| `fast_au` | Type 1 Code 3，RTT < 1000 ms | ❌ |
| `nr` | Type 1 Code 0（No Route） | ❌ |
| `ap` | Type 1 Code 1（Admin Prohibited） | ❌ |
| `rr` | Type 1 Code 6（Reject Route） | ❌ |
| `tx` | Type 3（Time Exceeded） | ❌ |
| `other_error` | 其它 | ❌ |
| `timeout` | 无响应（parser 补行） | ❌ |
| `unmatched` | 无法对回计划目标 | ❌ |

**阳性 = `direct` 或 `slow_au`**。`slow_au_threshold_ms` 主结果固定 **1000 ms**（D052 已核验：800/1200 ms 仅翻转 2/24942 个标签）。

---

## 7. 当前 `campaign.json`（校准专用，非正式三方法配置）

```json
{
  "campaign_id": "calibration-native-v1",
  "input": {
    "prefix_csv": "data/interim/ris_ipv6_prefixes_unique.csv",
    "frame_exclusions": "config/frame_exclusions.txt",
    "target_manifest": "runs/calibration-plan-native-v1/calibration_targets.csv",
    "ris_snapshots": []
  },
  "sampling": {
    "root_split_seed": "bep-journal-root-split-v1-20260919",
    "target_seed": "bep-journal-calibration-targets-v1-20260919",
    "panel_count": 24942,
    "rounds": ["search", "reference-1", "reference-2", "reference-3", "reference-4", "reference-5"]
  },
  "scanner": {
    "zmap_binary": ".build/zmap-build/src/zmap",
    "source_ipv6": "2607:8700:5500:3959::2",
    "interface": "ipv6net",
    "gateway_mac": "-",
    "probes": 1,
    "rate_pps": 0,
    "cooldown_seconds": 30
  },
  "labels": { "slow_au_threshold_ms": 1000, "threshold_sensitivity_ms": [800, 1000, 1200] },
  "safety": { "scan_exclusions": "config/scan_exclusions.txt" },
  "output_root": "runs/calibration-native-v1"
}
```

注意：`rate_pps: 0` 在 `run_scan.sh` 里被解释为"不限速"。**正式测量应设一个有限的速率上限**，并按 D067/D068 建立三方法各自的预算台账。

---

## 8. 已完成 vs 待办

### 已完成（校准管线，可复用为模板）

- BGP 前缀输入 → 去重/树/顶层根 → frame 排除 → 整根划分 → D050 六轮目标生成。
- ZMap 构建 + 4 补丁 + 扫描包装。
- 薄 runner + denylist 整 panel 排除 + effective manifest。
- 逐轮 parser + 多响应折叠 + calibration 分析器。
- D050 六轮已跑完并分析（D052：`m=1..5` 饱和 1695/1809/1847/1866/1877；search 召回 92.5%；guided 富集 3.75×；1000ms 阈值稳健）。

### 已写入但尚未在 Linux 运行的正式代码

- `scripts/run_formal.py`：按配置动态加载 `strategies.<method>`，每方法独立发包、解析、累计台账；不读取 scan 排除表。
- `strategies/journal.py`：从完整去重 RIS 前缀 CSV 构树，执行 HD 配额与动态搜索；D050 search `/64` 仅用于避免重复发包。
- `strategies/subrecon.py`：从顶层前缀 CSV 出发，使用仓库 `src/budget.c` 的探针表与本方法反馈逐步细分；不接入外部 Hitlist。
- `scripts/analyze_formal.py`：读取配置中的全部已完成方法，输出 `comparison.csv` 和 `cost-discovery-curve.csv.gz`。

### 待办（正式三方法对比）

1. **运行能力**：批次边界已可续跑；5B 规模的内存状态、逐目标计算成本，以及批次中途意外中断的人工核对仍需解决，再考虑长时正式扫描。
2. **TNet**：其代码未开源，现有论文描述不足以唯一确定数值策略；后续实现 `strategies/tnet.py`，其 `/48` 筛选探针计入 TNet 自身的预算。
3. **正式配置**：按 Linux 主机实际网络设置源地址、接口和有限速率；无需 RIS 快照身份和 scan 排除表。
4. **正式测量与对比**：每方法独立核算；D050 149,652 探针只记入期刊方法，D050 历史阳性不算新发现。当前 runner 可在策略提前结束时少于 5B，不能据此宣称完成等成本对比。

### 明确的非目标（不要实现）

terminal audit / missed-mass / HT-Neyman / parent-mass 理论 / 完整性下界 / held-out 评价 / action trace / soft-pruning 对比 / response-homogeneous block / 独立 ground-truth 面板。

---

## 9. 必须遵守的关键决策速查

- **D058**：直接采用 IMC 操作性活跃子网规则；`C64/A64` 只是缩写。
- **D059/D060**：移除 terminal audit、missed-mass、完整性/parent-mass 理论。
- **D061**：直接从 D050 进入 journal 实现；动态展开保留但**不需要 action trace**；timeout = 该 `/64` 的操作性 inactive（非物理不活跃）。
- **D062/D063/D064**：HD-Ratio 形状作 base 配额，journal 用全局 `theta_B` 覆盖所有真实前缀长度；base 之后可加探针；直接呈现 HD-Ratio，不搞 HD-to-IMC 理论审计。
- **D066**：正式对比 = journal vs 论文版 TNet vs ICNP 会议版 SubRecon；PaS/original-BEP/random 不是基线；只统计真正发探针的 IMC 阳性 `/64`。
- **D067/D068**：全树等总预算，无 held-out；共同起点 = 冻结 BGP 前缀帧（建树不花探针）；D050 成本只归本方法；SubRecon 无外部种子。

---

## 10. 数据流与文件依赖

```text
（D050 时一次性获取的输入；正式运行沿用同一冻结文件）
download_ris.sh → extract_ris_prefixes.sh → dedup_ris_prefixes.sh
        └──────────────────────────► data/interim/ris_ipv6_prefixes_unique.csv

data/interim/ris_ipv6_prefixes_unique.csv（冻结输入，三列 prefix,origin_count,origins）
   │
   ├─► extract_top_level_prefixes.py ──► data/interim/ris_ipv6_top_level_prefixes.csv
   │                                       （顶层非重叠前缀 = SubRecon BGP-only 起点）
   │
   └─► prepare_campaign.py ──► runs/calibration-plan-native-v1/
   │        （依赖 count_prefix_nesting.build_immediate_parent_map）
   │        ├─ bgp_tree.csv
   │        ├─ frame_roots.csv
   │        ├─ summary.json
   │        ├─ calibration_units.csv
   │        ├─ calibration_targets.csv
   │        └─ targets-search.txt / targets-reference-1..5.txt
   │
   │     campaign.json + config/scan_exclusions.txt
   │        │
   │        ▼
   └─► run_campaign.py（校准专用 runner）
           ├─ runs/calibration-native-v1/effective_calibration_targets.csv
           ├─ runs/calibration-native-v1/excluded_panels.csv
           └─ runs/calibration-native-v1/<round>/sent-targets-<round>.txt
                │（bash run_scan.sh，调 .build/zmap-build/src/zmap）
                ▼
              runs/calibration-native-v1/<round>/raw-<round>.csv
              （同目录 + .command.txt / .scanner-version.txt / campaign.json 快照）
                │
              parse_results.py
                ▼
              runs/calibration-native-v1/<round>/probes-<round>.csv
              （同目录 + probes-<round>.csv.summary.json）
                │
              analyze_campaign.py（校准专用）
                ▼
              runs/calibration-native-v1/analysis/panel_labels.csv
              runs/calibration-native-v1/analysis/summary.json
```

**关键依赖**：`count_prefix_nesting.py` 的 `build_immediate_parent_map()` 被 `prepare_campaign.py` 和 `extract_top_level_prefixes.py` 共同 import；`run_campaign.py` 通过 `bash run_scan.sh` 发包；`parse_results.py` 读 `run_campaign.py` 写的 effective manifest + `run_scan.sh` 写的 raw；`analyze_campaign.py` 读六轮 probes + effective manifest + units + `campaign.json`。

**正式运行路径**：`formal.json` → `run_formal.py` 动态加载 `strategies/journal.py` 或 `strategies/subrecon.py` → `run_scan.sh` 实际发包 → `parse_results.py` 解析 → 策略接收反馈 → 每批归档包含 `ledger.csv` 的 `.tar.gz` → 方法完成时写 `summary.json` → 全部配置方法完成后由 `analyze_formal.py` 汇总。正式运行不调用 `run_campaign.py`，也不读取校准的 scan 排除表。

## 11. 文件与代码清单（每个文件是什么 + 生命周期）

| 文件 | 内容 | 生命周期 |
|---|---|---|
| `scripts/build_zmap.sh` | 从 vendored ZIP + 4 补丁构建 ZMap → `.build/zmap-build/src/zmap` | 活跃（构建）|
| `scripts/run_scan.sh` | 拼装并执行 ZMap 命令（8 个位置参数） | 活跃（扫描包装，正式测量仍用）|
| `scripts/parse_results.py` | 逐轮解析：raw→probes，RTT、响应类、timeout、多响应折叠 | 活跃（可复用）|
| `scripts/prepare_campaign.py` | BGP 树/顶层根/整根划分/D050 校准目标生成 | 核心可复用；D050 目标生成是校准专用（正式搜索全树、不划根）|
| `scripts/extract_top_level_prefixes.py` | 剥离父子、只留顶层前缀，输出 SubRecon 起点文件 | 活跃（新加）|
| `scripts/run_campaign.py` | 校准专用固定 panel runner（历史 denylist 整 panel 排除 + 调 run_scan.sh） | 校准专用 |
| `scripts/analyze_campaign.py` | 校准专用分析（panel_labels + 六类汇总） | 校准专用 |
| `scripts/run_formal.py` | 按配置加载策略、调用 ZMap 与解析器、写每方法探针台账 | 已写入；Linux 未运行；支持已归档批次续跑 |
| `scripts/analyze_formal.py` | 读取配置中方法的完成汇总与 ledger，生成成本发现表 | 已写入；Linux 未运行 |
| `strategies/common.py`、`journal.py`、`subrecon.py` | 共用前缀帧和目标抽取；期刊与 SubRecon 各自搜索策略 | 已写入；状态常驻内存 |
| `scripts/count_prefix_nesting.py` | 一次性嵌套统计诊断；提供 `build_immediate_parent_map()` | `main()` 一次性；函数被复用 |
| `scripts/download_ris.sh` | 下载 RIPE RIS `latest-bview.gz` | 一次性（输入已冻结）|
| `scripts/extract_ris_prefixes.sh` | `bgpdump` 抽取 + 合并 IPv6 prefix/origin | 一次性 |
| `scripts/dedup_ris_prefixes.sh` | 多 collector 去重为一行一前缀 | 一次性 |
| `campaign.json` | 校准配置（seed、panel、scanner、阈值、路径） | 校准专用 |
| `formal.example.json` | 每方法 5B 上限、正式输入、策略与扫描参数示例 | 已写入；复制为 `formal.json` 后填写实际运行信息 |
| `config/frame_exclusions.txt` | `2002::/16` 排除 | 活跃 |
| `config/scan_exclusions.txt` | D050 校准历史的 scan denylist | 正式 runner 不使用 |
| `patches/0001..0004` | ZMap 4 个定点补丁（cmake / gengetopt / ipv6-ethertype / echo-field） | 活跃（构建）|
| `vendor/aim_zmap_reqnr_single.zip` | vendored ZMap 源（SHA 固定） | 活跃（构建源）|
| `README.md` / `UPSTREAM_AUDIT.md` | 测量运行时说明 / 上游复用审计 | 文档 |
| `vendor/README.md` / `strategies/README.md` | vendor 说明 / 当前策略接口 | 文档 |

**已经失效 / 被取代，不要再去实现或查找的**（来自记忆，非代码）：

- D046 的 `/32` 切根实现 → 已废止，顶层 BGP 前缀才是搜索根；
- D054 的 action trace → 被 D061 取代，**不需要**；
- soft pruning / `defer` → 被 D061 从核心移除；
- terminal audit / missed-mass / HT-Neyman → 被 D059 移除；
- response-homogeneous block → 被 D061 移除；
- held-out 评价 → 被 D067 移除；
- `P_D/P_ND/N_R/U_T/U_C` 词汇 → 历史（D039 后不再用）；
- 早期 `100 /52 PSUs × 256 A64s` 设计 → 已取代（D032 直接抽 C64）。

## 12. 生成文件清单（文件名 + 内容）

| 生成文件 | 内容 | 由谁生成 |
|---|---|---|
| `data/interim/ris_ipv6_top_level_prefixes.csv` | 顶层非重叠前缀（三列，SubRecon 起点） | extract_top_level_prefixes.py |
| `runs/calibration-plan-native-v1/bgp_tree.csv` | 每 BGP 前缀一行：parent/root/depth/children/descendants | prepare_campaign.py |
| `runs/calibration-plan-native-v1/frame_roots.csv` | 每顶层根一行：特征 + routed_c64_count | prepare_campaign.py |
| `runs/calibration-plan-native-v1/summary.json` | 结构计数（前缀/嵌套/顶层/C64 质量） | prepare_campaign.py |
| `runs/calibration-plan-native-v1/calibration_units.csv` | 每 panel 一行：arm + 入样概率 | prepare_campaign.py |
| `runs/calibration-plan-native-v1/calibration_targets.csv` | 每目标一行：6 轮 manifest（probe_id/panel/c64/target/role/round） | prepare_campaign.py |
| `runs/calibration-plan-native-v1/targets-*.txt` | 每轮已 hash 洗牌的目标地址（一行一个） | prepare_campaign.py |
| `runs/calibration-native-v1/effective_calibration_targets.csv` | denylist 过滤后的 manifest | run_campaign.py |
| `runs/calibration-native-v1/excluded_panels.csv` | 被 denylist 排除的 panel | run_campaign.py |
| `runs/calibration-native-v1/<round>/sent-targets-<round>.txt` | 本轮实际发送目标 | run_campaign.py |
| `runs/calibration-native-v1/<round>/campaign.json` | 配置快照 | run_campaign.py |
| `runs/calibration-native-v1/<round>/raw-<round>.csv` | ZMap 原始输出（15 字段） | run_scan.sh |
| `runs/calibration-native-v1/<round>/raw-<round>.csv.command.txt` | 实际 ZMap 命令 | run_scan.sh |
| `runs/calibration-native-v1/<round>/raw-<round>.csv.scanner-version.txt` | ZMap 版本 | run_scan.sh |
| `runs/calibration-native-v1/<round>/probes-<round>.csv` | 每探针一行：plan 字段 + 响应类/RTT/source/状态 | parse_results.py |
| `runs/calibration-native-v1/<round>/probes-<round>.csv.summary.json` | 本轮 matched/timeout/unmatched/多响应计数 | parse_results.py |
| `runs/calibration-native-v1/analysis/panel_labels.csv` | 每 panel 一行：search 响应类 + search 阳性 + reference_positive_m1..m5 + source 数 | analyze_campaign.py |
| `runs/calibration-native-v1/analysis/summary.json` | 六类结果 + validation | analyze_campaign.py |

正式方法另写 `runs/formal-journal-subrecon-5b-v1/<method>/`：`formal.json` 快照、完成后的 `summary.json`、`last-hop-routers.txt.gz`、按需生成的 `native-prefixes.txt.gz`，以及每批 `batch-<九位序号>.tar.gz`。压缩包包含 `targets.csv`、`target-metadata.csv`、`sent-targets.txt`、`raw-zmap.csv`、`raw-zmap.csv.command.txt`、`raw-zmap.csv.scanner-version.txt`、`probes.csv`、`probes.csv.summary.json`、`last-hop-router-observations.csv`、`scan.log` 和该批 `ledger.csv`。归档成功后删除对应 `.work/`。`analyze_formal.py` 在输出根目录写 `comparison.csv` 和 `cost-discovery-curve.csv.gz`。实际目录以 `formal.json` 的 `output_root` 为准。

## 13. 给接手者的三条落地提醒

1. `run_campaign.py` 与 `analyze_campaign.py` 只处理 D050 校准；正式调用 `run_formal.py` 与 `analyze_formal.py`。`parse_results.py` 为两条管线共用。
2. 先解决 5B 规模的内存与重放成本、批次中途意外中断的人工核对和 TNet 缺位；现有正式代码只能说明两种策略的调用路径，不能说明已完成等预算实测。
3. 遵守最小代码纪律：存在优先、复用优先、标准库优先；不为尚未发生的需求加框架或门槛。
