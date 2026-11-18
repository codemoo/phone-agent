"""Compile and install the Flip5 audio helper with the Android SDK."""
from pathlib import Path
import re
import tempfile
import zipfile

from .adb import BRIDGE_JAR, FLIP5_MODELS, run


def install_bridge(device, sdk):
    device.check()
    if not FLIP5_MODELS.fullmatch(device.model):
        raise RuntimeError("The cellular audio bridge currently supports the Galaxy Z Flip5 family")
    sdk = Path(sdk).expanduser()
    platform = sdk / "platforms/android-36/android.jar"
    if not platform.is_file():
        raise RuntimeError("Install Android SDK platform android-36 (compile SDK)")
    candidates = sorted(
        (tuple(map(int, path.name.split("."))), path / "d8")
        for path in (sdk / "build-tools").glob("*")
        if re.fullmatch(r"\d+\.\d+\.\d+", path.name) and (path / "d8").is_file()
    )
    if not candidates:
        raise RuntimeError("Install Android SDK build-tools with d8")
    source = Path(__file__).parents[1] / "call_agent/android/PhoneAudioBridge.java"
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        run(["javac", "--release", "8", "-cp", str(platform), "-d", directory, str(source)], 45)
        classes = sorted(map(str, temp.glob("*.class")))
        run([str(candidates[-1][1]), "--lib", str(platform), "--output", directory, *classes], 60)
        jar = temp / "phone-audio-bridge.jar"
        with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(temp / "classes.dex", "classes.dex")
        run([*device.prefix, "push", str(jar), BRIDGE_JAR], 25)
        device.shell("test", "-r", BRIDGE_JAR)
    return {"model": device.model, "audio_bridge_installed": True}
