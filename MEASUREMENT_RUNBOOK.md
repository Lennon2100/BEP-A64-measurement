# 正式实网测量运行手册

本手册只描述当前全树正式测量。代码由 GitHub 管理；以下命令在 Linux 项目的工作目录 `/root/BEP-A64-measurement/` 执行。Windows 工作区不发包。Linux 主机当前环境和运行进度尚未在本轮核实。

## 测量范围

- 期刊 BEP 方法从 `data/interim/ris_ipv6_prefixes_unique.csv` 读取完整去重 RIS IPv6 前缀，在内存构造 BGP 父子树。
- BGP-only SubRecon 从 `data/interim/ris_ipv6_top_level_prefixes.csv` 读取顶层前缀，使用公开仓库 `thuname/subrecon` 的 `src/budget.c` 探针表。它不使用外部 Hitlist。
- TNet 从同一份完整去重 RIS 前缀构造无重叠的 BGP 覆盖 `/48` 总体。论文的完整准备步骤需要约 15B 个 `/48` 各一探针；当前 5B 总预算采用显式的受预算约束版本：均匀无放回筛选 4.5B 个 `/48`，再把 500M 留给十轮动态搜索。筛选和动态搜索都计入 TNet 的同一预算。
- 会议版 BEP 是独立策略 `bep_conference`：把同一 RIS 路由空间归一化到唯一 `/32`，固定遍历 `/32→/40→/48→/56→/64`，使用论文表 I 的 `1024/256/64/16/1` 配额和全 256 子节点展开，不使用扩刊版的动态步长、BGP bonus 或混合深度竞争。
- 四种方法使用相同的 IMC 判据：目标 `/64` 收到匹配的 Echo Reply，或 RTT 不低于 1000 ms 的 ICMPv6 Type 1 Code 3，记为一次发现。末跳路由器候选地址来自匹配目标的 Type 1 Code 3 响应源；它不触发额外探针。TNet 的候选筛选使用所有匹配 AU 源判断末跳唯一性，IMC 阳性计数仍使用统一的 1000 ms 门槛。
- 四种方法各自从零探针成本开始，预算上限各为 5B。只在本方法内部对已探测 `/64` 去重。不读取历史目标清单、历史响应或历史成本；不做校准或留出集测量。修改后的 journal 策略必须使用新的 `campaign_id` 和 `output_root`，不能续跑旧单分支策略的归档。
- journal 在预算到达前保留尚未深入的兄弟候选；只有完整递归筛完全部 `/64` 才属于候选空间耗尽。TNet 使用独立配置和输出目录，不改变已经开始的 journal/SubRecon checkpoint。

## 理论规则与工程批次

- 决策单位是**动作块** `(节点, 探针数)`，不是单探针。一个反馈轮次内，调度器只做一次前沿评分，产出一个动作块计划，然后流式生成目标；批内没有新反馈，不重复评分。
- **反馈批次 = ZMap 文件批次**。ZMap 一次进程接受一份固定目标列表、发完后再收一个 `--cooldown-time`（接收尾窗），不支持扫描中途注入新目标，所以一个 ZMap 进程就是一轮反馈。父节点的一次展开最多筛选 `2^d<=256` 个兄弟节点，并作为不可拆分的筛选动作放进一个文件批次；若本批剩余槽位不足，整组留到下一批。多个完整展开动作可以共同组成一个 `batch_size` 文件批次，批次结束后统一更新后验。
- 每个探针在**恰好一个节点内均匀抽取**。节点分别保存基础样本与自适应样本的阳性/非阳性聚合；两类目标都在被选节点内均匀，所以都可进入该节点的 Beta 似然，但不向祖先重复加入似然。子节点用祖先确定性置换游标排除已测 `/64`；父节点激活后代后不再发新目标，因此无需保存全程 `/64` 集合也能精确去重。
- 前沿是不同真实前缀长度节点的同一轮调度（混合深度），容量、HD 基础配额、先验衰减都按真实 `len(v)` 计算。
- TNet 先完成全部已配置的候选筛选反馈，再开始动态阶段。动态阶段第一轮在保留的候选 `/48` 间均匀分配并轮转其 16 个 `/52`；后续轮次取上一轮命中率最高的 4% `/48`，按论文的温度衰减 Boltzmann 权重分配 `/48` 预算，并按累计 `/52` 命中率分配区内预算。

