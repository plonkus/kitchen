"""A fake Plow chat API — the ticket, messages and socket endpoints the
plow-chat server uses, served over real HTTP and WebSocket by uvicorn."""
import asyncio
import itertools
import socket
from collections import defaultdict

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

TOKEN = "agt_fake-token-xyz"
SIGNATURE = "signed-write-capability"
ACCEPTED_TYPES = {"image/png", "application/pdf"}  # the types the tests send
NAMES = {"+15550000001": "Sam", "+15550000002": "Patrick"}  # as Plow names members it knows


class FakePlow:
    def __init__(self):
        self.history = defaultdict(list)  # chat -> messages, oldest first
        self.sent = []  # (chat, body) the agent posted
        self.attachments = {}  # uid -> declared metadata, plus upload once the PUT lands
        self.upload_status = 200  # what the provider's upload URL answers
        self.refused_upload = None  # a filename the provider's upload URL answers 403
        self.sent_uids = []  # attachment_uids of each send
        self.hold = False  # answer sends as held, never delivered
        self.sockets = []
        self.flaky = set()  # chats whose next read answers 503
        self.inbound_files = {}  # attachment uid -> bytes a member sent; absent answers 404
        self.files_host = None  # serve attachment URLs from this host, as absolute URLs
        self.files_auth = {}  # attachment uid -> Authorization header its download carried
        self._uids = itertools.count(1)
        self.app = Starlette(routes=[
            Route("/v1/ws/ticket", self._ticket, methods=["POST"]),
            Route("/v1/chats/{chat}/messages", self._messages, methods=["GET", "POST"]),
            Route("/v1/chats/{chat}/attachments", self._attachments, methods=["POST"]),
            Route("/provider/{uid}", self._upload, methods=["PUT"]),
            Route("/v1/chats/{chat}/attachments/{uid}/content", self._content),
            WebSocketRoute("/v1/ws", self._ws),
        ])

    def _message(self, chat, body, handle=None, late=False, files=()):
        sender = {"type": "member", "display_name": NAMES.get(handle, handle), "provider_key": handle} if handle else {"type": "agent"}
        attachments = []
        for name, data in files:  # data None: Plow lists it but can't serve it
            uid = f"att_{next(self._uids)}"
            if data is not None:
                self.inbound_files[uid] = data
            path = f"/v1/chats/{chat}/attachments/{uid}/content?sig={SIGNATURE}"
            host = f"http://{self.files_host}:{self.port}" if self.files_host else ""
            attachments.append({"uid": uid, "filename": name, "status": "received", "url": host + path})
        message = {"uid": f"msg_{next(self._uids)}", "chat_uid": chat, "body": body, "sender": sender,
                   "attachments": attachments}
        # A late text was sent before the newest one, so Plow sorts it below it.
        self.history[chat].insert(len(self.history[chat]) - late, message)
        return message

    def add(self, chat, handle, body, late=False, files=()):
        """A member's text, with any (name, bytes) files, lands while no socket hears it."""
        return self._message(chat, body, handle, late, files)

    async def inbound(self, chat, handle, body, late=False, files=()):
        """A member texts the line, and every open socket hears it."""
        message = self.add(chat, handle, body, late, files)
        await self._broadcast(message)
        return message

    async def _broadcast(self, message):
        for ws in list(self.sockets):
            await ws.send_json({"event_type": "message_received", "data": {"message": message}})

    async def drop_sockets(self):
        for ws in list(self.sockets):
            await ws.close()
        self.sockets.clear()

    def _authorized(self, request):
        return request.headers.get("authorization") == f"Bearer {TOKEN}"

    async def _ticket(self, request):
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"ticket": f"wst_{next(self._uids)}"}, status_code=201)

    async def _messages(self, request):
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        chat = request.path_params["chat"]
        if chat in self.flaky and request.method == "GET":
            self.flaky.discard(chat)
            return JSONResponse({"error": "unavailable"}, status_code=503)
        if request.method == "POST":
            sent = await request.json()
            if self.hold:
                return JSONResponse({"uid": "msg_earlier", "held": True, "reason": "repeated_agent_message"})
            body = sent.get("body", "")
            self.sent.append((chat, body))
            self.sent_uids.append(sent.get("attachment_uids", []))
            for uid in sent.get("attachment_uids", []):
                self.attachments[uid]["sent_with"] = body
            message = self._message(chat, body)
            await self._broadcast(message)  # Plow echoes the agent's own sends
            return JSONResponse(message, status_code=201)
        limit = int(request.query_params.get("limit", 20))
        return JSONResponse({"data": self.history[chat][::-1][:limit]})  # newest first, as Plow orders

    async def _attachments(self, request):
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        declared = await request.json()
        if declared["content_type"] not in ACCEPTED_TYPES:
            return JSONResponse({"error": {"code": "unsupported_content_type"}}, status_code=415)
        uid = f"att_{next(self._uids)}"
        self.attachments[uid] = declared
        return JSONResponse({
            "uid": uid,
            "upload_url": f"{request.base_url}provider/{uid}?sig={SIGNATURE}",
            "upload_headers": {
                "Content-Type": declared["content_type"],
                "Content-Length": str(declared["size_bytes"]),
                "x-provider-grant": uid,
            },
        }, status_code=201)

    async def _content(self, request):
        self.files_auth[request.path_params["uid"]] = request.headers.get("authorization")
        data = self.inbound_files.get(request.path_params["uid"])
        if data is None or request.query_params.get("sig") != SIGNATURE:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return Response(data, media_type="image/jpeg")

    async def _upload(self, request):
        """The provider's signed URL: not Plow, so it must never see Plow's token."""
        attachment = self.attachments[request.path_params["uid"]]
        attachment["upload"] = (await request.body(), request.headers)
        return Response(status_code=403 if attachment["filename"] == self.refused_upload else self.upload_status)

    async def _ws(self, websocket):
        await websocket.accept()
        await websocket.send_json({"type": "connected"})
        self.sockets.append(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            self.sockets = [ws for ws in self.sockets if ws is not websocket]

    async def serve(self):
        """Start on a free localhost port; returns (base_url, uvicorn server)."""
        self.sockets.clear()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = self.port = s.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="warning", ws="websockets-sansio"))
        asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        return f"http://127.0.0.1:{port}", server
