"""Phone Agent command line entry point."""
import argparse
import json
import os
import shutil
import sys

from .device.adb import devices, doctor, load_device, save_profile


def add_profile(parser):
    parser.add_argument("--profile", default="default", help="Saved device profile")


def main():
    parser = argparse.ArgumentParser(prog="phone-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    device = commands.add_parser("device", help="Configure an ADB device after installation")
    d = device.add_subparsers(dest="action", required=True)
    listing = d.add_parser("list", help="List devices reported by ADB")
    setup = d.add_parser("setup", help="Save an authorized Android device profile")
    for cmd in (listing, setup):
        cmd.add_argument("--adb", default="adb", help="ADB executable (defaults to PATH)")
    add_profile(setup)
    setup.add_argument("--serial", help="Choose a serial when multiple devices are connected")
    status = d.add_parser("doctor", help="Read device status")
    add_profile(status)
    status.add_argument("--calls", action="store_true", help="Check cellular audio prerequisites")

    apps = commands.add_parser("apps", help="Serve the Android app tools over stdio MCP")
    add_profile(apps)

    calls = commands.add_parser("call", help="Run the separate cellular call agent")
    c = calls.add_subparsers(dest="action", required=True)
    bridge = c.add_parser("install-bridge", help="Build the Android audio helper and install it via ADB")
    add_profile(bridge)
    bridge.add_argument("--sdk", default=os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME"))
    configure = c.add_parser("setup", help="Register a call provider configuration on this host")
    configure.add_argument("--config", required=True, help="Path to call provider JSON")
    for action in ("check", "dial"):
        cmd = c.add_parser(action)
        add_profile(cmd)
        cmd.add_argument("--config", help="Path to call provider JSON")
        if action == "dial":
            cmd.add_argument("--to", required=True, help="Destination in international format")
            cmd.add_argument("--seconds", type=int, default=10, help="Connected call limit (default: 10)")

    args = parser.parse_args()
    try:
        if args.command == "device" and args.action == "list":
            result = devices(args.adb)
        elif args.command == "device" and args.action == "setup":
            adb = shutil.which(args.adb)
            if not adb:
                raise RuntimeError("Install Android Platform Tools or specify --adb")
            result = save_profile(args.profile, adb, args.serial)
        elif args.command == "call" and args.action == "setup":
            from .call_agent.runner import save_config
            result = save_config(args.config)
        else:
            target = load_device(args.profile)
            if args.command == "device":
                result = doctor(target, calls=args.calls)
            elif args.command == "apps":
                from .app_control.mcp_server import serve
                serve(target)
                return 0
            elif args.action == "install-bridge":
                from .device.build import install_bridge
                if not args.sdk:
                    raise ValueError("Set ANDROID_SDK_ROOT or supply --sdk")
                result = install_bridge(target, args.sdk)
            else:
                from .call_agent.runner import execute, load_config
                result = execute(target, load_config(args.config), getattr(args, "to", None),
                                 getattr(args, "seconds", 10))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ImportError, KeyError, OSError, RuntimeError, ValueError) as error:
        print(f"phone-agent: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
