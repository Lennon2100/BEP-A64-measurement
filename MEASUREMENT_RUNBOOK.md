# Measurement Runbook — 实网测量交接文档

本文档是给接手"正式实网测量"的实现者（人或 agent）的交接说明。它把当前已完成的校准管线、服务器环境、ZMap 参数、数据契约、以及**当前（2026-09-22）研究方向和下一步要写什么**一次性讲清楚，使接手者不需要再翻代码就能动手。

开始实现前，先按顺序读共享记忆（`project_memory/` 下）：`AGENTS.md` → `PROJECT_CONTEXT.md` → `DECISIONS.md` → `PROGRESS.md` → `TODO.md`。本 runbook 是这些记忆的"测量工程化"摘录，不替代它们。

---

## 0. 一句话现状

**已完成**：从 BGP 输入到逐轮解析的整条校准管线（prepare → run → parse → analyze），并在服务器上跑完了 D050 六轮校准（149,652 个探针，24,942 个 panel）。

**待办**：正式的**全树等总预算三方对比**（journal 方法 vs 论文版 TNet vs ICNP 会议版 SubRecon）还**没有实现**。现有 `campaign.json` / `analyze_campaign.py` 仍是"校准专用"，不是三方动态搜索的正式 runner。

---

## 1. 科学框架（这次测量到底在测什么）

- **搜索单元 = IPv6 `/64`**。`C64`（候选 `/64`）、`A64`（操作性阳性 `/64`）只是实现缩写，**不是论文贡献**。
- **操作性阳性规则（直接采用 *Destination Reachable* 的 IMC 规则，D058）**：对某个目标 `/64` 发一个随机 IID 的 ICMPv6 Echo Request，若收到
  - 正确匹配的 **Echo Reply**（Type 129），或
  - 正确匹配的 **ICMPv6 Destination Unreachable Type 1 Code 3**，且 **RTT ≥ 1000 ms**，
  则记该 `/64` 为一次"活跃子网发现（active-subnet discovery）"。其它结果（含 timeout）都不是发现。
- **正式任务**：在**固定总探针预算**下，最大化发现的**不同 IMC 阳性 `/64` 数量**。
- **对比对象（D066/D068）**：journal 方法、**论文版 TNet**（本地重实现，`/48` 候选筛选计入其预算）、**ICNP 会议版 SubRecon 的 BGP-only 策略改编**（无外部 Hitlist / 活跃种子 / 末跳路由器种子）。PaS、原始 BEP、random **不是**必跑基线。
- **三方法共同起点（D068）**：同一份冻结的去重 RIS BGP 前缀帧 + 相同的 scan 排除 + 相同的 IMC 响应规则。从 BGP 建前缀树是离线计算，**不花探针**。
- **D050 的 149,652 探针归属**：是本项目方法自己的**历史校准/设计成本**，只记到我们这边，**不共享给 TNet/SubRecon，也不用其响应初始化正式搜索**。
- **明确的非目标（D058-D061）**：不做 terminal audit、不做 missed-mass/Horvitz-Thompson/Neyman 估计、不做 parent-mass/完整性理论证明、不做 held-out 评价、不做 action trace、不做 soft-pruning 对比、不做 response-homogeneous block。这些都不是前置门槛。

---

## 2. 服务器环境与关键路径

服务器：`root@meta-codes-1`，工作根目录 `/root/BEP-A64-measurement/`（下文相对路径都以它为根）。

| 对象 | 路径 |
|---|---|
| BGP 前缀输入（三列 CSV） | `data/interim/ris_ipv6_prefixes_unique.csv` |
| frame 排除（6to4） | `config/frame_exclusions.txt`（内容 `2002::/16`） |
| scan 排除（denylist） | `config/scan_exclusions.txt`（当前为空，仅注释） |
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

### 4.2 `run_campaign.py` — 正式薄 runner（校准专用，已完成）

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

### 待办（正式三方法对比，**尚未实现**）

1. **journal 方法**（D061-D063）：保留 BEP 前缀树 + parent 级 Beta–Bernoulli 更新 + parent-to-child prior；HD-Ratio base 配额 `k_base = min(2^(64-ell), max(1, ceil(theta_B * 2^((56-ell)/4))))`，`/64` 配额 1；`theta_B` 从正式预算一次性选定；base 之后按 expected discovery + 信息增益加探针；混合深度 frontier 调度；跨层 `/64` 探针去重（已探过的 `/64` 不再作为后续目标）。
2. **TNet**：无开源实现，本地按论文重实现，`/48` 候选筛选计入其预算。
3. **ICNP 会议 SubRecon BGP-only 改编**：无外部 Hitlist/活跃种子/末跳路由器种子，只从 BGP 前缀出发，扩展仅用其自己已付费发现的地址/路由器。
4. **正式配置**：冻结各 collector RIS 快照身份、scan 排除、有限速率、每方法严格总预算 `B_total` 与阶段分配。
5. **成本台账**：D050 149,652 探针只记到本方法；TNet 的 `/48` 筛选记到 TNet；SubRecon 的所有探针记到 SubRecon；只统计新付费阶段发现的 IMC 阳性 `/64`，D050 历史阳性单独披露、不算新发现。

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

## 10. 给接手者的三条落地提醒

1. 现有 `prepare_campaign.py`/`run_campaign.py`/`parse_results.py`/`analyze_campaign.py` 是**校准管线**，是正式 runner 的工程模板，但**不能直接当三方法动态搜索 runner 用**（PROGRESS.md 已明示）。
2. 正式 runner 需要**每方法独立搜索状态 + 每阶段实际发包台账 + IMC 阳性 `/64` 去重 + 累计成本/发现曲线**；从共同 BGP 前缀帧出发，不继承 D050 响应标签。
3. 遵守最小代码纪律：存在优先、复用优先、标准库优先，不建框架/数据库/任务队列/配置 schema；每个脚本一个职责。
