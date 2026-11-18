"""Android bridge transport: PCM16, 16 kHz mono in both directions."""
import asyncio
import contextlib
import struct

from ..device.adb import BRIDGE_JAR

CHUNK = 3200                  # capture: 100 ms
FRAME = 640                   # playback: 20 ms
BYTES_PER_SECOND = 32000
MAX_PLAYBACK = 8 * BYTES_PER_SECOND


class Audio:
    def __init__(self):
        self.process = None
        self.ready = asyncio.Event()
        self.playback = bytearray()
        self.flush = False
        self.rx_bytes = self.tx_bytes = 0
        self.diagnostics_task = None
        self.playback_task = None

    async def open(self, device, duration):
        command = f"CLASSPATH={BRIDGE_JAR} timeout -k 1 {duration + 5} app_process / PhoneAudioBridge"
        self.process = await asyncio.create_subprocess_exec(*device.prefix, "shell", "-T", command,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=8192)
        self.diagnostics_task = asyncio.create_task(self.diagnostics())
        self.playback_task = asyncio.create_task(self.play())
        try:
            await asyncio.wait_for(self.ready.wait(), 4)
        except asyncio.TimeoutError as error:
            raise RuntimeError("Could not open the Flip5 cellular audio route") from error

    async def diagnostics(self):
        while line := await self.process.stderr.readline():
            if line.startswith(b"READY "):
                self.ready.set()

    def queue(self, data):
        if len(data) % 2 or len(self.playback) + len(data) > MAX_PLAYBACK:
            raise RuntimeError("Invalid or excessive PCM playback data")
        self.playback.extend(data)

    def clear(self):
        self.playback.clear()
        self.flush = True

    async def capture(self):
        while True:
            try:
                data = await asyncio.wait_for(self.process.stdout.readexactly(CHUNK), 3)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError) as error:
                raise RuntimeError("Phone audio capture stopped") from error
            self.rx_bytes += len(data)
            yield data

    async def play(self):
        await self.ready.wait()
        clock = asyncio.get_running_loop()
        deadline = clock.time()
        while True:
            if self.flush:
                self.process.stdin.write(struct.pack(">i", -1))
                self.flush = False
            chunk = bytes(self.playback[:FRAME])
            del self.playback[:FRAME]
            self.tx_bytes += len(chunk)
            self.process.stdin.write(struct.pack(">i", FRAME) + chunk.ljust(FRAME, b"\0"))
            await asyncio.wait_for(self.process.stdin.drain(), 1)
            deadline += 0.02
            now = clock.time()
            if now > deadline + 0.5:
                raise RuntimeError("Phone audio playback fell behind")
            await asyncio.sleep(max(0, deadline - now))

    async def close(self):
        tasks = [task for task in (self.diagnostics_task, self.playback_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not self.process:
            return
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, asyncio.TimeoutError):
            self.process.stdin.write(struct.pack(">i", -2))
            await asyncio.wait_for(self.process.stdin.drain(), 0.25)
        self.process.stdin.close()
        try:
            # Drain the capture pipe so asyncio can reap a child whose stream filled.
            await asyncio.wait_for(self.process.communicate(), 0.75)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.process.communicate(), 1)
