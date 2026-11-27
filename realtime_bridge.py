import base64
import json
import threading
import time
from typing import Callable, Optional

import websocket  # websocket-client

# OpenAI Realtime over Server WebSocket bridge.
# - Ingests Twilio Media Streams (u-law 8k) -> converts to PCM16 24k -> sends to OpenAI
# - Receives PCM16 24k audio deltas -> converts to u-law 8k -> emits to Twilio via provided callback

class RealtimeBridge:
    def __init__(self, api_key: str, model: str = 'gpt-4o-realtime-preview', voice: str = 'alloy', on_output_ulaw: Optional[Callable[[bytes], None]] = None):
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.on_output_ulaw = on_output_ulaw
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        self._awaiting_response = False

        self._connect()

    def _connect(self):
        url = f"wss://api.openai.com/v1/realtime?model={self.model}"
        headers = [
            f"Authorization: Bearer {self.api_key}",
            "OpenAI-Beta: realtime=v1",
        ]

        def on_open(ws):
            # Configure session: instructions, voice, turn detection, audio formats
            ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "instructions": (
                        "You are ZenVoice, a warm, calm, empathetic phone assistant fluent in both Hindi and English. "
                        "Keep replies brief (1-2 sentences). Use natural turn-taking and avoid long monologues. "
                        "IMPORTANT: Always respond in the SAME language as the user speaks. "
                        "If user speaks Hindi or Hinglish, respond in Hindi. If they speak English, respond in English. "
                        "Be emotionally intelligent and empathetic."
                    ),
                    "voice": self.voice,
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "turn_detection": {"type": "server_vad", "silence_duration_ms": 400},
                }
            }))

        def on_message(ws, message):
            try:
                data = json.loads(message)
            except Exception:
                return
            t = data.get("type")
            if t in ("response.output_audio.delta", "response.audio.delta"):
                # bytes are base64 PCM16 24k
                b64 = data.get("delta") or data.get("audio") or data.get("bytes")
                if not b64:
                    return
                try:
                    ulaw = base64.b64decode(b64)
                    if self.on_output_ulaw:
                        self.on_output_ulaw(ulaw)
                except Exception:
                    pass
            elif t in ("response.completed", "response.done", "response.refusal.delta"):
                self._awaiting_response = False

        def on_error(ws, err):
            # Swallow errors but mark closed on fatal
            pass

        def on_close(ws, code, msg):
            self._closed = True

        self._ws = websocket.WebSocketApp(
            url,
            header=headers,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        self._thread = threading.Thread(target=self._ws.run_forever, kwargs={"ping_interval": 20, "ping_timeout": 10}, daemon=True)
        self._thread.start()
        # Give it a moment to open
        time.sleep(0.2)

    # Public API
    def send_ulaw_frame_b64(self, b64_payload: str):
        ulaw = base64.b64decode(b64_payload)
        self.send_ulaw_frame(ulaw)

    def send_ulaw_frame(self, ulaw_bytes: bytes):
        if self._closed or not self._ws:
            return
        # Pass-through as G.711 μ-law audio
        b64 = base64.b64encode(ulaw_bytes).decode('ascii')
        try:
            self._ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": b64
            }))
        except Exception:
            pass

    def update_session(self, instructions: str):
        if self._closed or not self._ws:
            return
        try:
             self._ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "instructions": instructions
                }
            }))
        except Exception:
            pass

    def commit_and_request(self):
        if self._closed or not self._ws:
            return
        try:
            self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            if not self._awaiting_response:
                self._ws.send(json.dumps({"type": "response.create"}))
                self._awaiting_response = True
        except Exception:
            pass

    def close(self):
        self._closed = True
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

