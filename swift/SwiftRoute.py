#!/usr/bin/env python
"""
@author Jesse Haviland
"""

import platform
import swift as sw
import asyncio
from threading import Thread
import webbrowser as wb
import json
import http.server
import socketserver
from pathlib import Path
import os
from queue import Empty
from http import HTTPStatus
import urllib
from typing import Union

from queue import Queue
from typing_extensions import Literal as L

try:
    import websockets

    WS = True
except ImportError:
    WS = False

# Check for notebook support
try:
    from IPython.display import display
    from IPython.display import IFrame

    NB = True
except ImportError:
    NB = False

# Check for RTC Support
try:
    from aiortc import (
        RTCPeerConnection,
        RTCSessionDescription,
        RTCDataChannel,
    )

    RTC = True
except ImportError:
    RTCPeerConnection = None
    RTC = False

try:
    # Check if we are in Google Colab
    from google.colab.output import eval_js  # type: ignore

    COLAB = True
except ImportError:
    COLAB = False

try:
    import pyjs

    PYJS = True
except ImportError:
    PYJS = False

if platform.system() == "Emscripten":
    THREADS = False
else:
    THREADS = True

def __start_rtc_comms(outq, inq):
    if not RTC:
        print(
            "\rrtc comms not are supported on this platform\n"
        )
        raise

    # Start the RTC server
    socket = Thread(
        target=SwiftRtc,
        args=(
            outq,
            inq,
        ),
        daemon=True,
    )
    socket.start()

    socket_port = 1 if COLAB else 0

    return socket, socket_port

def __start_websocket_comms(outq, inq, stop_servers):
    if not WS:
        print(
            "\nWebsockets comms are not supported on this platform\n"
        )
        raise

    # Start our websocket server with a new port
    socket = Thread(
        target=SwiftSocket,
        args=(
            outq,
            inq,
            stop_servers,
        ),
        daemon=True,
    )

    socket.start()
    socket_port = inq.get()

    return socket, socket_port

def __start_pyjs_comms(outq, inq, run):
    if not PYJS:
        print(
            "\npyjs comms are not supported on this platform\n"
        )
        raise

    running_loop = asyncio.get_running_loop()

    pyjs.js.console.log("Hello from pyjs")
    pyjs.js.console.log(f"Running loop is: {running_loop}")

    return SwiftPyJS(outq, inq, run), None

def start_servers(
    outq: Queue,
    inq: Queue,
    stop_servers,
    open_tab: bool = True,
    browser: Union[str, None] = None,
    comms: L["websocket", "rtc", "pyjs"] = "websocket",
):
    # We are going to attempt to set up an RTC connection
    if comms == "rtc":
        socket, socket_port = __start_rtc_comms(outq, inq)
    elif comms == "pyjs":
        socket, server = __start_pyjs_comms(outq, inq, stop_servers)

        loop = asyncio.get_running_loop()
        serve_task = loop.create_task(socket.serve())

        return serve_task, None
    else:
        socket, socket_port = __start_websocket_comms(outq, inq, stop_servers)

    # Start a http server
    server = Thread(
        target=SwiftServer,
        args=(
            outq,
            inq,
            socket_port,
            stop_servers,
        ),
        daemon=True,
    )

    server.start()
    server_port = inq.get()

    if open_tab:
        if COLAB:
            colab_url = eval_js(f"google.colab.kernel.proxyPort({server_port})")
            url = colab_url + f"?{socket_port}"
        else:
            url = f"http://localhost:{server_port}/?{socket_port}"

        if browser is not None:
            if browser == "notebook":
                if not NB:
                    raise ImportError(
                        "\nCould not open in notebook mode, install ipython with 'pip"
                        " install ipython'\n"
                    )

                display(
                    IFrame(
                        src=url,
                        width="600",
                        height="400",
                    )
                )
            else:
                try:
                    wb.get(browser).open_new_tab(url)
                except wb.Error:
                    print("\nCould not open specified browser, using default instead\n")
                    wb.open_new_tab(url)
        else:
            wb.open_new_tab(url)

    if comms == "rtc":
        if not RTC:
            raise ImportError(
                "\nCould not start RTC server, install aiortc with 'pip install"
                " aiortc'\n"
            )
        # Get the RTC offer from the HTTP Server
        try:
            offer = inq.get(timeout=30)
        except Empty:
            print(
                "\nCould not connect to the Swift simulator, RTC Connection timed"
                " out \n"
            )
            raise

        # Send the offer to the RTC server
        outq.put(offer)

        # Get the answer from the RTC server
        offer_python = inq.get()

        # Send the answer to the HTTP server
        outq.put(offer_python)
    else:
        try:
            inq.get(timeout=10)
        except Empty:
            print("\nCould not connect to the Swift simulator \n")
            raise

    return socket, server


