"""ADB selection and on-host device settings, shared by independent entry points."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "phone-agent"
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "phone-agent"
BRIDGE_JAR = "/data/local/tmp/phone-agent-audio-bridge.jar"
FLIP5_MODELS = re.compile(r"SM-F731[A-Z0-9]*$")
SERIAL = re.compile(r"[a-zA-Z0-9_.:\[\]-]+")


def run(argv, timeout=12):
    try:
        # ADB shell otherwise forwards stdin, consuming the MCP handshake
        # when device validation runs before the stdio server starts.
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=timeout)
    except FileNotFoundError as error:
        raise RuntimeError(f"Executable not found: {argv[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{Path(argv[0]).name} timed out") from error
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"{Path(argv[0]).name} failed: {error.stderr.decode(errors='replace')[:240]}") from error


@dataclass(frozen=True)
class Device:
    serial: str
    model: str
    adb: str = "adb"

    @property
    def prefix(self):
        return [self.adb, "-s", self.serial]

    def shell(self, *argv, timeout=12):
        return run([*self.prefix, "shell", shlex.join(argv)], timeout).stdout.decode(errors="replace").strip()

    def check(self):
        model = self.shell("getprop", "ro.product.model", timeout=5)
        if model != self.model:
            raise RuntimeError("The selected device model changed. Run device setup again.")


def devices(adb="adb"):
    lines = run([adb, "devices", "-l"]).stdout.decode(errors="replace").splitlines()
    return [{"serial": f[0], "state": f[1]} for line in lines
            if not line.startswith("List of") and len(f := line.split()) >= 2]


def profile_path(profile):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", profile):
        raise ValueError("Profile name must contain letters, numbers, underscores or hyphens")
    return CONFIG / "devices" / f"{profile}.json"


def save_profile(profile, adb, serial):
    found = [item["serial"] for item in devices(adb) if item["state"] == "device"]
    if serial is None:
        if len(found) != 1:
            raise RuntimeError("Select an authorized device with --serial (see device list)")
        serial = found[0]
    if serial not in found or not SERIAL.fullmatch(serial):
        raise RuntimeError("That ADB device is unavailable or unauthorized")
    model = Device(serial, "", adb).shell("getprop", "ro.product.model")
    if not model:
        raise RuntimeError("ADB did not report a device model")
    path = profile_path(profile)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temp:
        json.dump({"serial": serial, "model": model, "adb": adb}, temp, indent=2)
        temp.write("\n")
    os.replace(temp.name, path)
    return {"profile": profile, "model": model, "path": str(path)}


def load_device(profile):
    try:
        data = json.loads(profile_path(profile).read_text())
    except FileNotFoundError as error:
        raise RuntimeError(f"Device profile '{profile}' is missing. Run phone-agent device setup.") from error
    device = Device(**data)
    if not SERIAL.fullmatch(device.serial):
        raise ValueError("Invalid ADB serial in device profile")
    return device


def lock_path(device, kind):
    ident = hashlib.sha256(device.serial.encode()).hexdigest()[:24]
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    return STATE / f"{kind}-{ident}.lock"


@contextmanager
def device_lock(device, kind):
    with lock_path(device, kind).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"A {kind} operation already owns this device") from error
        yield


def doctor(device, calls=False):
    device.check()
    power = device.shell("dumpsys", "power")
    wakefulness = re.search(r"(?m)^\s*mWakefulness=(\w+)", power)
    policy = device.shell("dumpsys", "window", "policy")
    result = {
        "model": device.model,
        "android": device.shell("getprop", "ro.build.version.release"),
        "wakefulness": wakefulness[1] if wakefulness else "unknown",
        "keyguard_showing": bool(re.search(r"(?m)^\s*showing=true\s*$", policy)),
    }
    if calls:
        result["flip5_audio_backend"] = bool(FLIP5_MODELS.fullmatch(device.model))
        result["audio_bridge_installed"] = bridge_exists(device)
        result["active_audio_route_tested"] = False
    return result


def bridge_exists(device):
    try:
        device.shell("test", "-r", BRIDGE_JAR)
    except RuntimeError:
        return False
    return True
