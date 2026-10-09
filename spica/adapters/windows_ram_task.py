"""Unelevated client for two administrator-installed, fixed RAM RGB tasks."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import stat
import time
import uuid

from spica.adapters.windows_wake_task import WindowsWakeTask, _com_thread


class WindowsRamCleanupPending(RuntimeError):
    """The fixed task's OS lifetime has not reached a verified terminal state."""
    def __init__(self, reason, owner):
        super().__init__(reason)
        self.owner = owner


class WindowsRamTask:
    def __init__(self):
        import win32api
        from win32com.shell import shell, shellcon
        owner = WindowsWakeTask()
        self.root, self.sid = owner.root, owner.sid
        self.identity = hashlib.sha256((str(self.root).casefold()+'\n'+self.sid).encode()).hexdigest()[:16]
        self.base = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_COMMON_APPDATA, 0, 0))/'SpicaWindowsRgb'
        self.state = self.base/self.identity
        self.program = str(Path(win32api.GetSystemDirectory())/'WindowsPowerShell/v1.0/powershell.exe')
        self.owner = owner

    def _name(self, mode):
        return 'Spica-RAM-'+('On' if mode == 'on' else 'Off')+'-'+self.identity

    def _acl(self, descriptor):
        import win32security
        admins = {'S-1-5-18', 'S-1-5-32-544'}
        if win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner()) not in admins:
            raise PermissionError('RAM protected object is not owned by SYSTEM/Administrators')
        acl = descriptor.GetSecurityDescriptorDacl()
        if acl is None:
            raise PermissionError('RAM protected object has no private DACL')
        for index in range(acl.GetAceCount()):
            ace = acl.GetAce(index)
            kind, flags = ace[0]
            if flags & 8 or kind == 1:
                continue
            if kind != 0:
                raise PermissionError('RAM object has an unsupported access rule')
            mask, sid = ace[1] & 0xffffffff, win32security.ConvertSidToStringSid(ace[2])
            if sid in admins:
                continue
            # Files use expanded ReadAndExecute; Task Scheduler may retain
            # GENERIC_READ/GENERIC_EXECUTE bits in its security descriptor.
            if sid != self.sid or mask & ~(0xA0000000 | 0x1200A9):
                raise PermissionError('RAM object is writable by an unprivileged or foreign principal')

    def _protected(self, path, *, directory=False):
        import win32security
        info = path.lstat()
        if (getattr(info, 'st_file_attributes', 0) & 0x400
                or stat.S_ISDIR(info.st_mode) != directory
                or not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
            raise PermissionError('RAM deployment contains a link or unexpected object')
        descriptor = win32security.GetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION)
        self._acl(descriptor)

    def _deployment(self):
        self._protected(self.base, directory=True)
        self._protected(self.state, directory=True)
        path = self.state/'deployment.json'
        self._protected(path)
        document = json.loads(path.read_text(encoding='utf-8'))
        if (document['identity'] != self.identity or document['owner_sid'] != self.sid
                or Path(document['installation_root']) != self.root
                or document['tasks'] != {mode: self._name(mode) for mode in ('off', 'on')}):
            raise PermissionError('RAM installation identity differs')
        for relative, digest in document['files'].items():
            if Path(relative).is_absolute() or '..' in Path(relative).parts:
                raise PermissionError('RAM deployment path escapes its protected directory')
            file = self.state/relative
            for parent in file.parents:
                if parent == self.state:
                    break
                self._protected(parent, directory=True)
            self._protected(file)
            if hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                raise PermissionError('RAM protected deployment content changed')
        self._protected(self.state/'results', directory=True)

    def _task(self, folder, mode):
        import win32security
        task = folder.GetTask(self._name(mode))
        self._acl(win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(task.GetSecurityDescriptor(5), 1))
        definition = task.Definition
        principal = definition.Principal.UserId
        if not principal.startswith('S-1-'):
            principal = win32security.ConvertSidToStringSid(win32security.LookupAccountName(None, principal)[0])
        description = 'Spica protected RAM v1\n'+str(self.root).casefold()+'\n'+self.sid+'\n'+mode
        if (definition.RegistrationInfo.Description != description or principal != 'S-1-5-18'
                or definition.Principal.LogonType != 5 or definition.Principal.RunLevel != 1
                or definition.Triggers.Count != 0 or definition.Actions.Count != 1
                or not definition.Settings.Enabled or definition.Settings.WakeToRun
                or definition.Settings.MultipleInstances != 2
                or definition.Settings.ExecutionTimeLimit != 'PT15S'
                or not definition.Settings.AllowHardTerminate):
            raise PermissionError('RAM task identity or execution policy differs')
        action = definition.Actions.Item(1)
        # Match the installer's literal argument string, including its explicit
        # quoting. No task arguments are supplied through Run().
        arguments = ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "'
                     + str(self.state/'run_ram_task.ps1') + '" -Mode ' + mode)
        if (action.Type != 0 or action.Path != self.program or action.Arguments != arguments
                or action.WorkingDirectory != str(self.state)):
            raise PermissionError('RAM task action differs')
        return task

    @contextmanager
    def _request_lock(self):
        import pywintypes
        import win32event
        import win32security
        descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            'D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;'+self.sid+')', 1)
        attributes = pywintypes.SECURITY_ATTRIBUTES()
        attributes.SECURITY_DESCRIPTOR = descriptor
        mutex = win32event.CreateMutex(attributes, False, 'Global\\Spica-RAM-Request-'+self.identity)
        owned = False
        try:
            security = win32security.GetSecurityInfo(mutex, win32security.SE_KERNEL_OBJECT,
                win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION)
            if win32security.ConvertSidToStringSid(security.GetSecurityDescriptorOwner()) not in {self.sid, 'S-1-5-18', 'S-1-5-32-544'}:
                raise PermissionError('RAM request mutex has a foreign owner')
            acl = security.GetSecurityDescriptorDacl()
            if acl is None:
                raise PermissionError('RAM request mutex is not private')
            for index in range(acl.GetAceCount()):
                ace = acl.GetAce(index)
                if ace[0][0] != 0 or win32security.ConvertSidToStringSid(ace[2]) not in {self.sid, 'S-1-5-18', 'S-1-5-32-544'}:
                    raise PermissionError('RAM request mutex grants foreign access')
            result = win32event.WaitForSingleObject(mutex, 0)
            owned = result in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED)
            if not owned:
                raise RuntimeError('另一个 Windows RAM 灯效请求仍在执行')
            yield
        finally:
            if owned:
                win32event.ReleaseMutex(mutex)
            mutex.Close()

    @staticmethod
    def _instances(task):
        return {str(item.InstanceGuid).casefold() for item in task.GetInstances(0)}

    def _idle(self, tasks):
        # UNKNOWN/disabled/queued is not a terminal receipt. Check every
        # instance, including an unexpected newer one, before releasing RGB.
        return all(task.State == 3 and not self._instances(task) for task in tasks.values())

    def _stop_and_wait(self, folder, tasks, mode, owner, started):
        try:
            self._task(folder, mode).Stop(0)
        except Exception:
            # The administrator-owned task also has a verified 15-second
            # execution limit. Its expiry is a reason to recheck, not proof.
            pass
        deadline = max(started + 18, time.perf_counter() + 3)
        while True:
            try:
                idle = self._idle(tasks)
                if idle:
                    tasks = {item: self._task(folder, item) for item in ('off', 'on')}
                    idle = self._idle(tasks)
            except Exception:
                idle = False
            if idle:
                raise RuntimeError('RAM 灯效请求已中止；已发送的灯效结果未确认')
            if time.perf_counter() >= deadline:
                raise WindowsRamCleanupPending('RAM 任务清理待确认；未继续其它灯控', owner)
            time.sleep(.05)

    @_com_thread
    def cleanup_settled(self, owner):
        if (owner.get('installation_identity') != self.identity
                or not isinstance(owner.get('instances'), dict)
                or not owner['instances'] or set(owner['instances']) - {'off', 'on'}
                or any(not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values)
                       for values in owner['instances'].values())):
            raise PermissionError('RAM cleanup owner differs from this installation')
        for values in owner['instances'].values():
            for value in values:
                uuid.UUID(value)
        with self._request_lock():
            self._deployment()
            with self.owner._scheduler() as (_, folder):
                tasks = {mode: self._task(folder, mode) for mode in ('off', 'on')}
                # Only certify an idle resource, never the old command's
                # effect. Missing/replaced tasks and foreign instances fail
                # closed, even if a stale success receipt is still on disk.
                return self._idle(tasks)

    @_com_thread
    def set(self, enabled, cancelled=None):
        mode = 'on' if enabled else 'off'
        with self._request_lock():
            self._deployment()
            with self.owner._scheduler() as (_, folder):
                tasks = {item: self._task(folder, item) for item in ('off', 'on')}
                try:
                    idle = self._idle(tasks)
                    instances = {} if idle else {item: sorted(self._instances(task)) for item, task in tasks.items()}
                except Exception as exc:
                    # These tasks are bound, but an existing OS lifetime is
                    # unknown. No Run happened here, so never Stop it.
                    raise WindowsRamCleanupPending('Windows RAM 任务状态未确认；未提交另一个请求',
                        dict(installation_identity=self.identity, instances={item: [] for item in tasks})) from exc
                if not idle:
                    owner = dict(installation_identity=self.identity, instances=instances)
                    raise WindowsRamCleanupPending('Windows RAM 灯效任务仍在收口；未提交另一个请求', owner)
                if cancelled is not None and cancelled.is_set():
                    return False
                task = tasks[mode]
                started = time.time()
                started_mono = time.perf_counter()
                try:
                    running = task.Run(None)
                    value = running.InstanceGuid
                    if not isinstance(value, str):
                        raise ValueError('RAM task instance identity is unavailable')
                    uuid.UUID(value)
                    instance = value.casefold()
                except Exception:
                    self._stop_and_wait(folder, tasks, mode,
                        dict(installation_identity=self.identity, instances={mode: []}), started_mono)
                owner = dict(installation_identity=self.identity, instances={mode: [instance]})
                deadline = started_mono+14
                while True:
                    try:
                        idle = self._idle(tasks)
                    except Exception:
                        self._stop_and_wait(folder, tasks, mode, owner, started_mono)
                    if idle:
                        break
                    if (cancelled is not None and cancelled.is_set()) or time.perf_counter() >= deadline:
                        # Its immutable helper contains every native child in a
                        # kill-on-close Job. Stopping the task closes that Job.
                        self._stop_and_wait(folder, tasks, mode, owner, started_mono)
                    time.sleep(.05)
                result_path = self.state/'results'/(mode+'.json')
                self._protected(result_path)
                result = json.loads(result_path.read_text(encoding='utf-8'))
                if (str(result.get('instance_guid', '')).casefold() != instance or result.get('mode') != mode
                        or result.get('started_at', 0) < started-.25
                        or result.get('observed_modes') != (['Rainbow', 'Rainbow'] if enabled else ['Off', 'Off'])
                        or result.get('status') != 'requested' or task.LastTaskResult != 0):
                    raise RuntimeError('RAM 本次任务没有匹配的成功回执：'+str(result.get('reason', '结果未知')))
                return True
