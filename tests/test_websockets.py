import asyncio
import logging
import re

from asyncio import Event, Queue, TimeoutError
from unittest.mock import Mock, call

import pytest

from websockets.frames import CTRL_OPCODES, DATA_OPCODES, OP_TEXT, Frame
from websockets.server import ServerProtocol

from sanic.exceptions import ServerError, WebsocketClosed
from sanic.server.protocols.websocket_protocol import WebSocketProtocol
from sanic.server.websockets import impl as wsimpl
from sanic.server.websockets.frame import WebsocketFrameAssembler
from sanic.server.websockets.impl import (
    CLOSED,
    CLOSING,
    OPEN,
    WebsocketImplProtocol,
)


try:
    from unittest.mock import AsyncMock
except ImportError:
    from tests.asyncmock import AsyncMock  # type: ignore


@pytest.mark.asyncio
async def test_ws_frame_get_message_incomplete_timeout_0():
    assembler = WebsocketFrameAssembler(Mock())
    assembler.message_complete = AsyncMock(spec=Event)
    assembler.message_complete.is_set = Mock(return_value=False)
    data = await assembler.get(0)

    assert data is None
    assembler.message_complete.is_set.assert_called_once()


@pytest.mark.asyncio
async def test_ws_frame_get_message_in_progress():
    assembler = WebsocketFrameAssembler(Mock())
    assembler.get_in_progress = True

    message = re.escape(
        "Called get() on Websocket frame assembler "
        "while asynchronous get is already in progress."
    )

    with pytest.raises(ServerError, match=message):
        await assembler.get()


@pytest.mark.asyncio
async def test_ws_frame_get_message_incomplete():
    assembler = WebsocketFrameAssembler(Mock())
    assembler.message_complete.wait = AsyncMock(return_value=True)
    assembler.message_complete.is_set = Mock(return_value=False)
    data = await assembler.get()

    assert data is None
    assembler.message_complete.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_ws_frame_get_message():
    assembler = WebsocketFrameAssembler(Mock())
    assembler.message_complete.wait = AsyncMock(return_value=True)
    assembler.message_complete.is_set = Mock(return_value=True)
    data = await assembler.get()

    assert data == b""
    assembler.message_complete.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_ws_frame_get_message_with_timeout():
    assembler = WebsocketFrameAssembler(Mock())
    assembler.message_complete.wait = AsyncMock(return_value=True)
    assembler.message_complete.is_set = Mock(return_value=True)
    data = await assembler.get(0.1)

    assert data == b""
    assembler.message_complete.wait.assert_awaited_once()
    assert assembler.message_complete.is_set.call_count == 2


@pytest.mark.asyncio
async def test_ws_frame_get_message_with_timeouterror():
    assembler = WebsocketFrameAssembler(Mock())
    assembler.message_complete.wait = AsyncMock(return_value=True)
    assembler.message_complete.is_set = Mock(return_value=True)
    assembler.message_complete.wait.side_effect = TimeoutError("...")
    data = await assembler.get(0.1)

    assert data == b""
    assembler.message_complete.wait.assert_awaited_once()
    assert assembler.message_complete.is_set.call_count == 2


@pytest.mark.asyncio
async def test_ws_frame_get_not_completed():
    assembler = WebsocketFrameAssembler(Mock())
    assembler.message_complete = AsyncMock(spec=Event)
    assembler.message_complete.is_set = Mock(return_value=False)
    data = await assembler.get()

    assert data is None


@pytest.mark.asyncio
async def test_ws_frame_get_not_completed_start():
    assembler = WebsocketFrameAssembler(Mock())
    assembler.message_complete = AsyncMock(spec=Event)
    assembler.message_complete.is_set = Mock(side_effect=[False, True])
    data = await assembler.get(0.1)

    assert data is None


@pytest.mark.asyncio
async def test_ws_frame_get_paused():
    assembler = WebsocketFrameAssembler(Mock())
    assembler.message_complete = AsyncMock(spec=Event)
    assembler.message_complete.is_set = Mock(side_effect=[False, True])
    assembler.paused = True
    data = await assembler.get()

    assert data is None
    assembler.protocol.resume_frames.assert_called_once()


