# PDF 视觉压缩与重新封装工具

> **用途：** 本能力文档说明 `app.tool.pdf_page` 如何将 PDF 页面按视觉 token 预算渲染为 PNG，并把模型实际读取的页面重新封装为处理版 PDF。

---

## 使用边界

该工具只处理 PDF 页面、图像尺寸、视觉 token 预算和 PDF 重新封装，不了解字段定义、提示词、工作流状态或模型请求。`app.service.pdf_preparation.AsyncPDFPreparationService` 在创建任务时异步调用该同步工具，将页面 PNG 与轻量元数据保存为工作流可直接消费的 `PreparedPDF`，不长期保存整份 PDF 字节。

处理版 PDF 由预算内 PNG 页面重新封装，因此保留页序和可见内容，但不保留原始文本层、矢量对象、批注、表单或链接。这个产物用于当前视觉模型处理和任务生命周期内存储，不替代需要原始法律文件语义的归档件。

这里的“压缩”指降低模型实际读取的页面分辨率和视觉 token，不承诺处理版 PDF 的字节数小于原文件。原文件主要由文字或高效矢量对象组成时，栅格化处理版可能反而更大；接口中的 `file_size_bytes` 与权威 `document_id` 一样描述处理版，原文件大小只在内部 `source_file_size_bytes` 中用于追溯。

默认配置为：最大渲染比例 `2.0`、视觉 patch `32×32` 像素、单页最多 `2048` token。对当前 A4 测试合同，`2×` 渲染约占 `2014` token，因此不会因预算而缩小。

---

## 接口

| 接口 | 职责 |
| --- | --- |
| `compress_pdf` | 按视觉预算渲染全部页面，并返回重新封装的 PDF 字节与同源页面缓存。 |
| `compress_pdf_page` | 渲染指定的一页 PDF，返回 PNG 字节、实际尺寸、渲染比例和 token 数。 |
| `compress_pdf_pages` | 按原始页序渲染整份或指定页面；可用 report_page_errors=True 将逐页错误包装为带页码的 PDFPageRenderError，默认异常行为不变。 |
| `assemble_pdf_pages` | 按连续页码将已有 PNG 封装为 PDF，不再次渲染。 |
| `estimate_visual_tokens` | 根据图像尺寸与 patch 大小估算视觉 token。 |

单页 token 估算公式如下：

```text
ceil(width_pixels / patch_size) × ceil(height_pixels / patch_size)
```

Agent 不再使用固定的单次视觉预算。MLLM 默认上下文窗口为 `262144` token，先扣除最大生成、公共提示词以及多轮工具历史预留，得到 `239616` token 的视觉容量上限。实际请求预算随 PDF 页数增长：

```text
min(视觉容量上限, PDF 页数 × 单页视觉上限)
```

短合同只使用必要的上下文；页数增加时可以使用更多视觉上下文，直到容量上限。若整份 PDF 按原始分辨率会超限，PDF 准备服务把视觉容量等分为动态单页预算，并对每页保持长宽比缩小。当前普通 A4 页以 `2×` 渲染约占 `2014` token，因此 21 页约占 `42294` token；同等页面在接近 119 页时才会触及当前视觉容量上限。

---

## 内存生命周期与按需组装

以下完整 PDF 身份与按需组装流程属于合同提取。Communication 的[文件可读性子图](../../architecture/workflow/contract-communication/file-readability.md)同样复用 compress_pdf_pages、MLLM 动态单页/总预算和 PreparedPDFPage，但仅保存逐页对象，不执行整份 PDF 组装或生成处理版文档 ID；多文件预算按每份 PDF 分别计算。

1. 创建时渲染原始 PDF，得到逐页 PNG，并临时组装一次处理版 PDF，计算权威 `document_id` 和文件大小；随后释放原始文件及整份处理版字节。
2. 任务仅长期保留同一份页面 PNG、媒体 UUID、内容指纹、像素尺寸和物理尺寸。模型公共上下文共享 PNG 引用，不保存 Base64，详见 [vLLM 多模态媒体引用](../infrastructure/vllm-media-reference.md)。
3. 预览与入库调用 `assemble_processed_pdf`，在线程内通过 `assemble_pdf_pages` 按需组装；交付前检查文件大小和 SHA-256 与创建时一致，不一致则报错，禁止交付错误身份的文件。
4. 组装结果只由当前资源响应或入库操作持有，不回写任务，也不增加 PDF 缓存。取消、过期、入库释放后，PNG 随剩余在途引用释放。

每页保留旋转生效后的可见矩形 `width_points`、`height_points`，不能由像素数除以渲染比例反推：像素取整可能导致 PDF 物理尺寸、编码字节及合同哈希变化。组装沿用原有插图顺序和 `garbage=4, deflate=True, no_new_id=True, preserve_metadata=False, reproducible=True` 编码选项；同一运行不改变图像或尺寸。

候选加载只恢复模型页面、保留磁盘合同的原有身份；其页面可能使用当前预算重新渲染，不能通过组装页面替代正式候选文件。候选下载仍读取 `data/contract` 中的原字节。

准备、候选恢复与按需组装的 PyMuPDF 操作通过同一进程内可重入锁串行执行，避免新增组装任务与渲染线程同时操作 PyMuPDF；异步调用方通过 `to_thread` 等待，不阻塞事件循环。此锁不是上传队列容量限制，也不限制模型全局并发。首次渲染、模型发送和并发下载仍有临时内存峰值，本方案不保证批量上传下的总内存上限。

---

## 验证要求

- 每页结果必须不超过 `max_visual_tokens_per_page`。
- 页码必须从 1 开始、严格递增且不能重复。
- 整份 PDF 的 token 总数不得超过该 PDF 的动态请求预算。
- 处理版 PDF 页数、页序和可见内容必须与页面缓存一致。
- 工作流复用同一合同页面时，应复用同一份渲染结果与页序，保证多模态公共前缀稳定。
- 混合尺寸、裁剪和旋转页面按需组装后，字节须与旧编码算法一致，内嵌像素须与 PNG 一致。
- 资源读取不得延长 TTL；组装期间发生取消时不得阻塞取消或继续交付文件。
