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
import zlib
from platform import system
from random import random
from typing import Any, cast

import aiohttp
import msgspec

from bls_utils.dispatch import Dispatch
from bls_utils.exceptions import ShardError

from .shard_rate_limiter import RateLimiter

_log = logging.getLogger(__name__)


class Inflation:
    def __init__(self) -> None:
        self.inflator = zlib.decompressobj()

    def reset(self) -> None:
        self.inflator = zlib.decompressobj()


class Shard:
    """
    Low-level implementation of a single Discord shard.

    Parameters
    ----------
    token: :class:`str`
        The bot token for this shard.
    session: :class:`aiohttp.ClientSession`
        The ClientSession to use for websocket connections.
    intents: :class:`int`
        Discord privileged intents value.
    dispatcher: :class:`bls_utils.Dispatch`
        Global dispatcher this shard should use.
    large_threshold: :class:`int`
        The Guild large threshold. Defaults to 250.
    library: :class:`str`
        The library to mark the Shard from. Defaults to `clyde`.
    proxy_url: :class:`str`
        The proxy to use for connections. Defaults to None.
    proxy_auth: :class:`aiohttp.BasicAuth`
        The authentication to use for proxies. Defaults to None.
    shard_id: :class:`int`
        The id of this shard. Defaults to 0.
    shard_count: :class:`int`
        Amount of other shards running. Defaults to 0.
    base_url: :class:`str`
        The Gateway URL to use for this shard. Defaults to `wss://gateway.discord.gg`.
    version: :class:`int`
        The Discord API version to use. Defaults to 10.
    """

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
        """
        Create a new connection, or reconnect, to the Discord Gateway.

        Parameters
        ----------
        reconnect: :class:`bool`
            Whether this should reconnect or connect. Defaults to False.
        """
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
        """
        Send a message to this Shard's websocket.

        Parameters
        ----------
        data: :class:`dict`[:class:`str`, :class:`Any`]
            The data to send to Discord.
        """
        if self._ws is None:
            raise ShardError('WebSocket must exist to send to it')

        async with self._rate_limiter:
            await self._ws.send_bytes(msgspec.json.encode(data))


    async def identify(self) -> None:
        """Identify to the Discord Gateway."""
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
        """Resume a previously closed connection."""
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
        """
        Handle a close code sent by Discord, or rather an error.

        Parameters
        ----------
        code: :class:`int` | None
            The code to handle. Defaults to None.
        """
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