## 时间与空间复杂度

- 每反馈轮次：前沿评分为 O(动作数 × 树深)（后验按 epoch 记忆化，一次评分只沿祖先链最多 64 跳）；每个实际 split 另用一次所属 BGP 根的前缀表扫描来给所有兄弟计算结构信号；目标流式生成是 O(批大小)。代码不为每个兄弟重复扫描同一 BGP 子树。
- 常驻内存：journal 策略侧为 O(已筛选节点)，包括节点聚合、兄弟候选前沿与确定性置换游标；解析侧为 O(单批响应目标数 + 单批动作数)。一个父节点的单次展开最多新增 256 个兄弟候选，不物化完整后代树。接管旧 `.work` 时，每个方法可能额外保留至多一个批次的有序 `/64` 恢复排除索引。
- **仍随总探针数增长的内容**包括必须保留的逐批磁盘证据，以及随已筛选空间增长的节点级紧凑状态；不保存 Python 全程 `/64` 去重集合或 `probed-*.bin`。4 GiB 主机必须依据新运行的 RSS 日志评估，不能沿用旧单链策略的内存结论。
- TNet 不为每个已筛选 `/48` 建 Python 节点对象；筛选期只保存收到 AU 的区域/路由器关系，动态期用紧凑整数数组保存候选 `/48`、16 个 `/52` 的计数和无重复游标。其状态为 O(有 AU 响应的筛选区域 + 候选 `/48` × 16)，仍需用首批真实响应率核对 4 GiB 主机峰值。

## 输入与配置

配置模板是 `formal.example.json`。将其复制成 `formal.json`，按 Linux 主机实际情况设置网络参数与新的 `output_root`。配置相对路径从 `formal.json` 所在目录解析。不要沿用包含旧规则归档的输出目录；续跑必须使用与本次运行内容相同的配置。

| 配置项 | 当前模板值或用途 |
|---|---|
| `input.journal_prefix_csv` | `data/interim/ris_ipv6_prefixes_unique.csv` |
| `input.subrecon_prefix_csv` | `data/interim/ris_ipv6_top_level_prefixes.csv` |
| `input.tnet_prefix_csv` | `data/interim/ris_ipv6_prefixes_unique.csv`；位于独立 `tnet.example.json` |
| `input.bep_conference_prefix_csv` | `data/interim/ris_ipv6_prefixes_unique.csv`；位于独立 `bep_conference.example.json` |
| `input.frame_exclusions` | `config/frame_exclusions.txt`，排除 `2002::/16` |
| `budget_total_per_method` | `5000000000`，每方法独立上限 |
| `batch_size` | `1000000`，一个反馈轮次/ZMap 进程的探针数 |
| `slow_au_threshold_ms` | `1000` |
| `scanner.zmap_binary` | `.build/zmap-build/src/zmap` |
| `scanner.source_ipv6` | 模板为 `2607:8700:5500:3959::2`，运行前按主机实际值确认 |
| `scanner.interface` | 模板为 `ipv6net` |
| `scanner.gateway_mac` | 模板为 `-`，SIT/NOARP 接口使用 ZMap `--iplayer` |
| `scanner.rate_pps` | 模板为 `1000`，运行前按实际允许速率确认 |
| `scanner.cooldown_seconds` | `30`，ZMap 接收尾窗，需覆盖慢 AU 的 RTT（观测最大约 25 s） |
| `strategies.journal.theta_b` | `16`，采用会议论文 HD-Ratio 的原始全局尺度 |
| `strategies.journal.root_budget_fraction` | 模板为 `0.5`；启动时自动核对它能否容纳全部根基础配额，不足则在发包前报出所需最小比例；若总预算本身不足则直接报出所需根基础探针数 |
| `strategies.tnet.candidate_screen_budget` | `4500000000`；属于 TNet 的 5B 总预算，余下 500M 进入动态搜索 |
| `strategies.tnet.rounds` | `10`，论文的动态反馈轮数 |
| `strategies.tnet.top_k_ratio` | `0.04`，论文默认值 |
| `strategies.tnet.initial_temperature` | `1.0`；论文只给出 `T=α/t` 而未报告 α，本实现将 α 显式配置 |
| `strategies.bep_conference.prior_decay` | `0.5`，会议论文父先验强度衰减 |
| `strategies.bep_conference.credible_z` | `1.96`，会议论文 95% 下界 |
| `strategies.bep_conference.expansion_cost` | `1.0`；论文定义 `c` 但未单独报告数值，本实现显式采用归一化单位成本 |
| `output_root` | `runs/formal-journal-subrecon-d070-v2`，首次运行应为空 |

