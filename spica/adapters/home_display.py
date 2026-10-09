"""Constrained X11 DPMS: require the configured physical monitor alone.

X11 DPMS is session-wide. Refuse multi-output layouts rather than waking other
displays. EDID identity and active output are rechecked for every visit.
"""
import re
import subprocess

from spica.home.models import DisplayResult


class HomeDisplay:
    def __init__(self, config, *, run=subprocess.run):
        self.config, self._run = config, run

    def _command(self, *args):
        result = self._run(args, env={"DISPLAY": self.config.display,
                          "XAUTHORITY": self.config.xauthority}, capture_output=True,
                          text=True, timeout=2, check=False)
        if result.returncode:
            raise RuntimeError("X11 display command unavailable")
        return result.stdout

    def check_target(self):
        if not all((self.config.display, self.config.xauthority, self.config.monitor_output,
                    self.config.monitor_name)):
            raise RuntimeError("Home display is not configured")
        outputs = self._command("/usr/bin/xrandr", "--props")
        active = re.findall(r"^(\S+) connected (?:primary )?\d+x\d+\+\d+\+\d+", outputs, re.M)
        if active != [self.config.monitor_output]:
            raise RuntimeError("configured monitor must be the only active X11 output")
        sections = re.split(r"(?=^\S+ (?:dis)?connected)", outputs, flags=re.M)
        target = next((s for s in sections if s.startswith(self.config.monitor_output+" connected ")), "")
        edid = re.search(r"\bEDID:\s*\n((?:[ \t]+[0-9a-fA-F]{32}\n)+)", target)
        if edid is None:
            raise RuntimeError("monitor EDID unavailable")
        raw = bytes.fromhex(edid.group(1))
        names = []
        for start in (54, 72, 90, 108):
            block = raw[start:start+18]
            if block[:5] == b"\x00\x00\x00\xfc\x00":
                names.append(block[5:].decode("ascii", errors="replace").strip())
        if names != [self.config.monitor_name]:
            raise RuntimeError("monitor identity differs from Home calibration")

    def wake(self):
        return self._set_power(True)

    def blank(self):
        return self._set_power(False)

    def _set_power(self, on):
        try:
            self.check_target()
            state = self._command("/usr/bin/xset", "q")
            if f"Monitor is {'On' if on else 'Off'}" in state:
                return DisplayResult("already_on" if on else "already_off")
            if not any(f"Monitor is {x}" in state for x in ("On", "Off", "Standby", "Suspend")):
                return DisplayResult("unavailable", "DPMS state unknown")
            self._command("/usr/bin/xset", "dpms", "force", "on" if on else "off")
            return DisplayResult("wake_requested" if on else "blank_requested", "physical light still requires observation")
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return DisplayResult("unavailable", str(exc))
