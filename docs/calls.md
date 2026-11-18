# Call provider configuration

Use `language` to pick the speech language. Pick `engine: elevenlabs` to
connect directly to ElevenAgents or `engine: pipeline` to configure STT,
chat, and TTS separately.

## ElevenAgents

Create an authenticated agent in ElevenLabs and configure its language,
voice, and prompt there. Save its ID in `elevenlabs.agent_id`. The key
referenced by `key_env` needs access to get a signed conversation URL.

Use `asr: scribe` to send phone audio to standalone Scribe with a 0.3-second
silence threshold. Committed text is passed into the voice session. This
uses the Speech-to-Text key permission as well. Use `asr: agents` to send
PCM directly to the agent's speech recognizer. Enable client events
`conversation_initiation_metadata`, `audio`, `ping`, and `interruption`
in the agent's configuration. Set its input and output audio formats to
`pcm_16000`.

## Composable pipeline

| Stage and provider | Required settings | Optional settings |
| --- | --- | --- |
| STT `faster-whisper` | `model` (local directory or cached model name) | `language`, `device`, `compute_type`, `beam_size`, `allow_download` |
| STT `openai` | `base_url`, `model` | `language`, `key_env`, `timeout` |
| LLM `openai` | `base_url`, `model` | `key_env`, `max_tokens`, `temperature`, `timeout` |
| TTS `piper` | `model` (ONNX file with adjacent `.onnx.json`) | `config`, `executable`, `timeout` |
| TTS `openai` | `base_url`, `model` | `voice`, `key_env`, `sample_rate`, `timeout` |

OpenAI-compatible roots generally end in `/v1`. The client calls
`audio/transcriptions`, `chat/completions`, and `audio/speech` on the
configured providers. For example, Ollama chat can use
`http://127.0.0.1:11434/v1` with a model you have loaded. HTTP TTS requests raw
little-endian PCM and defaults to 24 kHz. Configure `sample_rate` if your
endpoint returns another rate. Output is converted to 16 kHz.

Pipeline settings include `greeting`, `system_prompt`, `conversation_turns`,
`max_input_seconds`, and `max_output_seconds`. The call adapter accepts up
to eight seconds of queued synthesized output, so keep replies short.
The pipeline submits a turn after a short quiet interval. Its latency and
turn boundaries depend on the selected providers and the caller's audio.

Device and provider checks report setup readiness. Test a call on your
firmware to verify the live capture and telephony playback routes. The local
speech and the online provider interfaces can be used independently of
ElevenAgents.
