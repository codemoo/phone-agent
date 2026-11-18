"""App control for trusted MCP clients over stdio."""
import asyncio
import fcntl
import json
import re
from typing import Any
import uuid
import xml.etree.ElementTree as ET

from mcp.server.fastmcp import FastMCP, Image
from phone_mcp.tools.screen_interface import UIElement

from ..device.adb import doctor, lock_path, run


class ScreenElement(UIElement):
    """Adapt phone-mcp's UI representation to Android's adjacent coordinate pairs."""

    def __init__(self, node):
        attrs = node.attrib
        bounds = attrs.get("bounds", "")
        super().__init__({
            "text": "" if attrs.get("password") == "true" else attrs.get("text", ""),
            "content_desc": attrs.get("content-desc", ""),
            "resource_id": attrs.get("resource-id", ""),
            "class_name": attrs.get("class", ""),
            "bounds": "",
            "clickable": attrs.get("clickable") == "true",
        })
        self.bounds = bounds
        values = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds)
        if values:
            x1, y1, x2, y2 = map(int, values.groups())
            self.center_x, self.center_y = (x1 + x2) // 2, (y1 + y2) // 2
        self.package = attrs.get("package", "")
        self.enabled = attrs.get("enabled") == "true"
        self.scrollable = attrs.get("scrollable") == "true"

    def to_dict(self):
        return {**super().to_dict(), "package": self.package, "enabled": self.enabled,
                "scrollable": self.scrollable}


def logical_size(device):
    output = device.shell("dumpsys", "window", "displays")
    display = re.search(r"(?m)^\s*Display: mDisplayId=0\b[^\n]*\n[ \t]*[^\n]*\bcur=(\d+)x(\d+)", output)
    if not display:
        raise RuntimeError("Could not determine the main display's current size")
    return tuple(map(int, display.groups()))


def snapshot(device, max_elements):
    path = f"/data/local/tmp/phone-agent-ui-{uuid.uuid4().hex}.xml"
    try:
        device.shell("uiautomator", "dump", path)
        root = ET.fromstring(device.shell("cat", path))
    finally:
        try:
            device.shell("rm", "-f", path)
        except RuntimeError:
            pass
    width, height = logical_size(device)
    return {"width": width, "height": height,
            "elements": [ScreenElement(node).to_dict() for node in root.iter("node")][:max_elements]}


def screenshot(device):
    output = device.shell("dumpsys", "SurfaceFlinger", "--display-id")
    match = re.search(r"(?m)^Display (\d+) \(HWC display 0\)", output)
    args = ["-d", match[1]] if match else []
    data = run([*device.prefix, "exec-out", "screencap", *args, "-p"]).stdout
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Screen capture did not return PNG. Check that the main display is on.")
    return Image(data=data, format="png")


def action_on_screen(device, action, params):
    if action == "key":
        key = params.get("keycode")
        keys = {"wake": "KEYCODE_WAKEUP", "sleep": "KEYCODE_SLEEP", "home": "KEYCODE_HOME",
                "back": "KEYCODE_BACK", "enter": "KEYCODE_ENTER", "tab": "KEYCODE_TAB",
                "delete": "KEYCODE_DEL"}
        if key not in keys:
            raise ValueError("Supported keys: " + ", ".join(keys))
        device.shell("input", "keyevent", keys[key])
        return
    if action == "text":
        value = params.get("content", "")
        if not isinstance(value, str) or not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value) or "%s" in value or len(value) > 500:
            raise ValueError("ADB text entry supports up to 500 printable ASCII characters; use app UI for other text")
        if value:
            device.shell("input", "text", value.replace(" ", "%s"))
        return
    if action not in {"tap", "swipe"}:
        raise ValueError("Supported actions: tap, swipe, key, text")
    names = ("x", "y") if action == "tap" else ("x1", "y1", "x2", "y2")
    values = [params.get(name) for name in names]
    width, height = logical_size(device)
    if not all(type(v) is int and 0 <= v < (width, height)[i % 2] for i, v in enumerate(values)):
        raise ValueError("Coordinates must lie on the current main display")
    if action == "swipe":
        duration = params.get("duration", 350)
        if type(duration) is not int or not 1 <= duration <= 5000:
            raise ValueError("Invalid swipe duration")
        values.append(duration)
    device.shell("input", action, *map(str, values))


def serve(device):
    device.check()
    mcp = FastMCP("phone-agent-apps")
    lock = asyncio.Lock()
    owner = None

    def acquire():
        handle = lock_path(device, "apps").open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise RuntimeError("Another MCP client is using this device's app UI") from error
        return handle

    async def access(operation, *args):
        async with lock:
            handle = None if owner else acquire()
            try:
                await asyncio.to_thread(device.check)
                work = asyncio.create_task(asyncio.to_thread(operation, device, *args))
                try:
                    return await asyncio.shield(work)
                except asyncio.CancelledError:
                    await asyncio.gather(work, return_exceptions=True)
                    raise
            finally:
                if handle:
                    handle.close()

    @mcp.tool(structured_output=True)
    async def begin_app_work() -> dict[str, Any]:
        """Reserve the app UI for this stdio client across multiple tool calls."""
        nonlocal owner
        async with lock:
            if owner is None:
                owner = acquire()
        return {"reserved": True}

    @mcp.tool(structured_output=True)
    async def end_app_work() -> dict[str, Any]:
        """Release this client's app UI reservation."""
        nonlocal owner
        async with lock:
            if owner:
                owner.close()
                owner = None
        return {"reserved": False}

    @mcp.tool(structured_output=True)
    async def get_device_status() -> dict[str, Any]:
        """Get selected Android device model, screen state and keyguard status."""
        return await asyncio.to_thread(doctor, device)

    @mcp.tool(structured_output=True)
    async def launch_app_activity(package_name: str) -> dict[str, Any]:
        """Open an installed package's launcher activity."""
        if not re.fullmatch(r"[a-zA-Z][\w]*(?:\.[a-zA-Z][\w]*)+", package_name, re.ASCII):
            raise ValueError("Invalid Android package name")

        def launch(target):
            lines = target.shell("cmd", "package", "resolve-activity", "--brief", "-a",
                                     "android.intent.action.MAIN", "-c",
                                     "android.intent.category.LAUNCHER", package_name).splitlines()
            component = lines[-1] if lines else ""
            if not re.fullmatch(re.escape(package_name) + r"/[a-zA-Z0-9_.$]+", component):
                raise RuntimeError("No launcher activity for this package")
            output = target.shell("am", "start", "-W", "-n", component)
            if "Error:" in output or "Status: ok" not in output:
                raise RuntimeError("Android did not confirm launching the app")
            return {"package": package_name, "component": component}

        return await access(launch)

    @mcp.tool(structured_output=True)
    async def analyze_screen(max_elements: int = 150) -> dict[str, Any]:
        """Read visible UI text, resource IDs, and logical screen bounds."""
        if not 1 <= max_elements <= 400:
            raise ValueError("max_elements must be between 1 and 400")
        return await access(snapshot, max_elements)

    @mcp.tool(structured_output=False)
    async def capture_screen() -> Image:
        """Capture the main screen as a PNG when UI text is insufficient."""
        return await access(screenshot)

    @mcp.tool(structured_output=True)
    async def interact_with_screen(action: str, params: dict) -> dict[str, Any]:
        """Apply a tap, swipe, supported key, or ASCII text to the current screen."""
        await access(action_on_screen, action, params)
        return {"applied": True}

    try:
        mcp.run(transport="stdio")
    finally:
        if owner:
            owner.close()
