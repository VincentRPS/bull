"""
MIT License

Copyright (c) 2023 VincentRPS

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


import asyncio
import logging
import time
import zlib
from platform import system
from random import random
from typing import Any, cast

import aiohttp
import msgspec

from bls_utils.dispatch import Dispatch
from bls_utils.exceptions import ShardError

_log = logging.getLogger(__name__)


class Inflation:
    def __init__(self) -> None:
        self.inflator = zlib.decompressobj()

    def reset(self) -> None:
        self.inflator = zlib.decompressobj()


class RateLimiter:
    def __init__(self, concurrency: int, per: float | int) -> None:
        self.concurrency: int = concurrency
        self.per: float | int = per

        self.current: int = self.concurrency
        self._reserved: list[asyncio.Future[None]] = []
        self.loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        self.pending_reset: bool = False

    async def __aenter__(self) -> 'RateLimiter':
        while self.current == 0:
            future = self.loop.create_future()
            self._reserved.append(future)
            await future

        self.current -= 1

        if not self.pending_reset:
            self.pending_reset = True
            self.loop.call_later(self.per, self.reset)

        return self

    async def __aexit__(self, *_: Any) -> None:
        ...

    def reset(self) -> None:
        current_time = time.time()
        self.reset_at = current_time + self.per
        self.current = self.concurrency

        for _ in range(self.concurrency):
            try:
                self._reserved.pop().set_result(None)
            except IndexError:
                break

        if len(self._reserved):
            self.pending_reset = True
            self.loop.call_later(self.per, self.reset)
        else:
            self.pending_reset = False


class Shard:
    """Low-level implementation of a single Discord shard."""

    ZLIB_SUFFIX = b'\x00\x00\xff\xff'
    FMT_URL = '{base}/?v={version}&encoding=json&compress=zlib-stream'
    RESUMABLE: list[int] = [
        4000,
        4001,
        4002,
        4003,
        4005,
        4007,
        4008,
        4009,
    ]

    def __init__(
        self,
        *,
        token: str,
        session: aiohttp.ClientSession,
        intents: int,
        dispatcher: Dispatch,
        large_threshold: int = 250,
        library: str = 'clyde',
        proxy_url: str | None = None,
        proxy_auth: aiohttp.BasicAuth | None = None,
        shard_id: int = 0,
        shard_count: int = 0,
        base_url: str = 'wss://gateway.discord.gg',
        version: int = 10
    ) -> None:

        self.url = self.FMT_URL.format(base=base_url, version=version)
        self.__token = token
        self.id = shard_id
        self.count = shard_count
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reconnect_base: str | None = None
        self.version = version
        self._session = session
        self.inf = Inflation()
        self._sequence: int | None = None
        self.__proxy_url = proxy_url
        self.__proxy_auth = proxy_auth
        self._rate_limiter = RateLimiter(110, 60)
        self.library = library
        self._large_threshold = large_threshold
        self._intents = intents
        self.session_id: str | None = None
        self.__dispatcher = dispatcher


    async def connect(self, reconnect: bool = False) -> None:
        if self._ws:
            raise ShardError('WebSocket already exists')

        self._financed_hello: asyncio.Future[None] = asyncio.Future()
        self.inf.reset()

        if self._reconnect_base and reconnect:
            url = self.FMT_URL.format(base=self._reconnect_base, version=self.version)
        else:
            url  = self.url

        try:
            self._ws = await self._session.ws_connect(
                url,
                proxy=self.__proxy_url,
                proxy_auth=self.__proxy_auth
            )
        except(aiohttp.ClientConnectionError, aiohttp.ClientConnectorError):
            # TODO: replace with an exponential backoff.
            await asyncio.sleep(10)
            await self.connect(reconnect=reconnect)
            return

        self.__receive_task = asyncio.create_task(self.__receive())

        await self._financed_hello

        if reconnect:
            await self.resume()
        else:
            await self.identify()


    async def __receive(self) -> None:
        if self._ws is None:
            raise ShardError('WebSocket must exist to receive from it')

        async for message in self._ws:
            if message.type == aiohttp.WSMsgType.CLOSED:
                break
            elif message.type == aiohttp.WSMsgType.BINARY:
                # invalid message
                if len(message.data) < 4 or message.data[-4:] != self.ZLIB_SUFFIX:
                    continue

                try:
                    text_content = self.inf.inflator.decompress(message.data) \
                        .decode('utf-8')
                except zlib.error:
                    continue

                data: dict[str, Any] = msgspec.json.decode(
                    text_content,
                    type=dict[str, Any]
                )

                self._sequence = data.get('s')

                op: int = data['op']
                d: dict[str, Any] | int | None = data['d']
                t: str | None = data.get('t')

                match op:
                    case 0:
                        d = cast(dict[str, Any], d)
                        t = cast(str, t)
                        if t == 'READY':
                            self.session_id = d['session_id']
                            self._reconnect_base = d['resume_gateway_url']
                        asyncio.create_task(self.__dispatcher.call(t, d))
                    case 1:
                        await self._ws.send_bytes(
                            msgspec.json.encode(
                                {
                                    'op': 1,
                                    'd': self._sequence
                                }
                            )
                        )
                    case 7:
                        await self._ws.close(code=1002)
                        await self.connect(reconnect=True)
                        return
                    case 9:
                        await self._ws.close()
                        await self.connect()
                        return
                    case 10:
                        d = cast(dict[str, Any], d)

                        self._heartbeat_interval: int = d['heartbeat_interval'] / 1000

                        self.__hb_task = asyncio.create_task(
                            self.__heartbeat_handler(
                                jitter=True
                            )
                        )
                        self._financed_hello.set_result(None)
                    case 11:
                        if not self.__hb_received.done():
                            self.__hb_received.set_result(None)

                            self.__hb_task = asyncio.create_task(
                                self.__heartbeat_handler()
                            )

        await self.handle_close(self._ws.close_code)

    async def close(self) -> None:
        """Close this Shard's websocket."""
        if self._ws:
            await self._ws.close()


    async def send(self, data: dict[str, Any]) -> None:
        if self._ws is None:
            raise ShardError('WebSocket must exist to send to it')

        async with self._rate_limiter:
            await self._ws.send_bytes(msgspec.json.encode(data))


    async def identify(self) -> None:
        await self.send({
            'op': 2,
            'd': {
                'token': self.__token,
                'properties': {
                    'os': system(),
                    'browser': self.library,
                    'device': self.library
                },
                'compress': True,
                'large_threshold': int(self._large_threshold),
                'intents': int(self._intents),
                'shard': [self.id, self.count]
            }
        })


    async def resume(self) -> None:
        await self.send({
            'op': 6,
            'd': {
                'token': self.__token,
                'session_id': self.session_id,
                'seq': self._sequence
            }
        })


    async def __heartbeat_handler(self, jitter: bool = False) -> None:
        if self._ws is None:
            return

        if jitter:
            await asyncio.sleep(self._heartbeat_interval * random()) # noqa: S311
        else:
            await asyncio.sleep(self._heartbeat_interval)

        self.__hb_received: asyncio.Future[None] = asyncio.Future()

        try:
            await self._ws.send_bytes(
                msgspec.json.encode(
                    {
                        'op': 1,
                        'd': self._sequence
                    }
                )
            )
        except ConnectionResetError:
            self.__receive_task.cancel()
            if not self._ws.closed:
                await self._ws.close(code=1008)
            await self.connect(reconnect=bool(self._reconnect_base))
            return

        try:
            await asyncio.wait_for(self.__hb_received, 5)
        except asyncio.TimeoutError:
            self.__receive_task.cancel()

            if not self._ws.closed:
                await self._ws.close(code=1008)

            await self.connect(reconnect=bool(self._reconnect_base))


    async def handle_close(self, code: int | None = None) -> None:
        if self.__hb_task and not self.__hb_task.done():
            self.__hb_task.cancel()

        if code is None: # noqa: SIM114
            await self.connect(reconnect=True)
        elif code is self.RESUMABLE: # type: ignore[comparison-overlap]
            await self.connect(reconnect=True)
        elif code > 4000 or code == 4000:
            await self.connect()
        elif code in (4004, 4011, 4014):
            raise ShardError(f'Code: {code} received. Cannot recover from this code.')
        else:
            # connection may have been killed
            await self.connect(reconnect=True)

