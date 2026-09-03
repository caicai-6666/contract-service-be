# PDF 全量查重判断实验

## 用途与假设

本实验验证 `judge_full_documents` 在处理同一合同的图像扰动版本时，能否稳定输出 `duplicate`，并以异源合同对照检查是否误判为重复。

实验假设如下：

- 非等比缩放、强 JPEG 降质、缩小叠加强降质不应改变同一合同的重复关系。
- 缺失部分页面、但主体与关键身份仍能对应的同源合同，按当前“修订版或不完整副本可被替代”的业务规则，应判为 `duplicate`。
- 明显异源的合同应判为 `different`。
- 所有样本必须经生产 PDF 准备服务计算真实页数和视觉 token，并由正式路由函数判为 `full_document`；实验不调用尚未实现的翻页 Agent。

若正例或负例判断不符合预期，说明当前全量判断提示词、视觉输入或工具协议至少有一项仍需调整。小样本全部通过只证明当前样本上的可用性，不代表生产准确率。

## 实验设计

输入固定来自 `data/input/test-data`，运行器选择以下关系：

| 用例 | 上传合同变换 | 候选合同 | 预期 |
| --- | --- | --- | --- |
| `same-nonuniform-wide` | 2 页合同横向 1.35、纵向 0.75 | 同一原件 | `duplicate` |
| `same-missing-middle` | 3 页合同删除中间页 | 同一原件 | `duplicate` |
| `same-jpeg-q30` | 单页合同 JPEG quality 30 | 同一原件 | `duplicate` |
| `same-scale-half-jpeg-q30` | 2 页合同缩小 50% 后 JPEG quality 30 | 同一原件 | `duplicate` |
| `cross-quality` | 上述降质单页合同 | 异源 3 页合同 | `different` |
| `cross-nonuniform` | 上述非等比缩放合同 | 异源单页合同 | `different` |

变换件先由实验代码将 PDF 页面栅格化、执行确定性变换并重新封装为 PDF，再与原件分别进入 `AsyncPDFPreparationService`。正式判断节点只接收准备完成的 `PreparedPDF`。

运行分为两个阶段：

1. 对全部用例执行生产预处理和正式路由预检；只要一项不是 `full_document`，就在任何模型调用发生前终止。
2. 通过预检后，按 `--concurrency` 并发调用正式 `judge_full_documents` 节点。

模型、提示词版本、视觉预算、生成参数和随机种子保持当前环境配置，不在实验中覆盖。

## 指标与判定

- `route_eligible_rate`：进入 `full_document` 的用例数 / 全部用例数。
- `exact_relation_accuracy`：模型关系等于人工预期的用例数 / 已配置用例数；`failed` 计为错误。
- `duplicate_recall`：同源正例中输出 `duplicate` 的比例。
- `different_recall`：异源负例中输出 `different` 的比例。
- `failed_rate`：输出 `failed` 的比例。
- `end_to_end_elapsed_ms`：单用例从调用节点到获得终态的耗时，包含客户端处理、网络和全部模型轮次。
- `model_request_elapsed_ms`：旁路指标记录的单次 HTTP 模型请求耗时；与端到端耗时分开统计。
- token 与缓存指标：汇总模型响应返回的 prompt、completion 和 cached token；服务未返回时保持 `null`。

判定标准：6 个用例全部走全量路线，关系准确率、正例召回和负例召回均为 100%，且无 `failed`，记为本次小样本实验通过；预检失败或任一质量条件不满足，记为失败。若服务不可达导致未形成结果，则记为无结论。

## 运行方式

前置条件：

- 已在项目根目录的 `.env` 配置可达的 MLLM OpenAI 兼容地址、模型和 API key（如需）。
- 已安装 `requirements.txt` 中的依赖。
- `data/input/test-data` 中存在 README 所列固定样本。

只执行预处理与路由检查，不产生模型请求：

```bash
.venv/bin/python experiment/pdf-full-document-deduplication/run.py --preflight-only
```

执行真实实验：

```bash
.venv/bin/python experiment/pdf-full-document-deduplication/run.py --concurrency 3
```

每次运行都会创建独立 UTC 时间戳目录，不覆盖历史产物。

## 产物说明

- `manifest.json`：样本、变换、哈希、路由决定、模型/提示词/视觉与生成配置、Git 指纹。
- `result.json`：逐用例关系、质量指标和耗时汇总。
- `cases/<用例>/routing.json`：正式路由函数的原始结构化决定。
- `cases/<用例>/judgment.json`：正式节点的完整结构化终态与工具调用审计。
- `cases/<用例>/inference-metrics.json`：不包含提示词和合同正文的模型请求指标。
- `analysis.md`：运行后追加的人工分析，不由运行器生成。

## 局限性

- 样本量只有 6 对，且只覆盖中文合同和当前四类确定性变换。
- 异源样本的 `different` 标注依据文件来源与合同主题，不包含财务、采购专家的双人盲审。
- 没有构造真实修订版、印章遮挡、旋转、手写修改、重排页面或扫描噪声。
- 实验直接验证全量判断节点，不经过向量召回阈值；因此不能代表端到端查重召回率。
- 并发、远端服务负载、prefix cache 状态和模型随机性会影响耗时与少量生成差异。

