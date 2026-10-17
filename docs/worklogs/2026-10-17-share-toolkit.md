# 2026-10-17 容器云工具包独立发布

## 目标和起点

整理可供其他队伍使用的通用容器云工具，提交并推送到用户指定的 `scc-quattro/paratera-agentic-toolkit`。使用用户指定的 GitHub 署名。起始状态为新建的独立 Git 仓库，没有起始提交；远端查询成功且没有任何引用。

## 修改

- 独立的 Python 命令行工具：只读规格/镜像/报价查询、限额计划生成、单 GPU 容器创建和重启、租期台账、远程脚本与文件传输、关机确认。
- 使用官方 SDK 3.0 的固定下载地址与 SHA-256，保持 wheel 原样，在独立虚拟环境中安装锁定依赖。
- 付费前核对单卡规格、当前完整报价、CNY/hour 单位和预算；计划允许 5–120 分钟，不自动续费。
- 创建/启动前部署并验证独立的 systemd 用户服务。失去心跳五分钟或到期后使用平台 API 节省模式关机、保存环境，并验证异步任务与实例状态。
- 仅管理当前工作目录台账中 UUID、可用区和别名一致的实例。变更意图持久化在网络提交之前，结果不确定时不重复购买。
- 凭据只使用环境变量和忽略的 0600 配置；SDK 响应按字段白名单输出；下载归档与 SSH 诊断分离，拒绝覆盖现有证据。
- 补充中文 README、运行故障处理、agent 工作约定、占位配置、环境查询示例、MIT 许可和离线 CI。
- 仅选择通用源文件建立全新历史；没有复制任何已有仓库的 Git 对象。公开轨迹只保留本工具包相关的可公开事件，省略跨项目内容及身份认证信息，并明确标记截断和省略。

## 执行命令与验证

以下路径均相对本仓库；配置命令中的身份认证信息按约定不记录。

```bash
git init -b main
git ls-remote git@github.com:scc-quattro/paratera-agentic-toolkit.git
git remote add origin git@github.com:scc-quattro/paratera-agentic-toolkit.git
python3 tools/bootstrap.py
chmod 755 tools/paratera_askpass.py examples/hello.sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q tools tests
bash -n examples/hello.sh
.venv/bin/python -m pip check
.venv/bin/python tools/paratera_api.py region DescribeZones --check-sdk
.venv/bin/python tools/paratera_api.py ack_product DescribeACKServiceTypes --check-sdk
.venv/bin/python tools/paratera_api.py ackcs DescribeServices --check-sdk
.venv/bin/python tools/paratera_api.py ackci DescribeImages --check-sdk
.venv/bin/python tools/paratera_api.py ack_job DescribeJobs --check-sdk
.venv/bin/python .cache/publication/export_public_trace.py
git add .gitignore .github AGENTS.md LICENSE README.md requirements.lock tools tests examples docs/operations.md docs/worklogs/2026-10-17-share-toolkit.md docs/session-traces/2026-10-17-share-toolkit.jsonl
git diff --cached --check
git diff --cached --stat
git check-ignore .venv/bin/python .cache/paratera/test .paratera/credentials.json plans/task.json output/paratera/leases/test.json transfer/bundle/test
.venv/bin/python .cache/publication/sensitive_scan.py
```

本机 Python 3.12：**34 项离线测试全部通过**，编译、shell 语法和依赖检查通过。官方 wheel 实际下载与校验通过，五个生命周期相关客户端导入成功；导入检查明确 `authenticated_request_sent=false`。测试使用虚构可用区、资源编号、单价和凭据，不连接实例、不提交付费 API。

发布前暂存区共 **27 个文件**，格式检查通过，六类运行时路径均确认为忽略状态。用本机私有配置中实际敏感值和项目标识检查这 27 个暂存文件，**零命中**；审计只输出数量和路径，不输出敏感值。轨迹导出与审计辅助脚本放在忽略目录 `.cache/publication/`，不作为工具包发布内容。提交同时包含本日志和脱敏轨迹，提交哈希在提交后向用户汇报，不写入自身提交。

## 限制和下一步

- 目前是本地离线验证及官方 SDK 安装检查，未用其他队伍账号进行真实付费验收；CI 配置已加入，远端运行状态待推送后确认。
- 控制机必须持续在线且 systemd 用户服务保持运行；控制机故障或平台不可达可能延误回收，需要人工核对。
- `max_compute_cost` 是按最长租期估计的计算费用上限校验，异步关机延迟和保留存储费用另计，不保证平台总账单的硬上限。
- 不提供镜像构建、ECS 变更、多卡和集群管理；用户提供自己的镜像与任务。
- 代码、工作日志和轨迹完成后提交，推送 `main` 并验证远端 HEAD 与本地一致。后续由使用队伍在自己的授权与预算内试用。
