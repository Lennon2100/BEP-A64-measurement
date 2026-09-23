# 正式实网测量运行手册

本手册只描述当前全树正式测量。代码由 GitHub 管理；以下命令在 Linux 项目的工作目录 `/root/BEP-A64-measurement/` 执行。Windows 工作区不发包。Linux 主机当前环境和运行进度尚未在本轮核实。

## 测量范围

- 期刊 BEP 方法从 `data/interim/ris_ipv6_prefixes_unique.csv` 读取完整去重 RIS IPv6 前缀，在内存构造 BGP 父子树。
- BGP-only SubRecon 从 `data/interim/ris_ipv6_top_level_prefixes.csv` 读取顶层前缀，使用公开仓库 `thuname/subrecon` 的 `src/budget.c` 探针表。它不使用外部 Hitlist。
- 两种方法使用相同的 IMC 判据：目标 `/64` 收到匹配的 Echo Reply，或 RTT 不低于 1000 ms 的 ICMPv6 Type 1 Code 3，记为一次发现。末跳路由器候选地址来自匹配目标的 Type 1 Code 3 响应源；它不触发额外探针。
- 两种方法各自从零探针成本开始，预算上限各为 5B。只在本方法内部对已探测 `/64` 去重。不读取历史目标清单、历史响应或历史成本；不做校准或留出集测量。
- 当前正式配置只启用 journal 和 SubRecon。TNet 暂缓；后续加入 `strategies/tnet.py` 与配置项即可接入。两种方法的预算是上限，候选耗尽时可能提前结束。

## 理论规则与工程批次

- 决策单位是**动作块** `(节点, 探针数)`，不是单探针。一个反馈轮次内，调度器只做一次前沿评分，产出一个动作块计划，然后流式生成目标；批内没有新反馈，不重复评分。
- **反馈批次 = ZMap 文件批次**。ZMap 一次进程接受一份固定目标列表、发完后再收一个 `--cooldown-time`（接收尾窗），不支持扫描中途注入新目标，所以一个 ZMap 进程就是一轮反馈。策略的“动作粒度”（`action_block`）与“反馈粒度”（`batch_size`）是两回事：前者决定目标在批内的空间投放，后者决定多久更新一次后验。
- 每个探针在**恰好一个节点内均匀抽取**。节点分别保存基础样本与自适应样本的阳性/非阳性聚合；两类目标都在被选节点内均匀，所以都可进入该节点的 Beta 似然，但不向祖先重复加入似然。子节点用祖先确定性置换游标排除已测 `/64`；父节点激活后代后不再发新目标，因此无需保存全程 `/64` 集合也能精确去重。
- 前沿是不同真实前缀长度节点的同一轮调度（混合深度），容量、HD 基础配额、先验衰减都按真实 `len(v)` 计算。

## 时间与空间复杂度

- 每反馈轮次：决策 O(动作数 × 树深)（后验按 epoch 记忆化，一次评分只沿祖先链最多 64 跳）+ 目标流式生成 O(批大小)。
- 常驻内存：策略侧 O(活跃节点)，包括节点聚合、前沿堆与确定性置换游标；解析侧 O(单批响应数 + 单批动作数)，不随历史累计探针数增长。
- **仍随总探针数增长的内容**只有必须保留的逐批磁盘证据归档。恢复状态、节点聚合和游标是 O(活跃节点)，不再保存 Python 全程去重集合或 `probed-*.bin`。

## 输入与配置

配置模板是 `formal.example.json`。将其复制成 `formal.json`，按 Linux 主机实际情况设置网络参数与新的 `output_root`。配置相对路径从 `formal.json` 所在目录解析。不要沿用包含旧规则归档的输出目录；续跑必须使用与本次运行内容相同的配置。

| 配置项 | 当前模板值或用途 |
|---|---|
| `input.journal_prefix_csv` | `data/interim/ris_ipv6_prefixes_unique.csv` |
| `input.subrecon_prefix_csv` | `data/interim/ris_ipv6_top_level_prefixes.csv` |
| `input.frame_exclusions` | `config/frame_exclusions.txt`，排除 `2002::/16` |
| `budget_total_per_method` | `5000000000`，每方法独立上限 |
| `batch_size` | `1000000`，一个反馈轮次/ZMap 进程的探针数 |
| `strategies.journal.action_block` | `8192`，每个动态动作块的探针数 |
| `slow_au_threshold_ms` | `1000` |
| `scanner.zmap_binary` | `.build/zmap-build/src/zmap` |
| `scanner.source_ipv6` | 模板为 `2607:8700:5500:3959::2`，运行前按主机实际值确认 |
| `scanner.interface` | 模板为 `ipv6net` |
| `scanner.gateway_mac` | 模板为 `-`，SIT/NOARP 接口使用 ZMap `--iplayer` |
| `scanner.rate_pps` | 模板为 `1000`，运行前按实际允许速率确认 |
| `scanner.cooldown_seconds` | `30`，ZMap 接收尾窗，需覆盖慢 AU 的 RTT（观测最大约 25 s） |
| `strategies.journal.theta_b` | `1`；其余搜索系数见模板 |
| `output_root` | `runs/formal-journal-subrecon-5b-v1`，首次运行应为空 |

**可行性**：1000 pps 下每探针 1 ms，5B 探针纯发送约 57.9 天，与批次和冷却无关；30 s 冷却只是每轮固定的接收尾窗。1000 pps、100 万探针/批、30 s 尾窗时，每方法 5B 的理论纯发送与尾窗约 59.6 天，仍未计计算、解析和停机。5B 是配置上限而非已证可执行规模；继续长期运行前要用已完成批次的真实压缩率核对剩余存储和迁出能力。