class SwiftRtc:
    def __init__(self, outq, inq):
        self.pcs = set()

        self.outq = outq
        self.inq = inq

        pc = RTCPeerConnection()
        coro = self.run_rtc(pc)

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        self.as_inq = asyncio.Queue()
        self.as_wq = asyncio.Queue()

        self.run = True
        self.connected = False

        try:
            self.loop.run_until_complete(coro)
        except KeyboardInterrupt:
            pass
        finally:
            print("Closing RTC Loop")
            self.loop.close()

    async def run_rtc(self, pc: RTCPeerConnection):
        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            if pc.connectionState == "failed":
                print(f"RTC Connection state is {pc.connectionState}")
                await pc.close()
                self.run = False
                self.connected = False

        @pc.on("datachannel")
        async def on_datachannel(channel: RTCDataChannel):
            self.connected = True

            @channel.on("message")
            async def on_message(message):
                if isinstance(message, str):
                    if not message == "PING":
                        await self.as_inq.put(message)
                else:
                    print("Recieved Unknown RTC Message")
                    print(message)

            while self.connected:
                if channel.bufferedAmount < 10000:
                    try:
                        data = await self.producer(0.001)
                    except Empty:
                        await asyncio.sleep(0.001)
                        continue

                    expected = data[0]
                    msg = data[1]

                    channel.send(json.dumps(msg))

                    if expected:
                        recieved = await self.as_inq.get()
                        self.inq.put(recieved)
                else:
                    await asyncio.sleep(0.001)

        while self.run:
            if self.connected:
                await asyncio.sleep(5)
            else:
                message = await self.producer()

                params = message["offer"]

                offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

                # handle offer
                await pc.setRemoteDescription(offer)

                # send answer
                answer = await pc.createAnswer()

                # This line is taking forever??
                await pc.setLocalDescription(answer)

                self.inq.put(
                    json.dumps(
                        {
                            "sdp": pc.localDescription.sdp,
                            "type": pc.localDescription.type,
                        }
                    )
                )

                while not pc.iceConnectionState == "completed":
                    await asyncio.sleep(0.1)

                self.connected = True

    async def producer(self, timeout: Union[float, None] = None) -> Union[str, dict]:
        """Get a message from the queue."""
        data = self.outq.get(timeout=timeout)
        return data


class SwiftSocket:
    def __init__(self, outq, inq, run):
        self.pcs = set()
        self.run = run
        self.outq = outq
        self.inq = inq
        self.USERS = set()
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        started = False

        port = 53000
        while not started and port < 62000:
            try:
                start_server = websockets.serve(self.serve, "localhost", port)
                self.loop.run_until_complete(start_server)
                started = True
            except OSError:
                port += 1

        self.inq.put(port)
        self.loop.run_forever()

    async def register(self, websocket):
        self.USERS.add(websocket)

    async def serve(self, websocket, path):
        # Initial connection handshake
        await self.register(websocket)
        recieved = await websocket.recv()
        self.inq.put(recieved)

        # Now onto send, recieve cycle
        while self.run():
            message = await self.producer()
            expected = message[0]
            msg = message[1]
            await websocket.send(json.dumps(msg))
            await self.expect_message(websocket, expected)
        return

    async def expect_message(self, websocket, expected):
        if expected:
            recieved = await websocket.recv()
            self.inq.put(recieved)

    async def producer(self):
        data = self.outq.get()
        return data


