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
from collections.abc import Sequence
from typing import Any

import frozendict

from .coro_func import CoroFunc

__all__: Sequence[str] = ('Dispatch')


class Dispatch:
    """
    Utility for dispatching raw event data.

    .. note::
        Events dispatched are dispatched as frozen dictionaries.
        This means that they are immutable and cannot be globally mutated.
        To make them mutable, you should convert it back to a normalized dictionary.
    """

    def __init__(self) -> None:
        self._events: dict[str, list[CoroFunc]] = {}


    def add_call(self, invoker: str, caller: CoroFunc) -> None:
        """
        Add a function to be called when an event is dispatched.

        Parameters
        ----------
        invoker: :class:`str`
            The event `caller` is invoked for.
        caller: :class:`.coro_func.CoroFunc`
            The asynchronous function to be called by `invoker`.
        """
        try:
            self._events[invoker].append(caller)
        except KeyError:
            self._events[invoker] = [caller]


    def remove_call(self, invoker: str, caller: CoroFunc) -> None:
        """
        Remove a caller from this dispatcher.

        Parameters
        ----------
        invoker: :class:`str`
            The event `caller` is invoked for.
        caller: :class:`.coro_func.CoroFunc`
            The asynchronous function to be called by `invoker`.
        """
        self._events[invoker].remove(caller)


    async def call(self, type: str, data: dict[str, Any]) -> None:
        """
        Call functions from in this dispatcher.

        Parameters
        ----------
        type: :class:`str`
            The event being dispatched.
        data: :class:`dict`[:class:`str`, Any]
            The data to be dispatched.
        """
        callers = self._events.get(type)

        if callers is None:
            return

        # mypy thinks the frozendict class is a module for some reason,
        frozen = frozendict.frozendict(data) # type: ignore[operator]

        # gather tasks them run them concurrently
        tasks = []

        for caller in callers:
            tasks.append(caller(frozen))

        await asyncio.gather(*tasks)
