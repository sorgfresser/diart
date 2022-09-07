import asyncio
import base64
from pathlib import Path
from queue import SimpleQueue
from typing import Text, Optional, AnyStr

import numpy as np
import sounddevice as sd
import websockets
from einops import rearrange
from rx.subject import Subject
from torchaudio.io import StreamReader

from .audio import FilePath, AudioLoader


class AudioSource:
    """Represents a source of audio that can start streaming via the `stream` property.

    Parameters
    ----------
    uri: Text
        Unique identifier of the audio source.
    sample_rate: int
        Sample rate of the audio source.
    """
    def __init__(self, uri: Text, sample_rate: int):
        self.uri = uri
        self.sample_rate = sample_rate
        self.stream = Subject()

    @property
    def duration(self) -> Optional[float]:
        """The duration of the stream if known. Defaults to None (unknown duration)"""
        return None

    def read(self):
        """Start reading the source and yielding samples through the stream"""
        raise NotImplementedError


class FileAudioSource(AudioSource):
    """Represents an audio source tied to a file.

    Parameters
    ----------
    file: FilePath
        Path to the file to stream.
    sample_rate: int
        Sample rate of the chunks emitted.
    """
    def __init__(self, file: FilePath, sample_rate: int):
        super().__init__(Path(file).stem, sample_rate)
        self.loader = AudioLoader(self.sample_rate, mono=True)
        self._duration = self.loader.get_duration(file)
        self.file = file
        self.resolution = 1 / self.sample_rate
        self.block_size = 1024

    @property
    def duration(self) -> Optional[float]:
        # The duration of a file is known
        return self._duration

    def read(self):
        """Send each chunk of samples through the stream"""
        waveform = self.loader.load(self.file)
        _, num_samples = waveform.shape
        chunks = rearrange(
            waveform.unfold(1, self.block_size, self.block_size),
            "channel chunk sample -> chunk channel sample",
        ).numpy()

        # Add padded last chunk
        if num_samples % self.block_size != 0:
            last_chunk = waveform[:, chunks.shape[0] * self.block_size:].unsqueeze(0).numpy()
            diff_samples = self.block_size - last_chunk.shape[-1]
            last_chunk = np.concatenate([last_chunk, np.zeros((1, 1, diff_samples))], axis=-1)
            chunks = np.vstack([chunks, last_chunk])

        for i, waveform in enumerate(chunks):
            try:
                self.stream.on_next(waveform)
            except Exception as e:
                self.stream.on_error(e)
        self.stream.on_completed()


class MicrophoneAudioSource(AudioSource):
    """Represents an audio source tied to the default microphone available"""

    def __init__(self, sample_rate: int):
        super().__init__("live_recording", sample_rate)
        self.block_size = 1024
        self.mic_stream = sd.InputStream(
            channels=1,
            samplerate=sample_rate,
            latency=0,
            blocksize=self.block_size,
            callback=self._read_callback
        )
        self.queue = SimpleQueue()

    def _read_callback(self, samples, *args):
        self.queue.put_nowait(samples[:, [0]].T)

    def read(self):
        self.mic_stream.start()
        while self.mic_stream:
            try:
                self.stream.on_next(self.queue.get())
            except Exception as e:
                self.stream.on_error(e)
                break
        self.stream.on_completed()


class WebSocketAudioSource(AudioSource):
    """Represents a source of audio coming from the network using the WebSocket protocol.

    Parameters
    ----------
    sample_rate: int
        Sample rate of the chunks emitted.
    host: Text | None
        The host to run the websocket server. Defaults to ``None`` (all interfaces).
    port: int
        The port to run the websocket server. Defaults to 7007.
    """
    def __init__(self, sample_rate: int, host: Optional[Text] = None, port: int = 7007):
        name = host if host is not None and host else "localhost"
        uri = f"{name}:{port}"
        # FIXME sample_rate is not being used, this can be confusing and lead to incompatibilities.
        #  I would prefer the client to send a JSON with data and sample rate, then resample if needed
        super().__init__(uri, sample_rate)
        self.host = host
        self.port = port
        self.websocket = None

    async def _ws_handler(self, websocket):
        self.websocket = websocket
        try:
            async for message in websocket:
                # Decode chunk encoded in base64
                byte_samples = base64.decodebytes(message.encode("utf-8"))
                # Recover array from bytes
                samples = np.frombuffer(byte_samples, dtype=np.float32)
                # Reshape and send through
                self.stream.on_next(samples.reshape(1, -1))
            self.stream.on_completed()
        except websockets.ConnectionClosedError as e:
            self.stream.on_error(e)

    async def _async_read(self):
        async with websockets.serve(self._ws_handler, self.host, self.port):
            await asyncio.Future()

    async def _async_send(self, message: AnyStr):
        await self.websocket.send(message)

    def read(self):
        """Starts running the websocket server and listening for audio chunks"""
        asyncio.run(self._async_read())

    def send(self, message: AnyStr):
        """Send a message through the current websocket.

        Parameters
        ----------
        message: AnyStr
            Bytes or string to send.
        """
        # A running loop must exist in order to send back a message
        ws_closed = "Websocket isn't open, try calling `read()` first"
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError(ws_closed)

        if not loop.is_running():
            raise RuntimeError(ws_closed)

        # TODO support broadcasting to many clients
        # Schedule a coroutine to send back the message
        if message:
            asyncio.run_coroutine_threadsafe(self._async_send(message), loop=loop)


class TorchStreamAudioSource(AudioSource):
    def __init__(
        self,
        uri: Text,
        sample_rate: int,
        streamer: StreamReader,
        stream_index: Optional[int] = None,
    ):
        super().__init__(uri, sample_rate)
        self._streamer = streamer
        self._streamer.add_basic_audio_stream(
            frames_per_chunk=1024,
            stream_index=stream_index,
            format="fltp",
            sample_rate=self.sample_rate,
        )

    def read(self):
        for item in self._streamer.stream():
            try:
                # shape (samples, channels) to (1, samples)
                chunk = np.mean(item[0].numpy(), axis=1, keepdims=True).T
                self.stream.on_next(chunk)
            except Exception as e:
                self.stream.on_error(e)
                break
        self.stream.on_completed()
