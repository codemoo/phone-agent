# Phone Agent

Run a cellular call agent on a USB-connected Galaxy Z Flip5, or give another
agent Android app controls through MCP. Each entry point starts independently.
Both use an Android device profile created after installation.

| Function | Entry point | Install extra |
| --- | --- | --- |
| Configure and inspect a device | `phone-agent device` | Base package |
| Control Android apps from another agent | `phone-agent apps` | `apps` |
| Run a cellular voice conversation | `phone-agent call` | `calls` |
| Run speech recognition on the host | Faster-Whisper provider | `local-speech` |

## Install and set up a phone

Use Python 3.10 or newer on Linux with Android Platform Tools. Enable USB
debugging on your phone and approve the host's ADB key. After cloning:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install '.[apps,calls]'
phone-agent device list
phone-agent device setup
phone-agent device doctor
```

If more than one authorized device is connected, run
`phone-agent device setup --serial YOUR_ADB_SERIAL --profile my-phone`.
Pass `--profile my-phone` to the relevant command. Profiles are stored in
`${XDG_CONFIG_HOME:-$HOME/.config}/phone-agent/devices/`. ADB commands stay
pinned to the selected serial and model. The setup command records a profile
and leaves the Android lock screen and USB debugging policy as configured by
the device owner.

## App control

Add a stdio MCP server to the agent that will operate your phone. Use the
[local example](examples/mcp.local.json) if it runs on the ADB host, or the
[SSH example](examples/mcp.ssh.json) if it runs elsewhere. Replace the host
alias and executable path with your own. SSH must already authenticate and
recognize the host key. Connect trusted agents to this interface since it can
operate the foreground app.

| MCP tool | Purpose |
| --- | --- |
| `get_device_status` | Read the phone model and screen and keyguard state |
| `begin_app_work`, `end_app_work` | Reserve and release app UI access for the client's multi-step task |
| `launch_app_activity` | Launch an installed app by its Android package |
| `analyze_screen` | Read visible text, resource IDs and logical coordinates |
| `capture_screen` | Get a PNG image for screens with sparse UI data |
| `interact_with_screen` | Tap, swipe, send supported keys, or enter ASCII text |

For example, wake the screen with
`interact_with_screen(action="key", params={"keycode":"wake"})`, launch an
app, and analyze the current screen before choosing a tap. The
[agent integration guide](AGENTS.md) describes the tool contract. App-specific
skills live under the operator's local `skills/` directory.

## Cellular calls

The Android audio helper currently targets the Galaxy Z Flip5 family. The
underlying audio route was exercised on a Korean Z Flip5 running Android 16.
Other firmware and carrier combinations need their own checks. App control
also works on compatible Android devices with ADB and a readable UI hierarchy.
The call agent currently supports explicit outgoing calls.

Install JDK 17+ and Android SDK platform 36 with build-tools and `d8`. Set
`JAVA_HOME` and `ANDROID_SDK_ROOT` when needed. Provision the helper:

```sh
phone-agent call install-bridge --sdk "$ANDROID_SDK_ROOT"
phone-agent device doctor --calls
```

Select a call configuration file:

- [ElevenAgents](examples/calls.elevenlabs.json) uses an existing private agent
  with `pcm_16000` audio in both directions and a signed WebSocket. Use either
  the agent's speech recognition or standalone Scribe.
- [Local pipeline](examples/calls.local.json) uses Faster-Whisper, a local
  OpenAI-compatible chat endpoint, and Piper. Install `.[local-speech]` and
  provision the models and Piper executable. Use a voice that supports your
  chosen language.
- [Online pipeline](examples/calls.online.json) uses OpenAI-compatible HTTP
  speech and chat endpoints. Each of STT, LLM, and TTS can have its own API
  root, model, and credential environment variable.

Pass `--config PATH` for an individual command or register it with
`phone-agent call setup --config PATH`. Export any credential references
used by the chosen configuration.

```sh
phone-agent call setup --config /path/to/calls.json
phone-agent call check
phone-agent call dial --to +12025550123 --seconds 10
```

Use a destination you control for the first test. `call check` validates the
installed bridge and provider settings without dialing. On the active call,
the helper supplies 16 kHz PCM for capture and routes playback to telephony
TX with 700 ms of initial silence. Calls default to a 10-second test limit.
If USB disconnects midcall, end the call on the phone. See the
[call configuration guide](docs/calls.md) for provider details and limits.

Source code is licensed under Apache-2.0. See
[third-party notices](THIRD_PARTY_NOTICES.md) for phone-mcp attribution.