@pytest.mark.asyncio
async def test_ws_frame_get_data():
    assembler = WebsocketFrameAssembler(Mock())
    assembler.message_complete = AsyncMock(spec=Event)
    assembler.message_complete.is_set = Mock(return_value=True)
    assembler.chunks = [b"foo", b"bar"]
    data = await assembler.get()

    assert data == b"foobar"


@pytest.mark.asyncio
async def test_ws_frame_get_iter_in_progress():
    assembler = WebsocketFrameAssembler(Mock())
    assembler.get_in_progress = True

    message = re.escape(
        "Called get_iter on Websocket frame assembler "
        "while asynchronous get is already in progress."
    )

    with pytest.raises(ServerError, match=message):
        [x async for x in assembler.get_iter()]


@pytest.mark.asyncio
async def test_ws_frame_get_iter_none_in_queue():
    assembler = WebsocketFrameAssembler(Mock())
    assembler.message_complete.set()
    assembler.chunks = [b"foo", b"bar"]

    chunks = [x async for x in assembler.get_iter()]

    assert chunks == [b"foo", b"bar"]


@pytest.mark.asyncio
async def test_ws_frame_get_iter_paused():
    assembler = WebsocketFrameAssembler(Mock())
    assembler.message_complete.set()
    assembler.paused = True

    [x async for x in assembler.get_iter()]
    assembler.protocol.resume_frames.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("opcode", DATA_OPCODES)
async def test_ws_frame_put_not_fetched(opcode):
    assembler = WebsocketFrameAssembler(Mock())
    assembler.message_fetched.set()

    message = re.escape(
        "Websocket put() got a new message when the previous message was "
        "not yet fetched."
    )
    with pytest.raises(ServerError, match=message):
        await assembler.put(Frame(opcode, b""))


@pytest.mark.asyncio
@pytest.mark.parametrize("opcode", DATA_OPCODES)
async def test_ws_frame_put_fetched(opcode):
    assembler = WebsocketFrameAssembler(Mock())
    assembler.message_fetched = AsyncMock()
    assembler.message_fetched.is_set = Mock(return_value=False)

    await assembler.put(Frame(opcode, b""))
    assembler.message_fetched.wait.assert_awaited_once()
    assembler.message_fetched.clear.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("opcode", DATA_OPCODES)
async def test_ws_frame_put_message_complete(opcode):
    assembler = WebsocketFrameAssembler(Mock())
    assembler.message_complete.set()

    message = re.escape(
        "Websocket put() got a new message when a message was "
        "already in its chamber."
    )
    with pytest.raises(ServerError, match=message):
        await assembler.put(Frame(opcode, b""))


