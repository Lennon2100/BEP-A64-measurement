# 正式实网测量运行手册

本手册只描述当前全树正式测量。代码由 GitHub 管理；以下命令在 Linux 项目的工作目录 `/root/BEP-A64-measurement/` 执行。Windows 工作区不发包。Linux 主机当前环境和运行进度尚未在本轮核实。

## 测量范围

- 期刊 BEP 方法从 `data/interim/ris_ipv6_prefixes_unique.csv` 读取完整去重 RIS IPv6 前缀，在内存构造 BGP 父子树。
- BGP-only SubRecon 从 `data/interim/ris_ipv6_top_level_prefixes.csv` 读取顶层前缀，使用公开仓库 `thuname/subrecon` 的 `src/budget.c` 探针表。它不使用外部 Hitlist。
- 两种方法使用相同的 IMC 判据：目标 `/64` 收到匹配的 Echo Reply，或 RTT 不低于 1000 ms 的 ICMPv6 Type 1 Code 3，记为一次发现。末跳路由器候选地址来自匹配目标的 Type 1 Code 3 响应源；它不触发额外探针。
- 两种方法各自从零探针成本开始，预算上限各为 5B。只在本方法内部对已探测 `/64` 去重。不读取历史目标清单、历史响应或历史成本；不做校准或留出集测量。
- 当前正式配置只启用 journal 和 SubRecon。TNet 暂缓；后续加入 `strategies/tnet.py` 与配置项即可接入。两种方法的预算是上限，候选耗尽时可能提前结束。

## 输入与配置

配置模板是 `formal.example.json`。将其复制成 `formal.json`，按 Linux 主机实际情况设置网络参数与新的 `output_root`。配置相对路径从 `formal.json` 所在目录解析。不要沿用包含旧规则归档的输出目录；续跑必须使用与本次运行内容相同的配置。

| 配置项 | 当前模板值或用途 |
|---|---|
| `input.journal_prefix_csv` | `data/interim/ris_ipv6_prefixes_unique.csv` |
| `input.subrecon_prefix_csv` | `data/interim/ris_ipv6_top_level_prefixes.csv` |
| `input.frame_exclusions` | `config/frame_exclusions.txt`，排除 `2002::/16` |
| `budget_total_per_method` | `5000000000`，每方法独立上限 |
| `batch_size` | `8192` |
| `slow_au_threshold_ms` | `1000` |
| `scanner.zmap_binary` | `.build/zmap-build/src/zmap` |
| `scanner.source_ipv6` | 模板为 `2607:8700:5500:3959::2`，运行前按主机实际值确认 |
| `scanner.interface` | 模板为 `ipv6net` |
| `scanner.gateway_mac` | 模板为 `-`，SIT/NOARP 接口使用 ZMap `--iplayer` |
| `scanner.rate_pps` | 模板为 `1000`，运行前按实际允许速率确认 |
| `scanner.cooldown_seconds` | `30` |
| `strategies.journal.theta_b` | `1`；其余搜索系数见模板 |
| `output_root` | `runs/formal-journal-subrecon-5b-v1`，首次运行应为空 |

正式运行不读取 scan 排除表，也不要求 RIS 快照身份。Linux 主机需有上述两份 RIS CSV、frame 排除文件和已构建的 ZMap。ZMap 构建及扫描包装参数见 [README.md](README.md)。模板中的 1000 pps、8192 探针一批、每批 30 秒冷却意味着每方法 5B 探针仅发送与冷却的理论时间约 270 天；还未计计算、解析和停机时间。

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

需要暂停时，对 runner 发送一次 SIGINT（Ctrl+C）或 SIGTERM，等待当前批次扫描、解析、压缩完成并输出 `status: paused`。随后按方法续跑：

```bash
sudo python3 scripts/run_formal.py formal.json journal --resume
sudo python3 scripts/run_formal.py formal.json subrecon --resume
```

每个完成批次保存为 `batch-000000001.tar.gz` 等压缩包，包含目标清单、实际命令、扫描器版本、原始 ZMap CSV、逐探针解析 CSV、末跳源地址证据、该批 `ledger.csv` 和日志。压缩包写完并读回后，runner 删除对应 `.work/`；压缩期间磁盘要同时容纳该批原文件和压缩包。若进程在批次中途被强制终止，遗留的 `.work/` 或 `.tar.gz.part` 会阻止自动续跑，因为该批可能已有探针发出；必须先人工核对实际发包情况。

续跑从已归档批次的真实解析结果重建策略状态，不重发这些批次。重建时间和内存占用随已完成探针数增长；目前尚未证明 5B 规模能在目标主机上完成。

## 输出与比较

每方法完成后写 `summary.json`、`last-hop-routers.txt.gz` 和按需生成的 `native-prefixes.txt.gz`。末跳列表记录去重后的候选接口地址，不是经过独立核验的路由器设备数。分析器读取各批压缩台账，在输出根目录写 `comparison.csv` 与 `cost-discovery-curve.csv.gz`。成本列 `probe_cost` 及 `formal_sent` 统计本次成功完成扫描批次中的目标，不包含任何历史探针。

当前代码与文档只说明运行方式；本轮没有启动公网扫描，也没有运行模拟反馈。
