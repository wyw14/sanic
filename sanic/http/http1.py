from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from sanic.request import Request
    from sanic.response import BaseHTTPResponse

from asyncio import CancelledError, sleep
from time import perf_counter

from sanic.compat import Header
from sanic.exceptions import (
    BadRequest,
    ExpectationFailed,
    PayloadTooLarge,
    RequestCancelled,
    ServerError,
    ServiceUnavailable,
)
from sanic.headers import format_http1_response
from sanic.helpers import has_message_body
from sanic.http.constants import Stage
from sanic.http.stream import Stream
from sanic.log import access_logger, error_logger, logger
from sanic.touchup import TouchUpMeta


HTTP_CONTINUE = b"HTTP/1.1 100 Continue\r\n\r\n"


class Http(Stream, metaclass=TouchUpMeta):
    """ "Internal helper for managing the HTTP/1.1 request/response cycle.

    Raises:
        BadRequest: If the request body is malformed.
        Exception: If the request is malformed.
        ExpectationFailed: If the request is malformed.
        PayloadTooLarge: If the request body exceeds the size limit.
        RuntimeError: If the response status is invalid.
        ServerError: If the handler does not produce a response.
        ServerError: If the response is bigger than the content-length.
    """

    HEADER_CEILING = 16_384
    HEADER_MAX_SIZE = 0
    # Maximum length of a single chunk-size line (chunk extensions included)
    CHUNK_SIZE_LINE_MAX = 64
    # Framing and routing fields that RFC 9112 forbids in a trailer-part
    TRAILER_FORBIDDEN_FIELDS = frozenset(
        {
            "content-encoding",
            "content-length",
            "content-range",
            "content-type",
            "host",
            "trailer",
            "transfer-encoding",
        }
    )
    __touchup__ = (
        "http1_request_header",
        "http1_response_header",
        "read",
    )
    __slots__ = [
        "_send",
        "_receive_more",
        "dispatch",
        "recv_buffer",
        "protocol",
        "expecting_continue",
        "stage",
        "keep_alive",
        "head_only",
        "request",
        "exception",
        "url",
        "request_body",
        "request_bytes",
        "request_bytes_left",
        "response",
        "response_func",
        "response_size",
        "response_bytes_left",
        "upgrade_websocket",
        "perft0",
    ]

    def __init__(self, protocol):
        self._send = protocol.send
        self._receive_more = protocol.receive_more
        self.recv_buffer = protocol.recv_buffer
        self.protocol = protocol
        self.keep_alive = True
        self.stage: Stage = Stage.IDLE
        self.dispatch = self.protocol.app.dispatch

    def init_for_request(self):
        """Init/reset all per-request variables."""
        self.exception = None
        self.expecting_continue: bool = False
        self.head_only = None
        self.request_body = None
        self.request_bytes = None
        self.request_bytes_left = None
        self.request_max_size = self.protocol.request_max_size
        self.request: Request = None
        self.response: BaseHTTPResponse = None
        self.upgrade_websocket = False
        self.url = None
        self.perft0 = None

    def __bool__(self):
        """Test if request handling is in progress"""
        return self.stage in (Stage.HANDLER, Stage.RESPONSE)

    async def http1(self):
        """HTTP 1.1 connection handler"""
        # Handle requests while the connection stays reusable
        while self.keep_alive and self.stage is Stage.IDLE:
            self.init_for_request()
            # Wait for incoming bytes (in IDLE stage)
            if not self.recv_buffer:
                await self._receive_more()
                if not self.recv_buffer and self.protocol._eof:
                    # The client closed a reusable connection without
                    # sending another request. This is a normal end to the
                    # connection, not a malformed request: exit silently.
                    self.keep_alive = False
                    return
            self.stage = Stage.REQUEST
            try:
                # Receive and handle a request
                self.response_func = self.http1_response_header

                await self.http1_request_header()

                self.stage = Stage.HANDLER
                self.perft0 = perf_counter()
                self.request.conn_info = self.protocol.conn_info
                await self.protocol.request_handler(self.request)

                # Handler finished, response should've been sent
                if self.stage is Stage.HANDLER and not self.upgrade_websocket:
                    raise ServerError("Handler produced no response")

                if self.stage is Stage.RESPONSE:
                    await self.response.send(end_stream=True)
            except CancelledError as exc:
                # Write an appropriate response before exiting
                if not self.protocol.transport:
                    logger.info(
                        f"Request: {self.request.method} {self.request.url} "
                        "stopped. Transport is closed."
                    )
                    return
                e = (
                    RequestCancelled()
                    if self.protocol.conn_info.lost
                    else (self.exception or exc)
                )
                self.exception = None
                self.keep_alive = False
                await self.error_response(e)
            except Exception as e:
                # Write an error response
                await self.error_response(e)

            # Try to consume any remaining request body
            if self.request_body:
                if self.response and 200 <= self.response.status < 300:
                    error_logger.error(f"{self.request} body not consumed.")
                # Limit the size because the handler may have set it infinite
                self.request_max_size = min(
                    self.request_max_size, self.protocol.request_max_size
                )
                try:
                    async for _ in self:
                        pass
                except (BadRequest, PayloadTooLarge):
                    # The remaining body was malformed (e.g. a chunked
                    # trailer, which must never be reparsed as a smuggled
                    # request) or too large. Drop the connection. This little
                    # delay allows clients to push a small request into
                    # network buffers before we close the socket, so that
                    # they are then able to read the response.
                    await sleep(0.001)
                    self.keep_alive = False

            # Clean up to free memory and for the next request
            if self.request:
                self.request.stream = None
                if self.response:
                    self.response.stream = None

    async def http1_request_header(self):  # no cov
        """Receive and parse request header into self.request."""
        # Receive until full header is in buffer
        buf = self.recv_buffer
        pos = 0

        while True:
            pos = buf.find(b"\r\n\r\n", pos)
            if pos != -1:
                break

            pos = max(0, len(buf) - 3)
            if pos >= self.HEADER_MAX_SIZE:
                break

            await self._receive_more()
            if self.protocol._eof and buf.find(b"\r\n\r\n") == -1:
                raise BadRequest("Bad Request")

        if pos >= self.HEADER_MAX_SIZE:
            raise PayloadTooLarge("Request header exceeds the size limit")

        # Parse header content
        try:
            head = buf[:pos]
            raw_headers = head.decode(errors="surrogateescape")
            reqline, *split_headers = raw_headers.split("\r\n")
            method, self.url, protocol = reqline.split(" ")

            await self.dispatch(
                "http.lifecycle.read_head",
                inline=True,
                context={"head": bytes(head)},
            )

            if protocol == "HTTP/1.1":
                self.keep_alive = True
            elif protocol == "HTTP/1.0":
                self.keep_alive = False
            else:
                raise Exception  # Raise a Bad Request on try-except

            self.head_only = method.upper() == "HEAD"
            request_body = False
            headers = []

            for name, value in (h.split(":", 1) for h in split_headers):
                name, value = h = name.lower(), value.lstrip()

                if name in ("content-length", "transfer-encoding"):
                    if request_body:
                        raise ValueError(
                            "Duplicate Content-Length or Transfer-Encoding"
                        )
                    request_body = True
                elif name == "connection":
                    self.keep_alive = value.lower() == "keep-alive"

                headers.append(h)
        except Exception:
            raise BadRequest("Bad Request")

        if not self.protocol.app.config.KEEP_ALIVE:
            self.keep_alive = False

        headers_instance = Header(headers)
        self.upgrade_websocket = (
            headers_instance.getone("upgrade", "").lower() == "websocket"
        )

        try:
            url_bytes = self.url.encode("ASCII")
        except UnicodeEncodeError:
            raise BadRequest("URL may only contain US-ASCII characters.")

        # Prepare a Request object
        request = self.protocol.request_class(
            url_bytes=url_bytes,
            headers=headers_instance,
            head=bytes(head),
            version=protocol[5:],
            method=method,
            transport=self.protocol.transport,
            app=self.protocol.app,
        )
        self.protocol.request_class._current.set(request)
        await self.dispatch(
            "http.lifecycle.request",
            inline=True,
            context={"request": request},
        )

        # Prepare for request body
        self.request_bytes_left = self.request_bytes = 0
        if request_body:
            headers = request.headers
            expect = headers.getone("expect", None)

            if expect is not None:
                if expect.lower() == "100-continue":
                    self.expecting_continue = True
                else:
                    raise ExpectationFailed(f"Unknown Expect: {expect}")

            if headers.getone("transfer-encoding", None) == "chunked":
                self.request_body = "chunked"
                pos -= 2  # One CRLF stays in buffer
            else:
                self.request_body = True
                try:
                    self.request_bytes_left = self.request_bytes = (
                        self._safe_int(headers["content-length"])
                    )
                except Exception:
                    raise BadRequest("Bad content-length")

        # Remove header and its trailing CRLF
        del buf[: pos + 4]
        self.request, request.stream = request, self
        self.protocol.state["requests_count"] += 1

    async def http1_response_header(
        self, data: bytes, end_stream: bool
    ) -> None:  # no cov
        """Format response header and send it."""
        res = self.response

        # Compatibility with simple response body
        if not data and getattr(res, "body", None):
            data, end_stream = res.body, True  # type: ignore

        size = len(data)
        headers = res.headers
        status = res.status
        self.response_size = size

        if not isinstance(status, int) or status < 200:
            raise RuntimeError(f"Invalid response status {status!r}")

        if not has_message_body(status):
            # Header-only response status
            self.response_func = None
            if (
                data
                or not end_stream
                or "content-length" in headers
                or "transfer-encoding" in headers
            ):
                data, size, end_stream = b"", 0, True
                headers.pop("content-length", None)
                headers.pop("transfer-encoding", None)
                logger.warning(
                    f"Message body set in response on {self.request.path}. "
                    f"A {status} response may only have headers, no body."
                )
        elif self.head_only and "content-length" in headers:
            self.response_func = None
        elif end_stream:
            # Non-streaming response (all in one block)
            headers["content-length"] = size
            self.response_func = None
        elif "content-length" in headers:
            # Streaming response with size known in advance
            self.response_bytes_left = int(headers["content-length"]) - size
            self.response_func = self.http1_response_normal
        else:
            # Length not known, use chunked encoding
            headers["transfer-encoding"] = "chunked"
            data = b"%x\r\n%b\r\n" % (size, data) if size else b""
            self.response_func = self.http1_response_chunked

        if self.head_only:
            # Head request: don't send body
            data = b""
            self.response_func = self.head_response_ignored

        headers["connection"] = "keep-alive" if self.keep_alive else "close"

        # This header may be removed or modified by the AltSvcCheck Touchup
        # service. At server start, we either remove this header from ever
        # being assigned, or we change the value as required.
        headers["alt-svc"] = ""

        ret = format_http1_response(status, res.processed_headers)
        if data:
            ret += data

        # Send a 100-continue if expected and not Expectation Failed
        if self.expecting_continue:
            self.expecting_continue = False
            if status != 417:
                ret = HTTP_CONTINUE + ret

        # Send response
        if self.protocol.access_log:
            self.log_response()

        await self._send(ret)
        self.stage = Stage.IDLE if end_stream else Stage.RESPONSE

    def head_response_ignored(self, data: bytes, end_stream: bool) -> None:
        """HEAD response: body data silently ignored."""
        if end_stream:
            self.response_func = None
            self.stage = Stage.IDLE

    async def http1_response_chunked(
        self, data: bytes, end_stream: bool
    ) -> None:
        """Format a part of response body in chunked encoding."""
        # Chunked encoding
        size = len(data)
        if end_stream:
            await self._send(
                b"%x\r\n%b\r\n0\r\n\r\n" % (size, data)
                if size
                else b"0\r\n\r\n"
            )
            self.response_func = None
            self.stage = Stage.IDLE
        elif size:
            await self._send(b"%x\r\n%b\r\n" % (size, data))

    async def http1_response_normal(
        self, data: bytes, end_stream: bool
    ) -> None:
        """Format / keep track of non-chunked response."""
        bytes_left = self.response_bytes_left - len(data)
        if bytes_left <= 0:
            if bytes_left < 0:
                raise ServerError("Response was bigger than content-length")

            await self._send(data)
            self.response_func = None
            self.stage = Stage.IDLE
        else:
            if end_stream:
                raise ServerError("Response was smaller than content-length")

            await self._send(data)
        self.response_bytes_left = bytes_left

    async def error_response(self, exception: Exception) -> None:
        """Handle response when exception encountered"""
        # Disconnect after an error if in any other state than handler
        if self.stage is not Stage.HANDLER:
            self.keep_alive = False

        # Request failure? Respond but then disconnect
        if self.stage is Stage.REQUEST:
            self.stage = Stage.HANDLER

        # From request and handler states we can respond, otherwise be silent
        if self.stage is Stage.HANDLER:
            app = self.protocol.app

            if self.request is None:
                self.create_empty_request()

            request_middleware = not isinstance(
                exception, (ServiceUnavailable, RequestCancelled)
            )
            try:
                await app.handle_exception(
                    self.request, exception, request_middleware
                )
            except Exception as e:
                await app.handle_exception(self.request, e, False)

    def create_empty_request(self) -> None:
        """Create an empty request object for error handling use.

        Current error handling code needs a request object that won't exist
        if an error occurred during before a request was received. Create a
        bogus response for error handling use.
        """

        # Reformat any URL already received with \xHH escapes for better logs
        url_bytes = (
            self.url.encode(errors="surrogateescape")
            .decode("ASCII", errors="backslashreplace")
            .encode("ASCII")
            if self.url
            else b"*"
        )

        # FIXME: Avoid this by refactoring error handling and response code
        self.request = self.protocol.request_class(
            url_bytes=url_bytes,
            headers=Header({}),
            version="1.1",
            method="NONE",
            transport=self.protocol.transport,
            app=self.protocol.app,
        )
        self.request.stream = self

    def log_response(self) -> None:
        """Helper method provided to enable the logging of responses in case if the `HttpProtocol.access_log` is enabled."""  # noqa: E501
        req, res = self.request, self.response
        extra = {
            "status": getattr(res, "status", 0),
            "byte": res.headers.get("content-length", 0)
            if res.headers.get("transfer-encoding") != "chunked"
            else "chunked",
            "host": f"{id(self.protocol.transport):X}"[-5:-1] + "unx",
            "request": "nil",
            "duration": (
                f" {1000 * (perf_counter() - self.perft0):.1f}ms"
                if self.perft0 is not None
                else ""
            ),
        }
        if ip := req.client_ip:
            extra["host"] = f"{ip}:{req.port}"
        extra["request"] = f"{req.method} {req.url}"
        access_logger.info("", extra=extra)

    # Request methods

    async def __aiter__(self):
        """Async iterate over request body."""
        while self.request_body:
            data = await self.read()

            if data:
                yield data

    async def read(self) -> bytes | None:  # no cov
        """Read some bytes of request body."""

        # Send a 100-continue if needed
        if self.expecting_continue:
            self.expecting_continue = False
            await self._send(HTTP_CONTINUE)

        # Receive request body chunk
        buf = self.recv_buffer

        if self.request_bytes_left == 0 and self.request_body == "chunked":
            size = await self._read_chunk_size(buf)

            if size == 0:
                # Consume and process the trailer-part so that the
                # request is only complete when it is properly ended.
                await self._read_chunked_trailer(buf)
                self.request_body = None
            else:
                self.request_bytes_left = size
                self.request_bytes += size

        # Request size limit
        if self.request_bytes > self.request_max_size:
            self.keep_alive = False
            raise PayloadTooLarge("Request body exceeds the size limit")

        # End of request body?
        if not self.request_bytes_left:
            self.request_body = None
            return None

        # At this point we are good to read/return up to request_bytes_left
        if not buf:
            await self._receive_more()
            if not buf and self.protocol._eof:
                # The client went away before the declared body (or
                # chunk) was fully transferred. That is a client error,
                # distinct from closing an idle connection: answer with
                # a clear 400 and drop the connection.
                self.keep_alive = False
                raise BadRequest("Incomplete request body")

        data = bytes(buf[: self.request_bytes_left])
        size = len(data)

        del buf[:size]

        self.request_bytes_left -= size

        await self.dispatch(
            "http.lifecycle.read_body",
            inline=True,
            context={"body": data},
        )

        return data

    async def _read_chunk_size(self, buf: bytearray) -> int:
        """Parse one chunk header: ``CRLF <size>[;<chunk-ext>] CRLF``.

        The leading CRLF terminates the previous chunk's data (or is the
        CRLF left over from the header section for the first chunk).
        Returns the declared chunk size; ``0`` marks the final chunk.
        """
        # Wait for the CRLF that begins the chunk-size line.
        while len(buf) < 2:
            if self.protocol._eof:
                self.keep_alive = False
                raise BadRequest("Bad chunked encoding")
            await self._receive_more()

        if buf[:2] != b"\r\n":
            self.keep_alive = False
            raise BadRequest("Bad chunked encoding")

        # Wait for the CRLF ending the chunk-size line.
        while True:
            pos = buf.find(b"\r\n", 2)
            if pos != -1:
                break
            if len(buf) > self.CHUNK_SIZE_LINE_MAX:
                self.keep_alive = False
                raise BadRequest("Bad chunked encoding")
            if self.protocol._eof:
                self.keep_alive = False
                raise BadRequest("Bad chunked encoding")
            await self._receive_more()

        try:
            raw = bytes(buf[2:pos]).split(b";", 1)[0].strip()
            size = self._safe_int(raw.decode("ascii"), 16)
        except Exception:
            self.keep_alive = False
            raise BadRequest("Bad chunked encoding")

        # Consume the leading CRLF, the chunk-size line and its CRLF.
        del buf[: pos + 2]

        return size

    async def _read_chunked_trailer(self, buf: bytearray) -> None:
        """Consume the trailer-part following the zero-size chunk.

        ``trailer-part = *( field-line CRLF ) CRLF``

        Every trailer field is parsed as a header field and, when
        allowed, added to the request headers. The whole section is
        always consumed up to and including the terminating empty line,
        so no trailer byte can ever be reparsed as a pipelined request
        on this keep-alive connection.
        """
        trailer_bytes = 0

        while True:
            while len(buf) < 2:
                if self.protocol._eof:
                    self.keep_alive = False
                    raise BadRequest("Bad chunked encoding")
                await self._receive_more()

            # The empty line ending the (possibly empty) trailer section.
            if buf[:2] == b"\r\n":
                del buf[:2]
                return

            # Wait for the CRLF ending this trailer field line.
            pos = buf.find(b"\r\n")
            while pos == -1:
                if trailer_bytes + len(buf) > self.HEADER_CEILING:
                    self.keep_alive = False
                    raise BadRequest("Bad chunked encoding")
                if self.protocol._eof:
                    self.keep_alive = False
                    raise BadRequest("Bad chunked encoding")
                await self._receive_more()
                pos = buf.find(b"\r\n")

            line = bytes(buf[:pos])
            del buf[: pos + 2]
            trailer_bytes += pos + 2
            if trailer_bytes > self.HEADER_CEILING:
                self.keep_alive = False
                raise BadRequest("Bad chunked encoding")

            self._consume_trailer_field(line)

    def _consume_trailer_field(self, line: bytes) -> None:
        """Validate one trailer field line and attach it to the request."""
        try:
            text = line.decode("ascii")
        except UnicodeDecodeError:
            self.keep_alive = False
            raise BadRequest("Bad chunked encoding")

        if ":" not in text:
            self.keep_alive = False
            raise BadRequest("Bad chunked encoding")

        name, value = text.split(":", 1)
        # A field name consists of token characters only: no whitespace.
        if not name or any(ch.isspace() for ch in name):
            self.keep_alive = False
            raise BadRequest("Bad chunked encoding")
        name = name.lower()

        # RFC 9112: framing and routing fields MUST NOT be carried in
        # trailers. Accepting them could desync security decisions or
        # duplicate fields such as Host or Content-Length.
        if name in self.TRAILER_FORBIDDEN_FIELDS:
            self.keep_alive = False
            raise BadRequest("Bad chunked encoding")

        self.request.headers.add(name, value.lstrip())

    # Response methods

    def respond(self, response: BaseHTTPResponse) -> BaseHTTPResponse:
        """Initiate new streaming response.

        Nothing is sent until the first send() call on the returned object, and
        calling this function multiple times will just alter the response to be
        given.
        """
        if self.stage is not Stage.HANDLER:
            self.stage = Stage.FAILED
            raise RuntimeError("Response already started")

        # Disconnect any earlier but unused response object
        if self.response is not None:
            self.response.stream = None

        # Connect and return the response
        self.response, response.stream = response, self
        return response

    @property
    def send(self):
        return self.response_func

    @classmethod
    def set_header_max_size(cls, *sizes: int):
        cls.HEADER_MAX_SIZE = min(
            *sizes,
            cls.HEADER_CEILING,
        )

    @staticmethod
    def _safe_int(value: str, base: int = 10) -> int:
        if "-" in value or "+" in value or "_" in value:
            raise ValueError
        return int(value, base)
