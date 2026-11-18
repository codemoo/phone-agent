"""Composable, hardware-independent speech/chat pipeline (Python 3.10+).

Input and output are headerless little-endian mono PCM16 at 16 kHz. The
constructor checks config shape; validate() checks local dependencies and keys.
Heavy STT model loading is deferred until the first reply. No recordings are retained.
Provider sections: stt.provider = faster-whisper|openai, llm.provider = openai,
tts.provider = piper|openai. An omitted key_env permits keyless local endpoints.
"""
from __future__ import annotations

import asyncio
from array import array
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave


class PipelineError(RuntimeError):
    """A provider or local runtime could not complete an operation."""


class ConfigurationError(ValueError):
    """Pipeline settings or a required runtime dependency are invalid."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an Authorization header, audio, or conversation elsewhere.
        raise PipelineError("Provider redirects are disabled; configure the final base_url")


def _text(value: Any, name: str, *, default: str | None = None, limit: int = 8192) -> str:
    if value is None and default is not None:
        value = default
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ConfigurationError(f"{name} must be a nonempty string (at most {limit} characters)")
    return value.strip()


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ConfigurationError(f"{name} must be a number between {low:g} and {high:g}")
    return float(value)


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigurationError(f"{name} must be an integer between {low} and {high}")
    return value


def _section(settings: dict, name: str, providers: tuple[str, ...]) -> dict:
    raw = settings.get(name)
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{name} must be a provider configuration object")
    cfg = dict(raw)
    if cfg.get("provider") not in providers:
        raise ConfigurationError(f"{name}.provider must be one of: {', '.join(providers)}")
    cfg["model"] = _text(cfg.get("model"), f"{name}.model", limit=2048)
    cfg["timeout"] = _number(cfg.get("timeout", 60), f"{name}.timeout", 1, 300)
    if cfg["provider"] == "openai":
        url = _text(cfg.get("base_url"), f"{name}.base_url", limit=2048)
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise ConfigurationError(f"{name}.base_url is invalid") from exc
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConfigurationError(f"{name}.base_url must be an HTTP(S) API root without credentials, query, or fragment")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1") and not cfg.get("allow_insecure_http", False):
            raise ConfigurationError(f"{name}: remote HTTP requires allow_insecure_http=true; prefer HTTPS")
        if "allow_insecure_http" in cfg and not isinstance(cfg["allow_insecure_http"], bool):
            raise ConfigurationError(f"{name}.allow_insecure_http must be boolean")
        cfg["base_url"] = url.rstrip("/")
        if cfg.get("key_env") is not None:
            cfg["key_env"] = _text(cfg["key_env"], f"{name}.key_env", limit=256)
            if not cfg["key_env"].isidentifier() or not cfg["key_env"].isascii():
                raise ConfigurationError(f"{name}.key_env must name an environment variable")
    return cfg


class Pipeline:
    """Bounded speech-to-text → chat → text-to-speech conversation pipeline.

    Calls on one instance are serialized. Only turns whose synthesis succeeds
    enter the context. validate() does not contact a server or download a model.
    """

    def __init__(self, settings: dict):
        if not isinstance(settings, dict):
            raise ConfigurationError("settings must be an object")
        self.stt = _section(settings, "stt", ("faster-whisper", "openai"))
        self.llm = _section(settings, "llm", ("openai",))
        self.tts = _section(settings, "tts", ("piper", "openai"))
        self.system_prompt = _text(settings.get("system_prompt"), "system_prompt", default="You are a helpful phone assistant. Give concise, natural spoken answers.")
        self.greeting_text = _text(settings.get("greeting"), "greeting", default="Hello. How can I help you?")
        self.turn_limit = _integer(settings.get("conversation_turns", 8), "conversation_turns", 0, 100)
        self.context_chars = _integer(settings.get("context_char_limit", 24000), "context_char_limit", 1024, 200000)
        self.text_limit = _integer(settings.get("max_text_chars", 4000), "max_text_chars", 64, 16000)
        self.input_seconds = _number(settings.get("max_input_seconds", 120), "max_input_seconds", 1, 600)
        self.output_seconds = _number(settings.get("max_output_seconds", 60), "max_output_seconds", 1, 300)
        self.max_tokens = _integer(self.llm.get("max_tokens", 256), "llm.max_tokens", 1, 4096)
        self.temperature = _number(self.llm.get("temperature", 0.7), "llm.temperature", 0, 2)
        if len(self.greeting_text) > self.text_limit:
            raise ConfigurationError("greeting exceeds max_text_chars")
        if self.stt["provider"] == "faster-whisper":
            self.stt["device"] = _text(self.stt.get("device"), "stt.device", default="cpu", limit=128)
            self.stt["compute_type"] = _text(self.stt.get("compute_type"), "stt.compute_type", default="int8", limit=128)
            self.stt["beam_size"] = _integer(self.stt.get("beam_size", 1), "stt.beam_size", 1, 10)
            if not isinstance(self.stt.get("allow_download", False), bool):
                raise ConfigurationError("stt.allow_download must be boolean")
        if self.stt.get("language") is not None:
            self.stt["language"] = _text(self.stt["language"], "stt.language", limit=64)
        if self.tts["provider"] == "piper":
            self.tts["executable"] = _text(self.tts.get("executable"), "tts.executable", default="piper", limit=2048)
            if self.tts.get("config") is not None:
                self.tts["config"] = _text(self.tts["config"], "tts.config", limit=2048)
        else:
            self.tts["voice"] = _text(self.tts.get("voice"), "tts.voice", default="alloy", limit=256)
            # OpenAI's response_format=pcm is raw 24 kHz PCM16. Compatible
            # providers using another rate must explicitly declare it here.
            self.tts["sample_rate"] = _integer(self.tts.get("sample_rate", 24000), "tts.sample_rate", 8000, 192000)
        if self.tts.get("sample_rate") is not None:
            self.tts["sample_rate"] = _integer(self.tts["sample_rate"], "tts.sample_rate", 8000, 192000)
        self._history: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._whisper = None
        self._piper_rate: int | None = None
        self._validated = False
        self._opener = urllib.request.build_opener(_NoRedirect())

    @staticmethod
    def _key(cfg: dict) -> str | None:
        name = cfg.get("key_env")
        if name is None:
            return None
        key = os.environ.get(name, "").strip()
        if not key or "\r" in key or "\n" in key:
            raise ConfigurationError(f"Required credential environment variable {name} is missing or invalid")
        return key

    def validate(self) -> None:
        """Check fields, credentials, executables and files without network calls.

        Local model weights are initialized lazily. Cached model availability,
        server connectivity and model compatibility are established on first use.
        """
        for cfg in (self.stt, self.llm, self.tts):
            if cfg["provider"] == "openai":
                self._key(cfg)
        if self.stt["provider"] == "faster-whisper":
            if importlib.util.find_spec("faster_whisper") is None:
                raise ConfigurationError("Local STT requires the optional dependency: pip install faster-whisper")
            model = self.stt["model"]
            if (os.path.isabs(model) or model.startswith(".")) and not Path(model).is_dir():
                raise ConfigurationError("stt.model must be an existing faster-whisper model directory")
        if self.tts["provider"] == "piper":
            if not shutil.which(self.tts["executable"]):
                raise ConfigurationError("Piper executable was not found; install piper-tts or set tts.executable")
            if not Path(self.tts["model"]).is_file():
                raise ConfigurationError("tts.model must be an existing Piper ONNX model file")
            config = Path(self.tts.get("config", self.tts["model"] + ".json"))
            if not config.is_file():
                raise ConfigurationError("Piper model config is missing; supply tts.config or the adjacent .onnx.json file")
            try:
                if config.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("oversized config")
                data = json.loads(config.read_text(encoding="utf-8"))
                rate = _integer(data["audio"]["sample_rate"], "Piper config audio.sample_rate", 8000, 192000)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ConfigurationError("Piper config must contain a valid audio.sample_rate") from exc
            if self.tts.get("sample_rate", rate) != rate:
                raise ConfigurationError("tts.sample_rate disagrees with the Piper model config")
            self._piper_rate = rate
        self._validated = True

    async def reply(self, pcm: bytes) -> bytes:
        """Transcribe one utterance, answer, and return PCM16/16 kHz audio.

        Cancelling the awaiting coroutine does not kill an already-running local
        model/thread; HTTP and Piper have their configured operation timeouts.
        """
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
            raise ValueError("pcm must be nonempty, even-length little-endian PCM16 bytes")
        if len(pcm) > int(self.input_seconds * 32000):
            raise ValueError("Caller audio exceeds max_input_seconds")
        return await asyncio.to_thread(self._reply, pcm)

    async def greeting(self) -> bytes:
        """Synthesize the configured greeting without changing conversation history."""
        return await asyncio.to_thread(self._greeting)

    def _greeting(self) -> bytes:
        with self._lock:
            if not self._validated:
                self.validate()
            return self._synthesize(self.greeting_text)

    def _reply(self, pcm: bytes) -> bytes:
        with self._lock:
            if not self._validated:
                self.validate()
            transcript = self._transcribe(pcm).strip()
            if not transcript:
                return b""  # Silence is not a conversation turn.
            if len(transcript) > self.text_limit:
                raise PipelineError("Transcription exceeds max_text_chars")
            messages = [{"role": "system", "content": self.system_prompt}]
            for user, assistant in self._history:
                messages.extend(({"role": "user", "content": user}, {"role": "assistant", "content": assistant}))
            messages.append({"role": "user", "content": transcript})
            data = self._json_request(self.llm, "/chat/completions", {
                "model": self.llm["model"], "messages": messages,
                "max_tokens": self.max_tokens, "temperature": self.temperature, "stream": False,
            })
            try:
                answer = data["choices"][0]["message"]["content"]
                if not isinstance(answer, str) or not answer.strip():
                    raise ValueError("empty answer")
                answer = answer.strip()
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise PipelineError("Chat provider returned no usable message content") from exc
            if len(answer) > self.text_limit:
                raise PipelineError("Chat response exceeds max_text_chars; reduce llm.max_tokens")
            result = self._synthesize(answer)
            self._history.append((transcript, answer))
            while self._history and (len(self._history) > self.turn_limit or sum(len(u) + len(a) for u, a in self._history) > self.context_chars):
                self._history.pop(0)
            return result

    def _request(self, cfg: dict, path: str, body: bytes, content_type: str, limit: int) -> bytes:
        headers = {"Content-Type": content_type, "Accept": "application/json" if path != "/audio/speech" else "application/octet-stream"}
        key = self._key(cfg)
        if key:
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(cfg["base_url"] + path, data=body, headers=headers, method="POST")
        try:
            deadline = time.monotonic() + cfg["timeout"]
            with self._opener.open(request, timeout=cfg["timeout"]) as response:
                chunks, remaining = [], limit + 1
                while remaining:
                    if time.monotonic() >= deadline:
                        raise PipelineError(f"Provider {path} exceeded its operation timeout")
                    # read1 returns after one buffered/socket read, so a peer
                    # trickling data cannot reset the operation deadline forever.
                    chunk = response.read1(min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
        except urllib.error.HTTPError as exc:
            # Never include response bodies, request URLs, or credentials in errors.
            raise PipelineError(f"Provider {path} returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PipelineError(f"Provider {path} could not be reached or timed out") from None
        if len(raw) > limit:
            raise PipelineError(f"Provider {path} response exceeds the configured size limit")
        return raw

    def _json_request(self, cfg: dict, path: str, body: dict) -> dict:
        raw = self._request(cfg, path, json.dumps(body).encode("utf-8"), "application/json", 1024 * 1024)
        return self._decode_json(raw, path)

    @staticmethod
    def _decode_json(raw: bytes, path: str) -> dict:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return data
        except (ValueError, UnicodeError) as exc:
            raise PipelineError(f"Provider {path} returned invalid JSON") from None

    def _transcribe(self, pcm: bytes) -> str:
        if self.stt["provider"] == "faster-whisper":
            try:
                if self._whisper is None:
                    from faster_whisper import WhisperModel
                    self._whisper = WhisperModel(self.stt["model"], device=self.stt["device"], compute_type=self.stt["compute_type"], local_files_only=not self.stt.get("allow_download", False))
                # faster-whisper accepts a file-like WAV without a NumPy dependency
                # in this module. Its bundled decoder handles resampling internally.
                segments, _ = self._whisper.transcribe(io.BytesIO(self._wav(pcm)), language=self.stt.get("language"), beam_size=self.stt["beam_size"], vad_filter=True)
                chunks, length = [], 0
                for segment in segments:
                    length += len(segment.text)
                    if length > self.text_limit:
                        raise PipelineError("Transcription exceeds max_text_chars")
                    chunks.append(segment.text)
                return " ".join(chunks)
            except PipelineError:
                raise
            except Exception:
                raise PipelineError("faster-whisper failed; check model cache/path, device and compute_type (set allow_download=true to fetch uncached weights)") from None
        boundary = "phone-agent-" + uuid.uuid4().hex
        fields = [("model", self.stt["model"])]
        if self.stt.get("language"):
            fields.append(("language", self.stt["language"]))
        parts = []
        for name, value in fields:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8"))
        parts.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="utterance.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode(), self._wav(pcm), f'\r\n--{boundary}--\r\n'.encode()))
        raw = self._request(self.stt, "/audio/transcriptions", b"".join(parts), "multipart/form-data; boundary=" + boundary, 1024 * 1024)
        data = self._decode_json(raw, "/audio/transcriptions")
        if not isinstance(data.get("text"), str):
            raise PipelineError("STT provider returned no text field")
        return data["text"]

    @staticmethod
    def _wav(pcm: bytes) -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm)
        return output.getvalue()

    def _synthesize(self, text: str) -> bytes:
        if self.tts["provider"] == "piper":
            rate = self._piper_rate
            assert rate is not None
            command = [self.tts["executable"], "--model", self.tts["model"], "--config", self.tts.get("config", self.tts["model"] + ".json"), "--output-raw"]
            limit = int(self.output_seconds * rate * 2)
            # File-backed stdout avoids unbounded capture in memory. Piper runs
            # without a shell, and neither its stderr nor input text is logged.
            with tempfile.TemporaryFile() as output:
                try:
                    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL, shell=False)
                    try:
                        process.communicate((text + "\n").encode("utf-8"), timeout=self.tts["timeout"])
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                        raise PipelineError("Piper synthesis timed out") from None
                    if process.returncode:
                        raise PipelineError(f"Piper synthesis failed (exit {process.returncode}); check model and CLI compatibility")
                    if output.tell() > limit:
                        raise PipelineError("Piper audio exceeds max_output_seconds")
                    output.seek(0)
                    pcm = output.read(limit + 1)
                except OSError:
                    raise PipelineError("Piper could not be started; check executable and model permissions") from None
        else:
            rate = self.tts["sample_rate"]
            pcm = self._request(self.tts, "/audio/speech", json.dumps({"model": self.tts["model"], "voice": self.tts["voice"], "input": text, "response_format": "pcm"}).encode("utf-8"), "application/json", int(self.output_seconds * rate * 2))
        if not pcm or len(pcm) % 2:
            raise PipelineError("TTS provider returned empty or malformed PCM16 audio")
        if pcm[:4] in (b"RIFF", b"OggS", b"fLaC") or pcm[:3] == b"ID3":
            raise PipelineError("TTS provider returned an audio container instead of raw PCM16")
        return self._resample(pcm, rate)

    @staticmethod
    def _resample(pcm: bytes, rate: int) -> bytes:
        if rate == 16000:
            return pcm
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        count = len(samples) * 16000 // rate
        if count == 0:
            raise PipelineError("TTS audio is too short to resample")
        output = array("h")
        # Dependency-free linear interpolation. Install/use an external provider
        # with native 16 kHz output when higher-quality resampling is required.
        for index in range(count):
            position = index * rate / 16000
            left = int(position)
            right = min(left + 1, len(samples) - 1)
            fraction = position - left
            output.append(round(samples[left] * (1 - fraction) + samples[right] * fraction))
        if sys.byteorder != "little":
            output.byteswap()
        return output.tobytes()