**可行性**：1000 pps 下每探针 1 ms，5B 探针纯发送约 57.9 天，与批次和冷却无关；30 s 冷却只是每轮固定的接收尾窗。1000 pps、100 万探针/批、30 s 尾窗时，每方法 5B 的理论纯发送与尾窗约 59.6 天，仍未计计算、解析和停机。5B 是配置上限而非已证可执行规模；继续长期运行前要用已完成批次的真实压缩率核对剩余存储和迁出能力。若沿用 180M 实际上限，`theta_B=16` 很可能要求把 `root_budget_fraction` 调高；以启动时报出的精确最小比例为准，不能让代码静默丢弃顶层根。

正式运行不读取 scan 排除表，也不要求 RIS 快照身份。Linux 主机需有上述两份 RIS CSV、frame 排除文件和已构建的 ZMap。扫描包装脚本向 ZMap 传入 `-i`、`--ipv6-source-ip`，并为此 fork 的 IPv4 初始化传入 `-S 0.0.0.0`；实际 IPv6 探针使用配置中的源 IPv6。ZMap 构建及扫描包装参数见 [README.md](README.md)。

TNet 论文筛选全部约 15B `/48`，超出当前单方法 5B 上限，因此这里不能称为原论文完整候选集复现。`tnet.example.json` 的 4.5B/0.5B 划分保留论文 500M 动态搜索规模，并将可用的其余预算用于均匀候选筛选；离线合并 BGP `/48` 区间不发包，不消耗探针预算。

## 启动、暂停、续跑

以下运行命令会实际向公网发包。当前 SubRecon 保留旧策略运行的独立 checkpoint；D070 journal 必须新建配置和输出目录。先把服务器上原始 `formal.json` 原样保存为 `formal-v1.json`，再创建新配置。若既有 SubRecon 的正式上限实际是 180,000,000，新 journal 也应在 `formal-v2.json` 使用 180,000,000 才能直接做等预算比较；模板中的 5B 只是此前锁定的上限模板。

```bash
cd /root/BEP-A64-measurement
cp formal.json formal-v1.json
cp formal.example.json formal-v2.json
# 编辑 formal-v2.json：确认与 SubRecon 相同的预算，以及源地址、接口、速率和 v2 输出目录
sudo python3 scripts/run_formal.py formal-v2.json journal
# SubRecon 继续使用当初记录的完整 v1 配置
sudo python3 scripts/run_formal.py formal-v1.json subrecon --resume
```

TNet 使用独立的新目录启动，避免改变上述两个方法已经记录在各自 `formal.json` 中的配置：

```bash
cp tnet.example.json tnet.json
# 编辑 tnet.json 中的源地址、接口、速率与输出目录
sudo python3 scripts/run_formal.py tnet.json tnet
```

会议版 BEP 同样使用独立配置和目录：

```bash
cp bep_conference.example.json bep-conference.json
# 编辑网络参数与输出目录
sudo python3 scripts/run_formal.py bep-conference.json bep_conference
```

`analyze_formal.py` 只在两个预定结果均生成 `summary.json` 后运行。当前两种结果位于不同 output root；完成后可在 v2 根下建立指向 v1 `subrecon` 目录的符号链接，再使用 `formal-v2.json` 分析，无需复制历史归档：

```bash
ln -s ../formal-journal-subrecon-5b-v1/subrecon \
  runs/formal-journal-subrecon-d070-v2/subrecon
python3 scripts/analyze_formal.py formal-v2.json
```

此前缺少 `summary.json` 是方法未完成的后续现象；分析器不绕过这个条件。

同一目标可能收到多条甚至跨类别响应。正式 parser 保留完整 raw CSV，每个目标仍只计一次预算；只要任一匹配响应满足 IMC 规则，该 `/64` 就记为阳性。紧凑 `probes.csv` 选择第一条阳性到达（没有阳性时选择第一条到达），所有 AU 响应源均写入末跳证据，summary 记录多响应与跨类别目标数。

