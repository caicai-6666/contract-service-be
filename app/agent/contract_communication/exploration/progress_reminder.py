"""当前任务的反馈提醒计时；提醒只注入请求副本，不成为任务记忆。"""
from time import monotonic
from .prompt.guidance import build_progress_reminder_message


class ProgressReminder:
    def __init__(self, interval_seconds: float, *, clock=monotonic):
        self.interval_seconds = interval_seconds
        self.clock = clock
        self.last_feedback_at = clock()
        self.last_reminded_at = None
        self.pending = False

    def message(self):
        if self.interval_seconds <= 0:
            return None
        now = self.clock()
        if not self.pending and now - self.last_feedback_at < self.interval_seconds:
            return None
        # 提醒未被有效反馈满足时，不重新等待一个间隔；下一请求再次提示。
        self.pending = True
        self.last_reminded_at = now
        return build_progress_reminder_message()

    def acknowledge(self):
        """仅成功提交给用户的中途反馈能重置计时，不能按生成工具名提前解除。"""
        self.last_feedback_at = self.clock()
        self.pending = False
