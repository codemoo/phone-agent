"""Signed ElevenAgents sessions, optionally with separate Korean Scribe ASR."""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
import os
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import websockets


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def key_for(settings):
    env = settings.get("key_env", "ELEVENLABS_API_KEY")
    if not os.environ.get(env):
        raise RuntimeError(f"Set environment variable {env} to use ElevenLabs")
    return os.environ[env]


def prepare(settings):
    key = key_for(settings)
    agent_id = settings.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        raise ValueError("Set elevenlabs.agent_id to your private ElevenAgents agent ID")
    endpoint = "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url?" + urlencode({"agent_id": agent_id})
    try:
        with build_opener(NoRedirect).open(Request(endpoint, headers={"xi-api-key": key}), timeout=10) as reply:
            url = json.load(reply)["signed_url"]
    except (OSError, KeyError, ValueError) as error:
        raise RuntimeError("ElevenLabs signed URL request failed. Check the agent ID and key's Agents permissions.") from error
    parsed = urlsplit(url)
    if parsed.scheme != "wss" or parsed.hostname != "api.elevenlabs.io":
        raise RuntimeError("ElevenLabs supplied an unexpected WebSocket endpoint")
    return url


@asynccontextmanager
async def scribe_connection(settings, language):
    params = {"model_id": "scribe_v2_realtime", "audio_format": "pcm_16000",
              "commit_strategy": "vad", "vad_silence_threshold_secs": 0.3,
              "vad_threshold": 0.4, "min_speech_duration_ms": 100, "min_silence_duration_ms": 100,
              "language_code": language}
    address = "wss://api.elevenlabs.io/v1/speech-to-text/realtime?" + urlencode(params)
    async with websockets.connect(address, additional_headers={"xi-api-key": key_for(settings)},
                                  open_timeout=4, close_timeout=1) as ws:
        started = json.loads(await asyncio.wait_for(ws.recv(), 3))
        if started.get("message_type") != "session_started":
            raise RuntimeError("ElevenLabs Scribe rejected session startup")
        yield ws


async def drive(audio, url, settings, language):
    async with websockets.connect(url, open_timeout=4, close_timeout=1, max_size=4 * 1024**2) as ws:
        await ws.send(json.dumps({"type": "conversation_initiation_client_data"}))
        ready = asyncio.Event()
        cutoff = -1

        async def receive():
            nonlocal cutoff
            async for raw in ws:
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "conversation_initiation_metadata":
                    data = message["conversation_initiation_metadata_event"]
                    if (data.get("user_input_audio_format"), data.get("agent_output_audio_format")) != ("pcm_16000", "pcm_16000"):
                        raise RuntimeError("Set the ElevenAgents input and output formats to pcm_16000")
                    ready.set()
                elif kind == "ping":
                    await ws.send(json.dumps({"type": "pong", "event_id": message["ping_event"]["event_id"]}))
                elif kind == "interruption":
                    cutoff = max(cutoff, message["interruption_event"]["event_id"])
                    audio.clear()
                elif kind == "audio" and ready.is_set():
                    event = message["audio_event"]
                    if event.get("event_id", 0) > cutoff:
                        audio.queue(base64.b64decode(event["audio_base_64"], validate=True))
                elif kind in {"client_error", "error", "conversation_error"}:
                    raise RuntimeError("ElevenAgents reported a conversation error")
            raise RuntimeError("ElevenAgents WebSocket closed")

        async def send(scribe=None):
            await asyncio.wait_for(ready.wait(), 4)
            async for pcm in audio.capture():
                payload = base64.b64encode(pcm).decode()
                if scribe:
                    msg = {"message_type": "input_audio_chunk", "audio_base_64": payload,
                           "commit": False, "sample_rate": 16000}
                else:
                    msg = {"user_audio_chunk": payload}
                await asyncio.wait_for((scribe or ws).send(json.dumps(msg)), 2)

        async def transcribe(scribe):
            async for raw in scribe:
                message = json.loads(raw)
                kind = message.get("message_type")
                if kind == "committed_transcript":
                    text = message.get("text", "").strip()
                    if text:
                        audio.clear()
                        await asyncio.wait_for(ws.send(json.dumps({"type": "user_message", "text": text})), 2)
                elif kind not in {"partial_transcript", "committed_transcript_with_timestamps"}:
                    raise RuntimeError(f"Scribe reported {kind}")
            raise RuntimeError("Scribe WebSocket closed")

        async def concurrent(*coroutines):
            tasks = [asyncio.create_task(coro) for coro in coroutines]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        if settings.get("asr", "scribe") == "scribe":
            async with scribe_connection(settings, language) as scribe:
                await concurrent(receive(), send(scribe), transcribe(scribe))
        else:
            await concurrent(receive(), send())
