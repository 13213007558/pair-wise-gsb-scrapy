# 启动授权复核（preflight）演示

暂停数天的采集作业恢复时，站点侧的授权范围可能已经变化。本演示在任何持久化
请求被交给下载器之前，先核对 JOBDIR 中每条请求记录的授权版本与本地策略，把
落在被撤销范围内的请求隔离成可审查记录。

## 复核流程

1. **先加载本地策略文件**：策略无法读取或解析失败时直接 fail-closed，根本不
   打开请求队列，因此不会有“先发几个请求再终止”。
2. **只读扫描持久队列**：按 queuelib LIFO 磁盘格式从字节解析请求，原队列文件
   全程不打开写句柄、不截断、不删除。
3. **逐条核对授权**：对每条请求依次判断
   - `meta.authz_version` 缺失 → `missing_authz_version`
   - 存储版本与策略版本不一致 → `authz_version_mismatch`
   - 同版本但 host/path/scope 命中 deny 或不被 allow 覆盖 → `scope_revoked`
4. **暂存重建 + 原子切换**：允许的请求写入 `requests.queue.staging`，被撤销的
   写入 `preflight-quarantine.jsonl`；全部成功后才把原目录改名为
   `requests.queue.preflight-backup`，再把暂存目录提升为正式队列。
5. **失败回滚**：扫描或重建中途任何异常都会关闭暂存句柄并删除暂存目录，原队列
   保持字节不变，作业以非零退出码中止，不发送任何请求。

## 输出与脱敏

复核结果只输出数量（`scanned` / `retained` / `quarantined`）和不可逆标识：

- 记录 ID 与请求指纹为 SHA-256 截断值；
- URL 仅保留 scheme、脱敏 host（如 `sh***e`）和路径指纹，不输出真实路径；
- 隔离文件是每行一条 JSON 的可审查记录，同样不含完整 URL。

## 目录与文件

- `policy.py`：本地策略加载、scope 匹配与脱敏工具。
- `queue_scan.py`：只读队列解析、授权判定、暂存重建、隔离与清单写入。
- `preflight.py`：启动闸门编排，策略/队列错误统一 fail-closed。
- `fixtures.py`：用 Scrapy 真实磁盘队列类构造 JOBDIR，以及不联网的模拟下载器。
- `run_demo.py` / `__main__.py`：四个场景的端到端演示。

## 运行

```bash
python -m authz_preflight_demo
```

四个场景（全部使用 `$TMPDIR` 下的临时 JOBDIR 和本地策略文件）：

1. 首次启动：无持久队列，闸门放行且发送 0 条请求。
2. 正常恢复：授权版本未变，全部请求保留并放行。
3. 授权收紧（v1 → v2）：保留仍被允许的请求，撤销请求进入隔离文件，原队列整体
   备份，不被清空。
4. 检查失败：损坏的策略文件或队列触发 fail-closed，发送 0 条请求、原队列字节不
   变、无残留暂存目录。

场景 4 故意以非零退出码（2）结束，表示闸门关闭；这是预期行为。

## 测试

```bash
python -m pytest authz_preflight_demo/
```