@pytest.mark.asyncio
@pytest.mark.parametrize("opcode", DATA_OPCODES)
async def test_ws_frame_put_message_into_queue(opcode):
    foo = "foo" if (opcode == OP_TEXT) else b"foo"
    assembler = WebsocketFrameAssembler(Mock())
    assembler.chunks_queue = AsyncMock(spec=Queue)
    assembler.message_fetched = AsyncMock()
    assembler.message_fetched.is_set = Mock(return_value=False)
    await assembler.put(Frame(opcode, b"foo"))

    assert assembler.chunks_queue.put.call_args_list == [call(foo), call(None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("opcode", DATA_OPCODES)
async def test_ws_frame_put_not_fin(opcode):
    assembler = WebsocketFrameAssembler(Mock())

    retval = await assembler.put(Frame(opcode, b"foo", fin=False))

    assert retval is None


@pytest.mark.asyncio
@pytest.mark.parametrize("opcode", CTRL_OPCODES)
async def test_ws_frame_put_skip_ctrl(opcode):
    assembler = WebsocketFrameAssembler(Mock())

    retval = await assembler.put(Frame(opcode, b""))

    assert retval is None


# --------------------------------------------------------------------- #
# Keepalive / heartbeat lifecycle
# --------------------------------------------------------------------- #


class FakeTransport:
    def __init__(self):
        self.written: list[bytes] = []
        self.closed = False
        self.aborted = False

    def pause_reading(self):
        pass

    def write(self, data):
        self.written.append(bytes(data))

    def can_write_eof(self):
        return False

    def write_eof(self):
        pass

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True

    def abort(self):
        self.aborted = True
        self.closed = True


class FakeIO:
    def __init__(self, loop, transport):
        self.loop = loop
        self.transport = transport

    async def send(self, data):
        self.transport.write(data)


def _parse_frames(chunks):
    frames = []
    for chunk in chunks:
        i = 0
        while i + 2 <= len(chunk):
            first, second = chunk[i], chunk[i + 1]
            length = second & 0x7F
            i += 2
            if length == 126:
                length = int.from_bytes(chunk[i : i + 2], "big")
                i += 2
            elif length == 127:
                length = int.from_bytes(chunk[i : i + 8], "big")
                i += 8
            payload = chunk[i : i + length]
            i += length
            frames.append((first & 0x0F, payload))
    return frames


def _make_impl(loop, ping_interval=0.1, ping_timeout=0.1):
    ws_proto = ServerProtocol(state=OPEN, max_size=None)
    impl = WebsocketImplProtocol(
        ws_proto,
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
        close_timeout=0.1,
    )
    return ws_proto, impl


async def _shutdown_impl(impl):
    if impl.data_finished_fut and not impl.data_finished_fut.done():
        impl.data_finished_fut.cancel()
    impl.connection_lost(None)
    if impl.auto_closer_task and not impl.auto_closer_task.done():
        await asyncio.wait_for(impl.auto_closer_task, timeout=1)


@pytest.mark.asyncio
async def test_keepalive_timeout_sends_single_close_with_reason(caplog):
    caplog.set_level(logging.DEBUG)
    loop = asyncio.get_running_loop()
    ws_proto, impl = _make_impl(loop, ping_interval=0.1, ping_timeout=0.1)
    transport = FakeTransport()
    await impl.connection_made(FakeIO(loop, transport))
    keepalive = impl.keepalive_ping_task
    auto_closer = impl.auto_closer_task

    deadline = loop.time() + 3
    while ws_proto.close_sent is None and loop.time() < deadline:
        await asyncio.sleep(0.02)

    assert ws_proto.close_sent is not None
    assert ws_proto.close_sent.code == 1011
    assert ws_proto.close_sent.reason == "keepalive ping timeout"
    assert ws_proto.state in (CLOSING, CLOSED)

    frames = _parse_frames(transport.written)
    pings = [f for f in frames if f[0] == 0x9]
    closes = [f for f in frames if f[0] == 0x8]
    assert len(pings) == 1
    assert len(closes) == 1
    assert int.from_bytes(closes[0][1][:2], "big") == 1011
    assert closes[0][1][2:] == b"keepalive ping timeout"

    # Wind the connection down.
    await _shutdown_impl(impl)
    if keepalive and not keepalive.done():
        await asyncio.wait_for(keepalive, timeout=1)
    if auto_closer and not auto_closer.done():
        await asyncio.wait_for(auto_closer, timeout=1)

    # No new heartbeat and no second close frame after the timeout.
    await asyncio.sleep(0.25)
    frames = _parse_frames(transport.written)
    assert sum(1 for f in frames if f[0] == 0x9) == 1
    assert sum(1 for f in frames if f[0] == 0x8) == 1

    messages = [r.getMessage() for r in caplog.records]
    assert any("timed out waiting for pong" in m for m in messages)


@pytest.mark.asyncio
async def test_keepalive_pong_racing_timeout_is_not_overridden(monkeypatch):
    loop = asyncio.get_running_loop()
    ws_proto, impl = _make_impl(loop, ping_interval=0.01, ping_timeout=0.05)
    transport = FakeTransport()
    await impl.connection_made(FakeIO(loop, transport))

    pong_waiter = loop.create_future()
    ping_calls = []
    fail_calls = []

    async def fake_send_ping(data=None):
        ping_calls.append(1)
        if len(ping_calls) == 1:
            return pong_waiter
        # The next heartbeat finds the connection closing.
        ws_proto.state = CLOSED
        raise WebsocketClosed("closed")

    async def racing_wait_for(awaitable, timeout):
        # The deadline expires and the wait is aborted...
        await asyncio.sleep(0)
        # ...while the matching pong is processed in the same turn.
        pong_waiter.set_result(None)
        raise asyncio.TimeoutError()

    impl._send_ping = fake_send_ping
    impl.fail_connection = lambda *a, **k: fail_calls.append((a, k))
    monkeypatch.setattr(wsimpl, "_wait_for", racing_wait_for)

    keepalive = impl.keepalive_ping_task
    await asyncio.wait_for(keepalive, timeout=1)

    # The valid pong wins: connection must not be failed/closed.
    assert fail_calls == []
    assert len(ping_calls) == 2
    frames = _parse_frames(transport.written)
    assert all(op != 0x8 for op, _ in frames)

    await _shutdown_impl(impl)


@pytest.mark.asyncio
async def test_keepalive_task_cancelled_when_connection_lost():
    loop = asyncio.get_running_loop()
    ws_proto, impl = _make_impl(loop, ping_interval=10, ping_timeout=10)
    await impl.connection_made(FakeIO(loop, FakeTransport()))
    keepalive = impl.keepalive_ping_task

    assert keepalive is not None and not keepalive.done()

    impl.connection_lost(ConnectionResetError("boom"))
    assert impl.keepalive_ping_task is None
    await asyncio.sleep(0.05)
    assert keepalive.done()
    assert keepalive.cancelled()

    await _shutdown_impl(impl)


@pytest.mark.asyncio
async def test_new_connection_has_its_own_keepalive_timer():
    loop = asyncio.get_running_loop()
    ws_proto1, impl1 = _make_impl(loop, ping_interval=10, ping_timeout=10)
    await impl1.connection_made(FakeIO(loop, FakeTransport()))
    task1 = impl1.keepalive_ping_task

    # Old connection ends; its timer dies with it.
    impl1.connection_lost(None)
    assert impl1.keepalive_ping_task is None

    # A brand new connection gets a brand new timer.
    ws_proto2, impl2 = _make_impl(loop, ping_interval=10, ping_timeout=10)
    await impl2.connection_made(FakeIO(loop, FakeTransport()))
    try:
        assert impl2.keepalive_ping_task is not task1
        # The same impl cannot be attached to a second connection.
        with pytest.raises(ServerError):
            await impl2.connection_made(FakeIO(loop, FakeTransport()))
    finally:
        await _shutdown_impl(impl1)
        await _shutdown_impl(impl2)


@pytest.mark.asyncio
async def test_server_initiated_close_is_logged_separately(caplog):
    caplog.set_level(logging.DEBUG, logger="sanic.websockets")
    loop = asyncio.get_running_loop()
    ws_proto = ServerProtocol(state=OPEN, max_size=None)
    impl = WebsocketImplProtocol(ws_proto, close_timeout=1)
    impl.loop = loop
    impl.io_proto = FakeIO(loop, FakeTransport())

    impl.end_connection(1001, "going away")

    assert ws_proto.close_sent is not None
    assert ws_proto.close_sent.code == 1001
    messages = [r.getMessage() for r in caplog.records]
    assert any("closed by server" in m for m in messages)
    assert not any("lost unexpectedly" in m for m in messages)
    assert not any("timed out waiting for pong" in m for m in messages)


@pytest.mark.asyncio
async def test_unexpected_network_disconnect_is_logged_separately(caplog):
    caplog.set_level(logging.DEBUG)
    ws_proto = ServerProtocol(state=OPEN, max_size=None)
    impl = WebsocketImplProtocol(ws_proto)

    impl.connection_lost(ConnectionResetError("connection reset by peer"))

    assert ws_proto.state is CLOSED
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "lost unexpectedly" in m and "connection reset by peer" in m
        for m in messages
    )
    assert not any("closed by server" in m for m in messages)
    assert not any("timed out waiting for pong" in m for m in messages)


@pytest.mark.asyncio
async def test_protocol_discards_websocket_after_connection_lost():
    loop = asyncio.get_running_loop()
    proto = WebSocketProtocol(
        loop=loop, app=Mock(), connections=set()
    )
    transport = Mock()
    transport.get_extra_info = Mock(return_value=None)
    proto.connection_made(transport)

    old_websocket = Mock()
    proto.websocket = old_websocket
    proto.connection_lost(None)

    old_websocket.connection_lost.assert_called_once_with(None)
    assert proto.websocket is None