正式运行不读取 scan 排除表，也不要求 RIS 快照身份。Linux 主机需有上述两份 RIS CSV、frame 排除文件和已构建的 ZMap。扫描包装脚本向 ZMap 传入 `-i`、`--ipv6-source-ip`，并为此 fork 的 IPv4 初始化传入 `-S 0.0.0.0`；实际 IPv6 探针使用配置中的源 IPv6。ZMap 构建及扫描包装参数见 [README.md](README.md)。

## 启动、暂停、续跑

以下前两个命令会实际向公网发包。先确认 `formal.json` 和测量主机状态，再分别运行方法；两种方法有独立输出目录。

```bash
cd /root/BEP-A64-measurement
cp formal.example.json formal.json
# 编辑 formal.json：源地址、接口、速率、输出目录
sudo python3 scripts/run_formal.py formal.json journal
sudo python3 scripts/run_formal.py formal.json subrecon
python3 scripts/analyze_formal.py formal.json
```

`analyze_formal.py` 只在配置中的方法均生成 `summary.json` 后运行。此前缺少该文件，是正式方法未完成的后续现象；分析器不绕过这个条件。

同一目标可能收到多条甚至跨类别响应。正式 parser 保留完整 raw CSV，每个目标仍只计一次预算；只要任一匹配响应满足 IMC 规则，该 `/64` 就记为阳性。紧凑 `probes.csv` 选择第一条阳性到达（没有阳性时选择第一条到达），所有 AU 响应源均写入末跳证据，summary 记录多响应与跨类别目标数。

需要暂停时，对 runner 发送一次 SIGINT（Ctrl+C）或 SIGTERM，等待当前批次扫描、解析、压缩完成并输出 `status: paused`。随后按方法续跑：

```bash
sudo python3 scripts/run_formal.py formal.json journal --resume
sudo python3 scripts/run_formal.py formal.json subrecon --resume
```

若 ZMap 已完成但 parser 失败，保留对应的下一批 `.work`。`--resume` 会从最后一个完整归档恢复，确定性重生成并逐行比较该 `.work/manifest.csv`，然后只解析已有 `raw-zmap.csv`、写 checkpoint 并压缩归档；该恢复路径不会再次调用 ZMap。当前已知恢复对象是 journal 的 `batch-000000004.work` 和 SubRecon 的 `batch-000000061.work`。若 manifest 与恢复状态不一致，runner 会停止，不能绕过后重发。

续跑直接读取最新完成归档内的 `state.json`（紧凑节点聚合、前沿状态与置换游标），**不重放历史逐探针记录**；启动成本和常驻策略状态均为 O(活跃节点)。批后 `state.json` 先写入批目录、再随批归档，归档是唯一原子提交点；归档完成前中断会遗留 `.work` 或 `.tar.gz.part`，阻止自动续跑。

每个完成批次保存为 `batch-000000001.tar.gz` 等压缩包，内含唯一且保持发送顺序的 `manifest.csv`、实际命令、扫描器版本、原始 ZMap CSV、紧凑逐探针证据、节点级反馈、末跳源地址证据、批后 `state.json` 和日志。临时 `sent-targets.txt` 与 manifest 的 `target_ipv6` 列重复，扫描结束后即删除。压缩包写完并读回后，runner 删除对应 `.work/`；压缩期间磁盘要同时容纳该批原文件和压缩包。若进程在批次中途被强制终止，遗留的 `.work/` 或 `.tar.gz.part` 会阻止自动续跑，因为该批可能已有探针发出；必须先人工核对实际发包情况。

## 输出与比较

每方法完成后写 `summary.json`、`last-hop-routers.txt.gz` 和按需生成的 `native-prefixes.txt.gz`。末跳列表记录去重后的候选接口地址，不是经过独立核验的路由器设备数。分析器从各批 `probes.csv` 派生 `comparison.csv` 与 `cost-discovery-curve.csv.gz`，不再存逐探针台账文件。`formal_sent` 与 `distinct_new_positive_c64` 统计本次成功完成扫描批次中的目标（每个 `/64` 最多一发，故阳性探针数即不同阳性 `/64` 数），不包含任何历史探针。

## 磁盘增长估算（每批 B 个探针、全程 T 个探针）

- 工作目录单批仍由 `manifest.csv`、`probes.csv` 与原始 ZMap CSV 主导；按当前短字段预计未压缩约 0.3–0.6 KB/探针，压缩比必须以首个真实批次实测，不能把估算当容量保证。
- 全程磁盘是 O(T) 的原始证据归档；`state.json` 为 O(活跃节点)。若压缩后为 0.05–0.15 KB/探针，T=10^8 约 5–15 GB，T=5×10^9 约 250–750 GB；正式 5B 前必须按真实首批压缩率确认外置存储和迁出流程。

## 旧运行目录的安全处理

若某方法输出目录只有 `formal.json`（没有任何 `batch-*` 归档），且已由进程状态和日志确认 ZMap 未启动，则可安全删除或改名后重跑。处理前先确认：

```bash
cd /root/BEP-A64-measurement
ls -la runs/formal-journal-subrecon-5b-v1/journal
# 仅出现 formal.json（可能还有空目录）才继续
rm -rf runs/formal-journal-subrecon-5b-v1/journal
```

只要出现了任何 `batch-*.work`、`batch-*.tar.gz` 或 `batch-*.tar.gz.part`，就说明已有（或可能已有）发包，必须人工核对实际发送情况，不要盲删。用户报告的旧 journal 目录只有 `formal.json` 且 ZMap 未启动；确认现状仍一致后删除该目录，再用新代码和新配置启动。

当前代码与文档只说明运行方式；本轮没有启动公网扫描，也没有运行模拟反馈。
