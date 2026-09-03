# PDF 页面向量召回实验

> **用途：** 验证 Qwen3-VL-Embedding 在“单页向量化、整份 PDF 平均融合”条件下，能否从 `test-data` 合同库中召回同一合同的重新栅格化版本，并比较近重复专用指令与官方默认指令。

---

## 用途与假设

实验验证以下假设：

- 使用近重复专用指令生成的 PDF 融合向量，能够把同一合同的重新栅格化版本排在候选库前列。
- 专用指令应保留主体、金额、日期、条款和签章等实质差异，同时容忍缩放、渲染和再次压缩造成的视觉差异。
- 对全部页向量执行等权算术平均并重新 L2 归一化，可以形成可用于第一阶段召回的合同级向量。
- 相比官方默认指令，专用指令至少不降低 Recall@1，并应扩大正确合同与最强负样本之间的余弦相似度间隔。

若专用指令达到 100% Recall@1、100% Recall@3 且每份合同的正负间隔均大于零，则认为它在当前样本上的召回可用性通过。是否优于默认指令由两组平均正负间隔差决定，不由绝对相似度单独决定。

---

## 实验设计

### 样本与真值

- 输入：默认递归读取 `data/input/test-data/**/*.pdf`。
- 每份源 PDF 的 SHA-256 是实验身份；同一源 PDF 派生的 gallery 和 query 互为正样本，其余合同为负样本。
- 输入目录中源文件 SHA-256 完全相同的多条路径只保留字典序第一条，避免同一身份在候选库中重复出现；排除记录写入 `manifest.json`。
- gallery：使用生产 `AsyncPDFPreparationService` 按当前视觉预算形成的处理版页面。
- query：从处理版 PDF 再次按较低视觉 token 预算栅格化，模拟重复合同被重新压缩、缩放或封装后的上传版本。
- 本实验不会编辑合同文字，也不把不同合同人工标为重复。

### 变量与固定条件

指令变量：

| 版本 | 作用 |
| --- | --- |
| `official-default-v1` | 使用 vLLM 官方示例中的 `Represent the user's input.` 作为对照。 |
| `contract-near-duplicate-v1` | 使用项目正式的合同 PDF 近重复检索指令；文本从生产提示词模块导入，避免实验与实现漂移。 |

固定条件：

- 每次请求只输入一张页面图像。
- 同一组的 gallery 和 query 使用完全相同的指令。
- 每个页向量先 L2 归一化；全部页面等权平均后再次 L2 归一化。
- 使用精确余弦相似度进行内存排序，不引入 Elasticsearch HNSW 近似召回误差。
- 模型、服务地址、向量维度、渲染预算和并发数写入 `manifest.json`。

### 运行顺序

1. 准备全部 gallery 处理版 PDF。
2. 从各自处理版 PDF 生成低分辨率 query 页面。
3. 按“指令、文档、变体、页码”稳定构造请求，在固定并发上限内异步执行。
4. 保存逐页向量和请求指标。
5. 分别按两种指令融合 gallery/query 向量，执行全库精确排序并计算指标。

---

## 指标与判定

| 指标 | 口径 |
| --- | --- |
| Recall@1 | 正确合同位于第一名的 query 数 / 全部合同数；请求失败按未召回计。 |
| Recall@3 | 正确合同位于前三名的 query 数 / 全部合同数；请求失败按未召回计。 |
| MRR | 正确合同排名倒数的均值；失败记为 0。 |
| 正样本相似度 | query 与同源 gallery 融合向量的余弦相似度。 |
| 最强负样本相似度 | query 与所有异源 gallery 中最高的余弦相似度。 |
| 正负间隔 | 正样本相似度减去最强负样本相似度；大于零表示正确合同排在所有负样本之前。 |

专用指令的当前样本通过条件：

- `Recall@1 = 1.0`；
- `Recall@3 = 1.0`；
- 最小正负间隔大于 `0`；
- 全部页面请求成功且向量维度符合配置。

“优于默认指令”需要专用指令的平均正负间隔大于默认组；若只是 Recall 相同而间隔未提高，只能说明专用指令可用，不能证明它更优。

---

## 运行方式

前置条件：

- 已创建 `.venv` 并安装 `requirements.txt`。
- `.env` 中的 `VLLM_EMBEDDING_BASE_URL` 可访问，远程服务以 pooling runner 启动 Qwen3-VL-Embedding。

执行完整实验：

```bash
.venv/bin/python experiment/pdf-page-embedding-recall/run.py
```

冒烟验证一份合同：

```bash
.venv/bin/python experiment/pdf-page-embedding-recall/run.py --max-contracts 1
```

调整 query 页面预算或请求并发：

```bash
.venv/bin/python experiment/pdf-page-embedding-recall/run.py \
    --query-visual-tokens-per-page 2048 \
    --concurrency 4
```

每次运行创建新的 UTC 时间戳目录，不覆盖已有实验产物。

---

## 产物说明

```text
output/<UTC 时间>/
├── manifest.json
├── result.json
├── requests.json
├── page-embeddings.json
└── analysis.md
```

- `manifest.json`：样本哈希、完全重复输入排除记录、提示词、模型、去密钥服务配置、渲染参数和运行环境。
- `requests.json`：逐页面请求的状态、耗时、token、图片哈希和向量维度，不含图片与向量。
- `page-embeddings.json`：成功页面请求的原始向量；供后续融合策略实验复用，不包含页面图片。
- `result.json`：两组指令的逐合同排序、Recall、MRR、相似度间隔和组间差异。
- `analysis.md`：人工分析记录，只追加，不由运行器生成。

---

## 局限性

- 当前测试集只有少量合同，且没有同模板、仅金额或主体不同的系统性困难负样本。
- query 是程序确定性生成的重新栅格化版本，不能覆盖拍照、旋转、污损、删页、增页和人工修改等真实上传变化。
- 当前只验证等权平均融合；首页加权、首尾加权及其他融合策略留待后续实验。
- 内存精确排序不验证 Elasticsearch 索引 mapping、量化或 HNSW 参数造成的近似召回损失。
- 远程服务负载和模型缓存会影响耗时，但不应改变确定性输入的召回真值。
