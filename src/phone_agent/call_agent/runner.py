"""Run one explicitly requested outgoing call on the configured phone."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import time

from ..device.adb import BRIDGE_JAR, CONFIG, FLIP5_MODELS, device_lock
from .audio import Audio


def destination(value):
    value = re.sub(r"[\s()-]", "", value)
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", value):
        raise ValueError("Use a complete international number, such as +12025550123")
    return value


def call_states(device):
    text = device.shell("dumpsys", "telephony.registry", timeout=2)
    values = re.findall(r"(?m)^    mForegroundCallState=(\d+)\s*$", text)
    coarse = re.findall(r"(?m)^    mCallState=(\d+)\s*$", text)
    if not values or len(values) != len(coarse):
        raise RuntimeError("Could not determine per-SIM call state")
    return values, coarse


def idle(states):
    return all(value == "0" for group in states for value in group)


def load_config(path):
    path = Path(path) if path else CONFIG / "calls.json"
    try:
        config = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise RuntimeError("Call configuration missing. See examples/calls.*.json or pass --config.") from error
    if not isinstance(config, dict) or config.get("engine") not in {"elevenlabs", "pipeline"}:
        raise ValueError("Call engine must be elevenlabs or pipeline")
    return config


def save_config(path):
    config = load_config(path)
    stack = config.get("elevenlabs", {}) if config["engine"] == "elevenlabs" else config.get("pipeline", {})
    if config["engine"] == "elevenlabs":
        if not isinstance(stack.get("agent_id"), str) or not stack["agent_id"] or stack.get("asr", "scribe") not in {"agents", "scribe"}:
            raise ValueError("Provide elevenlabs.agent_id and a valid ASR mode")
    else:
        from .pipeline import Pipeline
        Pipeline(stack)
    CONFIG.mkdir(parents=True, mode=0o700, exist_ok=True)
    dest = CONFIG / "calls.json"
    with tempfile.NamedTemporaryFile(mode="w", dir=CONFIG, delete=False) as temp:
        json.dump(config, temp, ensure_ascii=False, indent=2)
        temp.write("\n")
    os.replace(temp.name, dest)
    return {"engine": config["engine"], "path": str(dest)}


def preflight(device, config):
    device.check()
    if not FLIP5_MODELS.fullmatch(device.model):
        raise RuntimeError("The cellular audio backend currently targets the Galaxy Z Flip5 family")
    device.shell("test", "-r", BRIDGE_JAR)
    if not idle(call_states(device)):
        raise RuntimeError("A call is in progress. Wait until the phone is idle.")
    if config["engine"] == "elevenlabs":
        from . import eleven
        settings = config.get("elevenlabs", {})
        if settings.get("asr", "scribe") not in {"agents", "scribe"}:
            raise ValueError("elevenlabs.asr must be agents or scribe")
        return eleven.prepare(settings)
    from .pipeline import Pipeline
    pipeline = Pipeline(config.get("pipeline", {}))
    pipeline.validate()
    return pipeline


async def composable(audio, pipeline, greeting=b""):
    if greeting:
        audio.queue(greeting)
    turns = asyncio.Queue(maxsize=1)
    generation = 0
    max_frames = min(100, int(pipeline.input_seconds * 10))

    async def listen():
        nonlocal generation
        frames = []
        quiet = 0
        voiced = 0
        async for pcm in audio.capture():
            values = tuple(v for (v,) in struct.iter_unpack("<h", pcm))
            speech = sum(v * v for v in values) >= len(values) * 250**2
            if speech or frames:
                frames.append(pcm)
                voiced += speech
                quiet = 0 if speech else quiet + 1
                if voiced == 2 and speech:
                    generation += 1
                    audio.clear()
                if quiet >= 5 or len(frames) >= max_frames:
                    if voiced >= 2:
                        if turns.full():
                            raise RuntimeError("Speech arrived faster than the configured providers could respond")
                        turns.put_nowait((generation, b"".join(frames)))
                    frames, quiet, voiced = [], 0, 0

    async def respond():
        while True:
            turn, pcm = await turns.get()
            response = await pipeline.reply(pcm)
            if turn == generation:
                audio.queue(response)

    tasks = [asyncio.create_task(listen()), asyncio.create_task(respond())]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def place_call(device, config, prepared, to, duration):
    audio = Audio()
    dialed = False
    cleaned = False
    tasks = []
    try:
        greeting = b""
        if config["engine"] == "pipeline":
            # Perform optional greeting synthesis before occupying the phone.
            greeting = await prepared.greeting()
        if not idle(await asyncio.to_thread(call_states, device)):
            raise RuntimeError("The device became busy before dialing")
        dialed = True
        await asyncio.to_thread(device.shell, "am", "start", "-a", "android.intent.action.CALL", "-d", "tel:" + to)
        deadline = time.monotonic() + 35
        last_inactive = time.monotonic()
        while time.monotonic() < deadline:
            states = await asyncio.to_thread(call_states, device)
            if "1" in states[0]:
                break
            if deadline - time.monotonic() < 32 and idle(states):
                raise RuntimeError("The call ended before it was answered")
            last_inactive = time.monotonic()
            await asyncio.sleep(0.4)
        else:
            raise RuntimeError("No answer within 35 seconds")
        stop_at = last_inactive + duration

        async def watch():
            while not idle(await asyncio.to_thread(call_states, device)):
                await asyncio.sleep(0.6)

        async def converse():
            await audio.open(device, duration)
            if config["engine"] == "elevenlabs":
                from . import eleven
                engine = eleven.drive(audio, prepared, config["elevenlabs"], config.get("language", "ko"))
            else:
                engine = composable(audio, prepared, greeting)
            engine_task = asyncio.create_task(engine)
            try:
                done, _ = await asyncio.wait([engine_task, audio.playback_task], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
            finally:
                engine_task.cancel()
                await asyncio.gather(engine_task, return_exceptions=True)

        conversation = asyncio.create_task(converse())
        watcher = asyncio.create_task(watch())
        tasks.extend([conversation, watcher])
        done, _ = await asyncio.wait(tasks, timeout=max(0, stop_at - time.monotonic()),
                                     return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        for task in tasks:
            task.cancel()
        if dialed:
            for _ in range(2):
                try:
                    if not idle(await asyncio.to_thread(call_states, device)):
                        await asyncio.to_thread(device.shell, "input", "keyevent", "KEYCODE_ENDCALL", timeout=2)
                        await asyncio.sleep(0.25)
                    if idle(await asyncio.to_thread(call_states, device)):
                        cleaned = True
                        break
                except RuntimeError:
                    continue
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await audio.close()
        if dialed and not cleaned:
            raise RuntimeError("Could not confirm the device is idle after hangup. Check the physical phone.")
    return {"result": "finished", "rx_bytes": audio.rx_bytes, "tx_bytes": audio.tx_bytes}


def execute(device, config, to=None, seconds=10):
    if not 1 <= seconds <= 300:
        raise ValueError("Call duration must be between 1 and 300 seconds")
    with device_lock(device, "calls"):
        prepared = preflight(device, config)
        if to is None:
            return {"preflight": "ok", "engine": config["engine"]}
        return asyncio.run(place_call(device, config, prepared, destination(to), seconds))