需要暂停时，对 runner 发送一次 SIGINT（Ctrl+C）或 SIGTERM，等待当前批次扫描、解析、压缩完成并输出 `status: paused`。随后分别使用各自原配置续跑：

```bash
sudo python3 scripts/run_formal.py formal-v2.json journal --resume
sudo python3 scripts/run_formal.py formal-v1.json subrecon --resume
sudo python3 scripts/run_formal.py tnet.json tnet --resume
sudo python3 scripts/run_formal.py bep-conference.json bep_conference --resume
```

若 ZMap 已完成但 parser 失败，保留对应的下一批 `.work`。旧 `.work` 没有生成后 checkpoint 时，`--resume` 以其已发送 manifest 为事实，登记该批 `/64` 为恢复排除项，应用已有 raw 反馈并归档，不再次调用 ZMap。新批次在发包前写 `prepared-state.pkl.gz`，以后同类失败可直接恢复生成后状态。若目录只有 `manifest.csv` 和 `sent-targets.txt`、完全没有扫描产物，说明中断发生在发包前；`--resume` 会删除并重新生成这一批。

续跑兼容读取旧归档的 `state.json`，新归档使用流式写入的 `state.pkl.gz`（紧凑节点聚合、前沿状态与置换游标），**不重放历史逐探针记录**。运行状态为 O(已筛选节点)，写检查点不复制完整节点图或构造完整 JSON 字符串。批后检查点先写入批目录、再随批归档，归档是唯一原子提交点。

每个完成批次保存为 `batch-000000001.tar.gz` 等压缩包，内含唯一且保持发送顺序的 `manifest.csv`、实际命令、扫描器版本、原始 ZMap CSV、紧凑逐探针证据、节点级反馈、末跳源地址证据、批后 `state.pkl.gz` 和日志。临时 `sent-targets.txt` 与 manifest 的 `target_ipv6` 列重复，扫描结束后即删除。压缩包写完并读回后，runner 删除对应 `.work/`；压缩期间磁盘要同时容纳该批原文件和压缩包。有扫描产物但证据不完整的 `.work/` 仍要求人工核对，避免重发。

## 输出与比较

每方法完成后写 `summary.json`、`last-hop-routers.txt.gz` 和按需生成的 `native-prefixes.txt.gz`。末跳列表记录去重后的候选接口地址，不是经过独立核验的路由器设备数。分析器从各批 `probes.csv` 派生 `comparison.csv` 与 `cost-discovery-curve.csv.gz`，不再存逐探针台账文件。`formal_sent` 与 `distinct_new_positive_c64` 统计本次成功完成扫描批次中的目标（每个 `/64` 最多一发，故阳性探针数即不同阳性 `/64` 数），不包含任何历史探针。

## 磁盘增长估算（每批 B 个探针、全程 T 个探针）

- 工作目录单批仍由 `manifest.csv`、`probes.csv` 与原始 ZMap CSV 主导；按当前短字段预计未压缩约 0.3–0.6 KB/探针，压缩比必须以首个真实批次实测，不能把估算当容量保证。
- 全程磁盘是 O(T) 的原始证据归档；`state.pkl.gz` 为 O(已筛选节点)。若压缩后为 0.05–0.15 KB/探针，T=10^8 约 5–15 GB，T=5×10^9 约 250–750 GB；正式 5B 前必须按真实首批压缩率确认外置存储和迁出流程。

## 旧运行目录的安全处理

旧 `v1` journal 已经产生正式归档和 `summary.json`，应保留为旧策略观测证据，不要删除、改名，也不要用 D070 代码对它执行 `--resume`。新策略直接使用 `v2` 输出目录；两者路径不同，不需要处理旧目录：

```bash
cd /root/BEP-A64-measurement
ls -la runs/formal-journal-subrecon-5b-v1/journal
ls -la runs/formal-journal-subrecon-d070-v2
```

只要出现了任何 `batch-*.work`、`batch-*.tar.gz` 或 `batch-*.tar.gz.part`，就说明已有（或可能已有）发包，必须保留证据并核对实际发送情况。

当前代码与文档只说明运行方式；本轮没有启动公网扫描，也没有运行模拟反馈。
