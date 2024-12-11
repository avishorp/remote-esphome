from typing import Callable, Coroutine
from random import Random

import pytest
import asyncio

@pytest.mark.asyncio
async def test_get_hello_no_tunnel(
    test_server: int, check_get_hello: Callable[[int], Coroutine[None, None, None]]
):
    await check_get_hello(test_server)


@pytest.mark.asyncio
async def test_get_hello_with_tunnel(
    test_server: int,
    tunnel,
    check_get_hello: Callable[[int], Coroutine[None, None, None]],
):
    async with tunnel(test_server) as tunnel_port:
        await check_get_hello(tunnel_port)


@pytest.mark.asyncio
async def test_get_random_no_tunnel(
    test_server: int, check_get_random: Callable[[int], Coroutine[None, None, None]]
):
    await check_get_random(test_server)


@pytest.mark.asyncio
async def test_get_random_with_tunnel(
    test_server: int,
    tunnel,
    check_get_random: Callable[[int], Coroutine[None, None, None]],
):

    async with tunnel(test_server) as tunnel_port:
        await check_get_random(tunnel_port)


@pytest.mark.asyncio
async def test_post_echo_no_tunnel(
    test_server: int, check_post_echo: Callable[[int], Coroutine[None, None, None]]
):
    await check_post_echo(test_server)


@pytest.mark.asyncio
async def test_post_echo_with_tunnel(
    test_server: int,
    tunnel,
    check_post_echo: Callable[[int], Coroutine[None, None, None]],
):

    async with tunnel(test_server) as tunnel_port:
        await check_post_echo(tunnel_port)


@pytest.mark.asyncio
async def test_ws_echo_no_tunnel(
    test_server: int, check_ws_echo: Callable[[int], Coroutine[None, None, None]]
):
    await check_ws_echo(test_server)

@pytest.mark.asyncio
async def test_ws_echo_with_tunnel(
    test_server: int,
    tunnel,
    check_ws_echo: Callable[[int], Coroutine[None, None, None]],
):

    async with tunnel(test_server) as tunnel_port:
        await check_ws_echo(tunnel_port)


@pytest.mark.asyncio
async def test_multiple_requests(
    test_server: int,
    tunnel,
    check_ws_echo: Callable[[int], Coroutine[None, None, None]],
    check_get_random: Callable[[int], Coroutine[None, None, None]],
    check_post_echo: Callable[[int], Coroutine[None, None, None]]
):
    async with tunnel(test_server) as tunnel_port:
        tasks = []
        tasks.append(asyncio.create_task(check_ws_echo(tunnel_port)))
        tasks.append(asyncio.create_task(check_get_random(tunnel_port)))
        #tasks.append(asyncio.create_task(check_post_echo(tunnel_port)))
        #await asyncio.gather(*tasks)
        await asyncio.sleep(25)
        pass