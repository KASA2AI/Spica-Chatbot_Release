"""Fixed Linux RTC/logind operations. No shell, privilege escalation or sleep fallback."""
import json
import os
from pathlib import Path
import subprocess
import time


class HomePower:
    def __init__(self, *, rtc='/sys/class/rtc/rtc0/wakealarm', run=subprocess.run, wall_clock=time.time,
                 mem_sleep='/sys/power/mem_sleep', suspend_count='/sys/power/suspend_stats/success'):
        self.rtc = Path(rtc)
        self.mem_sleep, self.suspend_count = Path(mem_sleep), Path(suspend_count)
        self.run, self.clock = run, wall_clock

    @staticmethod
    def boot_id():
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def _bus(self, operation, member, *arguments):
        result = self.run(('/usr/bin/busctl', '--json=short', '--allow-interactive-authorization=no',
            operation, 'org.freedesktop.login1', '/org/freedesktop/login1',
            'org.freedesktop.login1.Manager', member, *arguments),
            capture_output=True, text=True, timeout=2, check=False)
        if result.returncode:
            raise RuntimeError('logind rejected ' + member)
        return json.loads(result.stdout)['data'] if result.stdout.strip() else None

    def scheduled_shutdown(self):
        value = self._bus('get-property', 'ScheduledShutdown')
        if (not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], str)
                or type(value[1]) is not int or value[1] < 0):
            raise RuntimeError('invalid logind shutdown receipt')
        return tuple(value)

    def shutting_down(self):
        value = self._bus('get-property', 'PreparingForShutdown')
        if type(value) is not bool:
            raise RuntimeError('shutdown state unavailable')
        return value

    def read_alarm(self):
        return int(self.rtc.read_text().strip() or '0')

    def status(self):
        return dict(rtc_epoch=self.read_alarm(), rtc_writable=os.access(self.rtc, os.W_OK),
                    suspend=self._bus('call', 'CanSuspend'), mem_sleep=self.mem_sleep.read_text().strip(),
                    resume_stamp=self.resume_stamp(),
                    scheduled_shutdown=self.scheduled_shutdown(), boot_id=self.boot_id())

    def resume_stamp(self):
        """Kernel success count changes on real resume, including the same process."""
        return dict(boot_id=self.boot_id(), suspend_count=int(self.suspend_count.read_text().strip()))

    def resume_ready(self):
        """The kernel wakes before systemd's GPU/VT restoration has finished."""
        value = self._bus('get-property', 'PreparingForSleep')
        if type(value) is not bool:
            raise RuntimeError('system resume state unavailable')
        return not value

    def resumed_since(self, receipt):
        before = receipt.get('resume_stamp')
        after = self.resume_stamp()
        return bool(before and before['boot_id'] == after['boot_id']
                    and after['suspend_count'] > before['suspend_count'])

    def prepare_suspend(self, epoch, cancelled, *, record=lambda receipt: None):
        receipt = dict(resume_stamp=self.resume_stamp(), rtc_epoch=0, rtc_owned=False, rtc_attempted=False)
        try:
            if '[deep]' not in self.mem_sleep.read_text().split():
                raise RuntimeError('当前未选择挂起到内存 deep，未执行电源操作')
            capability = self._bus('call', 'CanSuspend')
            if capability != ['yes'] and capability != 'yes':
                raise RuntimeError('当前会话没有非交互挂起权限')
            if epoch is not None:
                return dict(self.arm_wake(epoch, cancelled, record=lambda value: record(dict(receipt, **value))),
                            resume_stamp=receipt['resume_stamp'])
            if cancelled.is_set():
                raise InterruptedError('bedtime cancelled')
            if self.scheduled_shutdown() != ('', 0) or self.read_alarm():
                raise RuntimeError('已有其他电源或 RTC 安排，未执行无闹钟挂起')
            return dict(receipt, status='verified', reason='')
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return dict(receipt, status='failed', reason=str(exc))

    def suspend(self, receipt, cancelled, *, validate, margin_seconds, record=lambda receipt: None):
        """One fixed request, after final business/RTC checks; acceptance is not sleep."""
        result = dict(receipt, suspend_attempted=False)
        try:
            if cancelled.is_set():
                raise InterruptedError('bedtime cancelled')
            validate()
            epoch = receipt.get('rtc_epoch', 0)
            if self.read_alarm() != epoch:
                raise RuntimeError('RTC 安排已改变，未继续挂起')
            if self.scheduled_shutdown() != ('', 0):
                raise RuntimeError('已有其他系统关机计划')
            if epoch and (margin_seconds is None or epoch-self.clock() <= margin_seconds):
                return dict(result, status='stay_awake', reason=(
                    'suspend_margin_uncalibrated' if margin_seconds is None else 'wake_time_too_close'))
            if '[deep]' not in self.mem_sleep.read_text().split():
                raise RuntimeError('deep 挂起模式已改变')
            result.update(resume_stamp=self.resume_stamp(), suspend_attempted=True)
            record(result)
            if cancelled.is_set():
                return dict(result, suspend_attempted=False, status='failed', reason='bedtime cancelled')
            self._bus('call', 'Suspend', 'b', 'false')
            return dict(result, status='suspend_requested', reason='')
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return dict(result, status='unknown' if result['suspend_attempted'] else 'failed', reason=str(exc))

    def arm_wake(self, epoch, cancelled, *, record=lambda receipt: None):
        """Kernel ABI: absolute epoch seconds; never overwrite another RTC plan."""
        receipt = dict(rtc_epoch=int(epoch), rtc_owned=False, rtc_attempted=False)
        try:
            if int(epoch) <= self.clock():
                raise ValueError('主机启动时刻已经过去')
            if cancelled.is_set():
                raise InterruptedError('bedtime cancelled')
            if self.scheduled_shutdown() != ('', 0):
                raise RuntimeError('已有其他系统关机计划')
            capability = self._bus('call', 'CanSuspend')
            if capability != ['yes'] and capability != 'yes':
                raise RuntimeError('当前会话没有非交互挂起权限')
            current = self.read_alarm()
            if current not in (0, int(epoch)):
                raise RuntimeError('已有其他 RTC 自动启动计划')
            if not current:
                if cancelled.is_set():
                    raise InterruptedError('bedtime cancelled')
                receipt['rtc_attempted'] = True
                record(receipt)
                self.rtc.write_text(str(int(epoch)))
                receipt['rtc_owned'] = True
            if self.read_alarm() != int(epoch):
                raise RuntimeError('RTC 读回与计划不同')
            return dict(receipt, status='verified', reason='')
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            if receipt['rtc_attempted'] and not receipt['rtc_owned']:
                try:
                    receipt['rtc_owned'] = self.read_alarm() == int(epoch)
                    receipt['rtc_attempted'] = receipt['rtc_owned']
                except Exception:
                    return dict(receipt, status='unknown', reason=str(exc))
            return dict(receipt, status='failed', reason=str(exc))

    def cancel(self, receipt, *, closing=False, after_boot=False):
        """Clear only our exact reservations, retaining RTC on uncertain shutdown."""
        try:
            if receipt.get('backend') not in (None, 'linux_rtc'):
                return dict(receipt, status='unknown', reason='foreign power reservation; original platform must reconcile it')
            if receipt.get('suspend_attempted') and not after_boot and not self.resumed_since(receipt):
                # logind has no CancelSuspend. A lost/accepted reply must not
                # remove the RTC that may be needed moments later.
                return dict(receipt, status='unknown', reason='挂起申请可能已受理，保留 RTC 等待真实恢复')
            if not any(receipt.get(key) for key in ('rtc_owned', 'rtc_attempted', 'shutdown_owned', 'shutdown_attempted')):
                return dict(receipt, status='cancelled', reason='')
            if self.shutting_down():
                return dict(receipt, status='shutting_down' if closing else 'unknown',
                            reason='系统已开始关机，RTC 安排保留')
            if receipt.get('shutdown_attempted') and not receipt.get('shutdown_owned'):
                if self.scheduled_shutdown() == ('poweroff', receipt['shutdown_usec']):
                    receipt = dict(receipt, shutdown_owned=True)
                elif not after_boot:
                    return dict(receipt, status='unknown', reason='关机申请可能已受理，保留 RTC 并等待核对')
            if receipt.get('shutdown_owned'):
                current = self.scheduled_shutdown()
                if current == ('poweroff', receipt['shutdown_usec']):
                    self._bus('call', 'CancelScheduledShutdown')
                    if self.scheduled_shutdown() == current:
                        raise RuntimeError('系统延迟关机尚未确认撤销')
                # A different schedule belongs to another caller and is untouched.
            if receipt.get('rtc_attempted') and not receipt.get('rtc_owned'):
                current = self.read_alarm()
                if current == receipt['rtc_epoch']:
                    receipt = dict(receipt, rtc_owned=True)
                elif current != 0:
                    return dict(receipt, status='unknown', reason='RTC 写入结果未确认，不清除其他时刻的安排')
            if receipt.get('rtc_owned') and self.read_alarm() == receipt['rtc_epoch']:
                self.rtc.write_text('0')
                if self.read_alarm() != 0:
                    raise RuntimeError('RTC 清理结果未确认')
            return dict(receipt, status='cancelled', reason='')
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return dict(receipt, status='unknown', reason=str(exc))

    def close(self):
        pass