class SwiftServer:
    def __init__(self, outq, inq, socket_port, run, verbose=False, custom_root=None):
        server_port = 52000
        self.inq = inq
        self.run = run

        root_dir = Path(sw.__file__).parent / "out"

        class MyHttpRequestHandler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super(MyHttpRequestHandler, self).__init__(
                    *args, directory=str(root_dir), **kwargs
                )

            def log_message(self, format, *args):
                if verbose:
                    http.server.SimpleHTTPRequestHandler.log_message(
                        self, format, *args
                    )
                else:
                    pass

            def do_POST(self):
                # Handle RTC Offer
                if self.path == "/offer":
                    # Get the initial offer
                    length = int(self.headers.get("content-length"))  # type: ignore
                    params = json.loads(self.rfile.read(length))

                    inq.put(params)
                    answer = outq.get()

                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(str.encode(answer))

                    return

            def do_GET(self):
                if self.path == "/":
                    self.send_response(301)

                    self.send_header(
                        "Location",
                        "http://localhost:"
                        + str(server_port)
                        + "/?"
                        + str(socket_port),
                    )

                    self.end_headers()
                    return
                elif self.path == "/?" + str(socket_port):
                    self.path = "index.html"
                elif self.path.startswith("/retrieve/"):
                    # print(f"Retrieving file: {self.path[10:]}")
                    self.path = urllib.parse.unquote(self.path[9:])
                    self.send_file_via_real_path()
                    return

                self.path = Path(self.path).as_posix()

                try:
                    http.server.SimpleHTTPRequestHandler.do_GET(self)
                except BrokenPipeError:
                    # After killing this error will pop up but it's of no use
                    # to the user
                    pass

            def send_file_via_real_path(self):
                try:
                    f = open(self.path, "rb")
                except OSError:
                    self.send_error(HTTPStatus.NOT_FOUND, "File not found")
                    return None
                ctype = self.guess_type(self.path)
                try:
                    fs = os.fstat(f.fileno())
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-type", ctype)
                    self.send_header("Content-Length", str(fs[6]))
                    self.send_header(
                        "Last-Modified", self.date_time_string(fs.st_mtime)
                    )
                    self.end_headers()
                    self.copyfile(f, self.wfile)
                finally:
                    f.close()

        Handler = MyHttpRequestHandler

        connected = False

        while not connected and server_port < 62000:
            try:
                with socketserver.TCPServer(("", server_port), Handler) as httpd:
                    self.inq.put(server_port)
                    connected = True

                    httpd.serve_forever()
            except OSError:
                server_port += 1


class SwiftPyJS:
    def __init__(self, outq, inq, run, url = './swift/index.html', target_element_id = 'swift-viewport', entry_point = '_bindSwiftMessageHandlers'):
        self._cb_on_open = None
        self._cb_on_close = None
        self._cb_on_message = None

        self.run  = run
        self.outq = outq
        self.inq  = inq
        self.is_connected = False
        
        self._init_hooks(url, target_element_id, entry_point)

    async def serve(self):
        # Wait for callbacks to be bound
        while not self.is_connected and self.run():
            await asyncio.sleep(0.5)

        if not self.run():
            return

        pyjs.js.console.log('SwiftPyJS comms are running')

        # Invoke on open callback
        if self._cb_on_open is not None:
            self._cb_on_open()

        # Now onto send, recieve cycle
        while self.run():
            message = await self.producer()
            pyjs.js.console.log(f'recieved message: {message}')
            try:
              expected = message[0]
              msg      = message[1]
              if self._cb_on_message is not None:
                self._cb_on_message(json.dumps(msg))
              else:
                pyjs.js.console.log('Discarding message as no "on_message" callback has been bound')
            except Exception as e:
              pyjs.js.console.warn(e)

        pyjs.js.console.log('SwiftPyJS comms are stopping')

        if self._cb_on_close is not None:
            self._cb_on_close()


    async def producer(self):
        data = self.outq.get()
        if asyncio.iscoroutine(data):
          return await data
        else:
          return data

    @staticmethod
    def make_js_func(py_func):
        """
        Wrap a python function in a function instance that is
        callable from JavaScript
        """
        handle = pyjs.JsValue(py_func)
        f = handle.py_call.bind(handle)
        return f, handle

    async def on_recv(self, data):
        recieved = pyjs.to_py(data)
        await self.inq.put(str(recieved))

    def bind_message_handlers(self, on_open, on_close, on_message):
        self._cb_on_open    = on_open
        self._cb_on_close   = on_close
        self._cb_on_message = on_message
        self.is_connected   = True # Start processing messages

        pyjs.js.console.log("Swift message handlers have been bound")

        cb, handle = SwiftPyJS.make_js_func(self.on_recv)

        return cb

    def _init_hooks(self, url, target_element_id, entry_point):
        cb, handle = SwiftPyJS.make_js_func(self.bind_message_handlers)
        if pyjs.js.window is not None:
          pyjs.js.window[entry_point] = cb
          pyjs.js.console.log(f"Entry point bound to window.{entry_point}")
        elif pyjs.js.globalThis is not None:
          pyjs.js.globalThis[entry_point] = cb
          pyjs.js.console.log(f"Entry point bound to globalThis.{entry_point}")
        else:
          pyjs.js.console.warn("Could not bind entry point. 'window' and 'globalThis' were null")

        target = pyjs.js.document.getElementById(target_element_id)

        if target == None:
          pyjs.js.console.warn(f"Could not init comms. Target element 'id={target_element_id}' does not exist")
          return None

        new_element = pyjs.js.document.createElement("iframe")
        new_element.id = target_element_id
        new_element.setAttribute("src", url + f"?pyjs.{entry_point}")
        target.replaceWith(new_element)
