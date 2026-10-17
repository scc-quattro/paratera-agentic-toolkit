# Paratera Agentic Toolkit

面向比赛队伍和本地 agent 的并行智算云工具包：查询规格与报价、生成限额计划、申请一台单 GPU 容器、执行任务、取回文件，并通过独立看门狗关机。各队使用自己的账号、镜像和任务脚本。

Python 3.11+、Linux、OpenSSH、tar 和 systemd 用户服务是运行条件。离线测试不需要云账号。付费操作需要用户事先授权；阅读本仓库不会授予 agent 购买算力的权限。

## 安装

```bash
git clone git@github.com:scc-quattro/paratera-agentic-toolkit.git
cd paratera-agentic-toolkit
python3 tools/bootstrap.py
.venv/bin/python -m unittest discover -s tests -v
```

安装程序建立仓库专属 `.venv`，安装 `requirements.lock` 中的依赖，并从[官方 SDK 下载入口](https://ai.paratera.com/document/computer/api/sdk)使用的厂商地址下载 SDK 3.0。wheel 的 SHA-256 固定为 `f8070db632ce0729f802d86e96962976fb05437389bee04442cfb3d8dc18f168`，每次加载前复核；SDK 不随 Git 分发。厂商更新文件后，需要人工核验新版本，再更新校验值。

控制机应持续在线，独立于申请的容器。在开始付费任务前，由控制机管理员启用当前用户的 lingering 服务：

```bash
loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

程序要求 `Linger=yes`，并且在付费请求前确认 systemd 看门狗已完成一次真实、限定范围的 API 查询。控制机断电、网络长期中断或平台 API 故障时，本机看门狗无法保证准时关机，须到平台控制台核对。参见[故障恢复](docs/operations.md)。

## 配置自己的密钥

需要平台 **AccessKey 和 SecretAccessKey 两项**。可在当前 shell 中安全输入：

```bash
read -r -s -p 'AccessKey: ' PARATERA_ACCESS_KEY; echo
read -r -s -p 'SecretAccessKey: ' PARATERA_SECRET_ACCESS_KEY; echo
export PARATERA_ACCESS_KEY PARATERA_SECRET_ACCESS_KEY
```

也可复制模板，在本机编辑，不把实际值写进命令历史：

```bash
install -d -m 700 .paratera
install -m 600 examples/credentials.example.json .paratera/credentials.json
${EDITOR:-vi} .paratera/credentials.json
```

环境变量优先。为让 systemd 服务在 agent 退出后继续工作，创建/启动命令会将当前凭据保存到忽略目录 `.paratera/credentials.json`，权限为 `0600`。SSH 密码同样只存在本机私有配置中，不放入命令参数或 API 日志。不要上传 `.paratera`、输出目录或完整 API 原始响应。

## 查询并生成计划

以下命令均为只读，不申请机器：

```bash
.venv/bin/python tools/paratera_api.py region DescribeZones
.venv/bin/python tools/paratera_api.py ack_product DescribeACKServiceTypes \
  --output output/catalog.json --summary
.venv/bin/python tools/paratera_api.py ack_product DescribeACKPublicImages \
  --output output/public-images.json --summary
.venv/bin/python tools/paratera_api.py ackci DescribeImages \
  --output output/private-images.json --summary
```

`paratera_api.py` 按字段白名单输出，剔除登录密码、签名及账户余额等字段。先查看当前规格、镜像和完整配置；需要比较其他产品时，可用只读的 `ecs InquiryPriceRunInstances` 查询，不支持 ECS 的申请和关机。需要额外查询参数时，把 JSON 保存在 `plans/` 下，再传 `--payload-file`。

确认当前[容器计价说明](https://ai.paratera.com/document/container)及[计量计费](https://ai.paratera.com/document/billing)中的币种、单位和存储费用。在本机设置下列变量，值须来自实时目录、自己的镜像和用户批准的预算：

```bash
export CLOUD_ZONE='REPLACE_FROM_CATALOG'
export CLOUD_MODEL='REPLACE_WITH_FULL_SINGLE_GPU_MODEL'
export CLOUD_IMAGE='REPLACE_WITH_YOUR_IMAGE_UUID'
export CLOUD_MINUTES='30'
export CLOUD_COMPUTE_BUDGET='REPLACE_WITH_APPROVED_CNY_AMOUNT'
.venv/bin/python tools/make_plan.py \
  --zone "$CLOUD_ZONE" --model "$CLOUD_MODEL" --image "$CLOUD_IMAGE" \
  --minutes "$CLOUD_MINUTES" --max-compute-cost "$CLOUD_COMPUTE_BUDGET" \
  --verified-cny-per-hour \
  --pricing-source https://ai.paratera.com/document/container \
  --pricing-source https://ai.paratera.com/document/billing \
  --output plans/task.json
```

`--verified-cny-per-hour` 是操作者对官方币种和计费单位的确认，URL 本身不构成机器校验。计划记录当前完整 `originPrice`、规格、来源、最长租期及预计计算费用；预计费用超过预算即拒绝。示例结构见 [plan.example.json](examples/plan.example.json)。创建/启动前再次查询报价，任何涨跌都要求重新生成计划。

`max_compute_cost` 只约束按计划时长估算的计算费用。关机是异步操作，存在提交和释放延迟；余额冻结规则、保留镜像和存储费用需另行预留，不能把该字段当成平台账单总额硬上限。计划单次允许 5–120 分钟、`PostPaid`、`count=1`、`autoContinue=false`、一个完整 GPU；不包含多卡、训练集群或分数卡申请。

## 创建、执行、取回、关机

在用户已授权的任务范围内执行创建命令；仅生成计划不会创建资源：

```bash
.venv/bin/python tools/create_paratera_container.py --plan plans/task.json
```

命令返回本机 `lease` 路径。将该路径赋给 `LEASE`，查询状态，等待 `observed_status` 为 `Running`：

```bash
export LEASE='output/paratera/leases/REPLACE_WITH_RETURNED_LEASE.json'
.venv/bin/python tools/paratera_lease.py status "$LEASE"
.venv/bin/python tools/paratera_lease.py heartbeat "$LEASE" --phase waiting-for-running
```

准备上传文件时仅使用忽略目录 `transfer/`，自行挑选任务所需文件：

```bash
mkdir -p transfer/bundle
cp examples/hello.sh transfer/bundle/hello.sh
.venv/bin/python tools/paratera_remote.py upload --lease "$LEASE" --phase upload \
  --bundle transfer/bundle --destination /tmp/agent-cloud-bundle --log output/upload.log
.venv/bin/python tools/paratera_remote.py run --lease "$LEASE" --phase hello \
  --script examples/hello.sh --log output/hello.log
.venv/bin/python tools/paratera_remote.py download --lease "$LEASE" --phase collect \
  --remote-directory /tmp/agent-cloud-results \
  --archive output/paratera/results/hello.tar.gz --log output/download.log
.venv/bin/python tools/paratera_lease.py stop "$LEASE" --phase task-complete
.venv/bin/python tools/paratera_lease.py wait "$LEASE" --timeout 180
```

远程任务持续运行期间每 20 秒刷新心跳；连续 5 分钟没有任务心跳，或达到租期上限，看门狗对该台账中的实例提交 `StopServices`（`STOP_CHARGING`、`saveEnv=true`）。必须同时确认关机异步任务 `done=true, success=true` 和实例 `Stopped`，才算完成回收。`stop` 只登记请求，`wait` 才等待闭环确认。不要使用系统 `shutdown` 代替平台关机。

工具不删除实例、磁盘或镜像。保存环境之后的存储费用需在平台核对并由资源所有者管理。工具也不自动构建或导出任务镜像：各队提供自己的可用镜像 UUID。

## 再次启动自己登记的实例

保留原台账和工作目录。用已完成关机的台账查看当前实例配置，通过只读查询确认保存后的镜像 UUID；以原 `aliasName`、可用区、规格及当前镜像重新生成报价计划：

```bash
.venv/bin/python tools/make_plan.py --zone "$CLOUD_ZONE" --model "$CLOUD_MODEL" \
  --image "$CLOUD_IMAGE" --alias 'REPLACE_WITH_ORIGINAL_AGENT_CLOUD_ALIAS' \
  --minutes "$CLOUD_MINUTES" --max-compute-cost "$CLOUD_COMPUTE_BUDGET" \
  --verified-cny-per-hour --pricing-source https://ai.paratera.com/document/container \
  --output plans/restart.json
.venv/bin/python tools/start_paratera_container.py \
  --source-lease "$LEASE" --plan plans/restart.json
```

只有台账证明此前成功完成保留环境的节省模式关机、平台当前为 `Stopped`、UUID/可用区/别名/规格全部匹配时才会启动。启动会产生新租期台账；后续操作使用新返回的路径。

## 给 agent 使用

将 [AGENTS.md](AGENTS.md) 的约定加入工作区。推荐任务模板：

> 用我自己的凭据，在批准的计算费用和最长租期内，查询实时规格与完整报价后申请一台单 GPU 容器。先验证独立看门狗，再执行我提供的脚本，取回结果，最后确认平台关机成功。只操作当前工作目录登记的资源。异常回执不要重试购买。

每个工作目录最多一个未关闭租期；多个工作目录仍须由使用者统筹账户总预算和并发。工具包不带任何队伍的任务实现、数据、权重或实际资源台账。

## 验证和来源

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q tools tests
bash -n examples/hello.sh
git diff --check
```

离线测试模拟 API/SSH，验证预算拒绝、身份匹配、独立服务先于购买、回执不确定时不重复申请、异步关机和二进制下载；不会产生云账单。实际可用规格、权限、容量与价格以各队自己的平台账号为准。

- [官方 SDK](https://ai.paratera.com/document/computer/api/sdk)
- [创建容器 API](https://ai.paratera.com/document/openapi/container/services/CreateServices)
- [启动 API](https://ai.paratera.com/document/openapi/container/services/StartServices)
- [关机 API](https://ai.paratera.com/document/openapi/container/services/StopServices)
- [运行与故障处理](docs/operations.md)

本仓库代码采用 MIT 许可；厂商 SDK 的许可及服务条款独立适用。
