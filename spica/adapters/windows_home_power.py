"""Windows S3 backend for the existing Home bedtime receipt/state machine.

rtc_* are the shared receipt vocabulary, not claims that Windows wrote Linux's
RTC sysfs. backend/task identity keeps a copied Linux receipt from cancelling
Windows tasks or being mistaken for a Windows reservation.
"""
import time


class WindowsHomePower:
    def __init__(self, *, state=None, task=None, wall_clock=time.time):
        from spica.adapters.windows_power_state import WindowsPowerState
        from spica.adapters.windows_wake_task import WindowsWakeTask
        self.task = task if task is not None else WindowsWakeTask()
        self.state = state if state is not None else WindowsPowerState()
        self.clock = wall_clock

    def boot_id(self):
        return self.state.boot_id

    def resume_stamp(self):
        return self.state.resume_stamp()

    def resume_ready(self):
        return self.state.resume_ready()

    def resumed_since(self, receipt):
        before, after = receipt.get('resume_stamp'), self.resume_stamp()
        return bool(before and before['boot_id'] == after['boot_id']
                    and after['suspend_count'] > before['suspend_count'])

    def status(self):
        return dict(backend='windows_task_scheduler', rtc_epoch=self.task.read(),
            rtc_writable=self.state.wake_enabled(), suspend='yes' if self.state.can_suspend() else 'no',
            mem_sleep='[deep]' if self.state.can_suspend() else 'unavailable',
            resume_stamp=self.resume_stamp(), boot_id=self.boot_id(), scheduled_shutdown=None)

    def _receipt(self, epoch=0):
        return dict(backend='windows_task_scheduler', wake_task=self.task.name,
                    rtc_epoch=int(epoch), rtc_owned=False, rtc_attempted=False)

    def _check(self, cancelled):
        if cancelled.is_set():
            raise InterruptedError('bedtime cancelled')
        if self.state.shutting_down():
            raise RuntimeError('Windows shutdown is in progress')
        if not self.state.can_suspend():
            raise RuntimeError('Windows S3 is unavailable; no other sleep mode requested')
        if not self.resume_ready():
            raise RuntimeError('Windows resume is not ready')

    def arm_wake(self, epoch, cancelled, *, record=lambda receipt: None):
        receipt = self._receipt(epoch)
        try:
            self._check(cancelled)
            if int(epoch) <= self.clock():
                raise ValueError('主机唤醒时刻已经过去')
            if not self.state.wake_enabled():
                raise RuntimeError('Windows 当前电源计划未启用普通唤醒定时器')
            if self.task.read():
                raise RuntimeError('已有唤醒任务，未覆盖或接管')
            if cancelled.is_set():
                raise InterruptedError('bedtime cancelled')
            receipt['rtc_attempted'] = True
            record(receipt)
            self.task.create(int(epoch))
            receipt['rtc_owned'] = True
            if self.task.read() != int(epoch):
                raise RuntimeError('Windows 唤醒任务读回不符')
            return dict(receipt, status='verified', reason='')
        except Exception as exc:
            # A failed COM reply may follow a successful registration. Keep
            # attempted evidence for exact-identity cleanup, never blindly retry.
            return dict(receipt, status='unknown' if receipt['rtc_attempted'] else 'failed', reason=str(exc))

    def prepare_suspend(self, epoch, cancelled, *, record=lambda receipt: None):
        receipt = self._receipt()
        try:
            self._check(cancelled)
            receipt['resume_stamp'] = self.resume_stamp()
            if epoch is not None:
                return dict(self.arm_wake(epoch, cancelled, record=lambda value: record(dict(receipt, **value))),
                            resume_stamp=receipt['resume_stamp'])
            if self.task.read():
                raise RuntimeError('已有 Home 唤醒任务，未执行无闹钟挂起')
            return dict(receipt, status='verified', reason='')
        except Exception as exc:
            return dict(receipt, status='failed', reason=str(exc))

    def _owns_backend(self, receipt):
        return receipt.get('backend') == 'windows_task_scheduler' and receipt.get('wake_task') == self.task.name

    def suspend(self, receipt, cancelled, *, validate, margin_seconds, record=lambda receipt: None):
        result = dict(receipt, suspend_attempted=False)
        try:
            if not self._owns_backend(receipt) or receipt.get('status') != 'verified':
                raise RuntimeError('Windows power reservation is not verified')
            self._check(cancelled)
            validate()
            epoch = receipt.get('rtc_epoch', 0)
            if self.task.read() != epoch:
                raise RuntimeError('Windows 唤醒任务已改变，未继续挂起')
            if epoch:
                if not self.state.wake_enabled():
                    raise RuntimeError('Windows 唤醒定时器策略已改变')
                if margin_seconds is None or epoch-self.clock() <= margin_seconds:
                    return dict(result, status='stay_awake', reason=(
                        'suspend_margin_uncalibrated' if margin_seconds is None else 'wake_time_too_close'))
            result.update(resume_stamp=self.resume_stamp(), suspend_attempted=True)
            record(result)
            if cancelled.is_set():
                return dict(result, suspend_attempted=False, status='failed', reason='bedtime cancelled')
            self.state.suspend()
            return dict(result, status='suspend_requested', reason='')
        except Exception as exc:
            return dict(result, status='unknown' if result['suspend_attempted'] else 'failed', reason=str(exc))

    def cancel(self, receipt, *, closing=False, after_boot=False):
        try:
            owned = any(receipt.get(key) for key in ('rtc_owned', 'rtc_attempted', 'shutdown_owned', 'shutdown_attempted'))
            if not self._owns_backend(receipt):
                if owned:
                    return dict(receipt, status='unknown', reason='foreign power reservation; original platform must reconcile it')
                return dict(receipt, status='cancelled', reason='')
            if receipt.get('suspend_attempted') and not after_boot and not self.resumed_since(receipt):
                return dict(receipt, status='unknown', reason='挂起结果未确认，保留 Windows 唤醒任务')
            if self.state.shutting_down():
                return dict(receipt, status='shutting_down' if closing else 'unknown', reason='Windows 正在关机，唤醒任务保留')
            if owned:
                self.task.cancel(receipt['rtc_epoch'])
            return dict(receipt, status='cancelled', reason='')
        except Exception as exc:
            return dict(receipt, status='unknown', reason=str(exc))

    def close(self):
        self.state.close()
