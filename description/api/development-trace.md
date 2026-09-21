# 开发链路观察接口

仅供独立 Trace Studio 展示本人真实会话的内部执行过程，不改变正式 SSE 协议。

## 接口目录

| 接口 | 方法 | 完整路径 |
| --- | --- | --- |
| [创建临时展示任务](#创建临时展示任务) | `POST` | `/contract/api/communication/development-turns` |
| [读取开发链路](#读取开发链路) | `GET` | `/contract/api/communication/conversations/{conversation_id}/turns/{turn_id}/development-trace` |

---

## 读取开发链路

`GET /contract/api/communication/conversations/{conversation_id}/turns/{turn_id}/development-trace`，读取当前驻留任务的观察快照。

认证使用 `Authorization: Bearer <login_code>`，必须拥有该会话及轮次。仅 `APP_ENV=development` 且 `COMMUNICATION_TRACE_ENABLED=true` 时开放，默认关闭。无需请求体；路径参数为创建接口返回的完整会话 ID、轮次 ID。

成功返回 `200 application/json`：

```json
{"snapshot": {"conversation_id": "…", "turn_id": "…", "status": "processing"}, "nodes": [], "audit": []}
```

`snapshot` 为正式轮次快照；`nodes` 按发生顺序记录门禁边界、vLLM 模型请求和主循环工具执行回执；`audit` 为该任务当前主循环私有审计副本。正在执行的模型节点 `status=running`，完成后更新结果和指标。模型节点包含脱敏媒体后的输入、输出、请求选项及可用的 `model_metrics.elapsed_seconds/completion_tokens`。工具执行失败不会被标成成功。

`401` 表示未登录或免登码过期；`404` 表示功能未开启、会话/轮次不存在、被驱逐或不属于当前用户；会话存储不可用返回 `503`。

```sh
curl -H 'Authorization: Bearer <login_code>' \
  'http://127.0.0.1:20000/contract/api/communication/conversations/<conversation_id>/turns/<turn_id>/development-trace'
```

记录只存在于内存，随会话或轮次清理，重启不能恢复；上限 2000 个观察节点。图片不复制原始 Base64。记录不会加入模型上下文或正式用户历史。速度仅使用模型 HTTP 请求至完整响应耗时，不含模型并发额度排队和后续工具执行，包含网络与流回调开销；并行节点耗时不可视为任务总墙钟时间。外部专家和非模型内部子步骤暂未独立计时。

独立界面位于同级 `agent-trace-showcase`，运行其 `server.py` 后访问本机 8766 端口。页面使用下述临时任务入口，经 SSE 激活、真实业务门禁和 Agent Core 执行；结果不落盘。正式会话仍保持原有持久化行为。


---

## 创建临时展示任务

`POST /contract/api/communication/development-turns`，创建单次、仅内存的展示任务。认证采用 Bearer 免登码，仅在开发环境且链路开关启用时可用。

请求为 `multipart/form-data`，只有必填 `text` 字段，1～20000 字符且不能全空白，无路径参数。每次创建独立会话，不承接上一次提问。

成功返回 `201 application/json`，结构与普通创建轮次一致，包含 `conversation_id`、`turn_id`、`status=pending_activation`、激活截止时间及 `can_interrupt=false`。随后订阅该轮次的正式 `/events` 接口激活，使用 `/development-trace` 观察；结束后可调用普通会话 DELETE 接口释放内存。归属仍按当前认证用户校验。

错误：未登录 `401`；功能关闭 `404`；空白或超长问题 `422`；临时容量已满 `503`。

```sh
curl -X POST -H 'Authorization: Bearer <login_code>' \
  -F 'text=请说明合同验收条款需要约定哪些内容' \
  'http://127.0.0.1:20000/contract/api/communication/development-turns'
```

临时会话最多驻留 32 个。只初始化内存中的空工作区、任务轨迹与思考窗口，不创建数据库会话、不触发备份、不生成检索记忆、不保存思考窗口。无活跃任务且空闲十五分钟后，由后台扫描清理，或由页面主动 DELETE。业务门禁与模型工具循环复用原实现；数据库仍可作为工具的只读检索来源。浏览器刷新不恢复结果，关闭页面并不直接中断在途模型请求。

独立页面在任务终态后使用 `nodes` 中已完成模型请求的有效用量，计算「输出 token 总和 / 对应模型耗时总和」作为平均生成速度；不平均每轮速率，不计工具时间。缺失指标时标注统计覆盖次数，没有有效指标则显示暂无有效统计。统计仅留在页面内存。
