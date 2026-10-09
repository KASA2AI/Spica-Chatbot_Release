"""One installation/user-owned Task Scheduler wake reservation; never a shell."""
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
import hashlib
from pathlib import Path
import subprocess
import sys
import traceback


def _com_thread(operation):
    @wraps(operation)
    def call(*args, **kwargs):
        import pythoncom
        pythoncom.CoInitialize()
        try:
            return operation(*args, **kwargs)
        except BaseException as error:
            # Exception tracebacks retain completed operation frames and their
            # COM wrappers. Release those locals while the apartment is still
            # initialized, keeping the original exceptions and traceback sites.
            pending, seen = [error], set()
            while pending:
                current = pending.pop()
                if id(current) in seen:
                    continue
                seen.add(id(current))
                pending.extend(value for value in (current.__cause__, current.__context__) if value is not None)
                traceback.clear_frames(current.__traceback__)
            raise
        finally:
            # The operation frame and all of its COM wrappers are released
            # before the apartment belonging to this worker is closed.
            pythoncom.CoUninitialize()
    return call


class WindowsWakeTask:
    def __init__(self, *, root=None, executable=None):
        import win32api
        import win32con
        import win32security
        self.root = Path(root or Path(__file__).resolve().parents[2]).resolve()
        self.executable = str(Path(executable or sys.executable).resolve())
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            self.sid = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])
        finally:
            token.Close()
        identity = str(self.root).casefold() + '\n' + self.sid
        self.name = 'Spica-Desktop-Home-Wake-' + hashlib.sha256(identity.encode()).hexdigest()[:16]
        self.description = 'Spica desktop Home wake reservation v1\n' + identity
        # S3 retains this desktop process; the scheduler action only wakes it.
        # It must not start the ecosystem core or a second desktop instance.
        self.arguments = subprocess.list2cmdline(['-X', 'utf8', '-s', '-c', 'pass'])

    @contextmanager
    def _scheduler(self):
        import pythoncom
        import win32com.client
        try:
            scheduler = win32com.client.Dispatch('Schedule.Service')
            scheduler.Connect()
            yield scheduler, scheduler.GetFolder('\\')
        except pythoncom.com_error as exc:
            raise OSError('Windows Task Scheduler operation failed (0x%08x)' % (exc.hresult & 0xffffffff)) from exc

    def _task(self, folder):
        import pythoncom
        try:
            return folder.GetTask(self.name)
        except pythoncom.com_error as exc:
            # Only an actual missing task is an empty reservation. Access
            # denied, unavailable service, and corrupt data remain errors.
            code = (exc.excepinfo[5] if exc.excepinfo else exc.hresult) & 0xffffffff
            if code in (0x80070002, 0x80070003):
                return None
            raise

    def _epoch(self, task):
        import win32security
        definition = task.Definition
        actions, triggers = definition.Actions, definition.Triggers
        principal = definition.Principal.UserId
        if not principal.startswith('S-1-'):
            principal = win32security.ConvertSidToStringSid(win32security.LookupAccountName(None, principal)[0])
        if (definition.RegistrationInfo.Description != self.description or actions.Count != 1 or triggers.Count != 1
                or principal != self.sid
                or definition.Principal.LogonType != 3 or definition.Principal.RunLevel != 0
                or not definition.Settings.WakeToRun or not definition.Settings.Enabled):
            raise RuntimeError('wake task identity or policy differs; task left untouched')
        action, trigger = actions.Item(1), triggers.Item(1)
        if (action.Type != 0 or action.Path != self.executable or action.Arguments != self.arguments
                or action.WorkingDirectory != str(self.root) or trigger.Type != 1 or not trigger.Enabled
                or trigger.Repetition.Interval or trigger.RandomDelay not in ('', 'PT0S')):
            raise RuntimeError('wake task action or trigger differs; task left untouched')
        instant = datetime.fromisoformat(trigger.StartBoundary.replace('Z', '+00:00'))
        if instant.tzinfo is None:
            raise RuntimeError('wake task has no absolute timezone')
        return int(instant.timestamp())

    @_com_thread
    def read(self):
        with self._scheduler() as (_, folder):
            task = self._task(folder)
            return self._epoch(task) if task is not None else 0

    @_com_thread
    def create(self, epoch):
        with self._scheduler() as (scheduler, folder):
            if self._task(folder) is not None:
                raise RuntimeError('wake task already exists; it was not overwritten')
            definition = scheduler.NewTask(0)
            definition.RegistrationInfo.Description = self.description
            definition.Principal.UserId = self.sid
            definition.Principal.LogonType = 3  # interactive token, no stored password
            definition.Principal.RunLevel = 0
            settings = definition.Settings
            settings.Enabled = settings.WakeToRun = True
            settings.DisallowStartIfOnBatteries = settings.StopIfGoingOnBatteries = False
            settings.StartWhenAvailable = False  # a late wake is not a new alarm
            settings.MultipleInstances = 2  # ignore additional instances
            settings.ExecutionTimeLimit = 'PT2M'
            trigger = definition.Triggers.Create(1)
            trigger.Enabled = True
            trigger.StartBoundary = datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='seconds')
            action = definition.Actions.Create(0)
            action.Path, action.Arguments, action.WorkingDirectory = self.executable, self.arguments, str(self.root)
            # TASK_CREATE only. A concurrent foreign registration cannot be replaced.
            folder.RegisterTaskDefinition(self.name, definition, 2, self.sid, None, 3)
            if self._epoch(folder.GetTask(self.name)) != epoch:
                raise RuntimeError('wake task readback differs')

    @_com_thread
    def cancel(self, epoch):
        with self._scheduler() as (_, folder):
            task = self._task(folder)
            if task is None:
                return
            if self._epoch(task) != epoch:
                raise RuntimeError('wake task was changed; task left untouched')
            folder.DeleteTask(self.name, 0)
            if self._task(folder) is not None:
                raise RuntimeError('wake task cancellation not confirmed')
