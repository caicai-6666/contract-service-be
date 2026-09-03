# 模型推理指标观察能力

> **用途：** 本文说明应用如何按异步任务隔离采集 MLLM 与 Embedding 的请求耗时、token 和 vLLM 逐请求指标。合同批量基线的实验方案见[合同提取质量与推理指标实验](../../../experiment/contract-extraction-quality/README.md)。

---

## 能力边界

`app.infrastructure.inference_metrics` 提供可选的任务局部观察器。MLLM 和 Embedding 适配器在每次 OpenAI 兼容请求完成或失败后发送一条 `InferenceRequestMetrics`，记录：

- 模型类型、业务阶段、端点和配置模型名。
- 请求 UTC 开始时间、客户端端到端耗时、成功状态和有限错误类型。
- 响应 ID、实际模型名、HTTP 状态，以及 prompt、cached、completion 和 total token 用量。
- vLLM 顶层 `metrics` 中的数值型逐请求指标。

记录不包含模型消息、PDF 图像、工具参数、回答正文、API Key 或完整错误响应。业务节点已有的私有工具审计继续负责动作和校验追溯，推理指标观察器只负责性能事实，不能替代前者。

---

## 并发隔离

观察器和业务阶段都使用 `ContextVar` 绑定。绑定随当前异步任务传播到它创建的并发子任务，同时不会进入其他合同任务。分类、Core、Clause 和 Retrieval 内部并发请求因此可汇入当前实验收集器，并保留各自阶段标签。

观察器属于旁路能力：未绑定时不会保存记录；观察回调抛出异常时，适配器忽略该异常并继续正式模型请求，避免监控故障改变合同提取结果。实验需要完整指标时，应另行校验请求数和服务端指标覆盖率。

---

## vLLM 指标

MLLM 使用 `--enable-per-request-metrics` 启动时，非流式 Chat Completions 响应可携带顶层 `metrics`。当前采集器保留其中全部数值字段；实验聚合以下稳定指标：

| 字段 | 单位 | 含义 |
| --- | --- | --- |
| `time_to_first_token_ms` | ms | 请求被调度到首个输出 token 的时间。 |
| `queue_time_ms` | ms | 调度队列等待时间。 |
| `generation_time_ms` | ms | 首 token 到末 token 的解码时间。 |
| `mean_itl_ms` | ms | 平均 token 间延迟。 |
| `tokens_per_second` | token/s | 当前请求生成吞吐。 |

Embedding 服务未必返回同一顶层指标；缺失时仍记录客户端耗时、状态和服务返回的 token。指标缺失使用 `null` 或零样本数表达，不能用客户端耗时伪装服务端推理时间。

---

## 使用与限制

普通应用运行不会自动把指标写入控制台、SSE、快照或文件。实验运行器通过 `bind_inference_metrics_observer` 显式绑定收集器，并用 `bind_inference_stage` 标记当前阶段。

逐请求指标适合分析延迟、排队和吞吐，不记录也不能推导模型思维链。启用 vLLM 指标可能增加服务端 CPU 开销；生产使用前应以目标并发重新评估。持久化、Prometheus 导出、采样和敏感信息治理不在当前能力范围内。
