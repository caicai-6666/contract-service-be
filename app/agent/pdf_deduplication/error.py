"""PDF 查重工作流共享异常。"""


class PDFDeduplicationNodeNotImplementedError(NotImplementedError):
    """节点契约已建立，但外部能力尚未接入。"""


__all__ = ["PDFDeduplicationNodeNotImplementedError"]
