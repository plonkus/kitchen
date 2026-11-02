"""plow-chat channel server — bridges a Plow chat line to the sous.

Holds the Plow chat API socket for the line's agent credential, pushes each
member's text into the sous as a `<channel source="plow-chat">` tag, and gives
the sous `reply` and `send_file` tools. No allowlist: Plow shows the credential only the chats
the line's account owns, so every chat and every member it delivers is served.
"""
import asyncio
import json
import mimetypes
import sys
from dataclasses import dataclass, field
from itertools import takewhile
from pathlib import Path

import httpx
import mcp.types as types
import websockets
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server

from claude_kitchen.channel import CHANNEL_CAPABILITY, _refuse_if_deaf, push
from claude_kitchen.state import atomic_write_json, plow_chat_dir

CREDENTIALS = "plow-credentials"  # written mode 600 by `plow-agents mint LINE --credential-file`
RECONNECT_DELAY = 5
PAGE = 50
# One speaker's back-to-back texts are one tag: iMessage sends a text and the
# link after it as two messages, measured from 0.83 s up to 1.50 s apart. The
# window restarts on each text; another speaker or chat ends it.
BURST_SECONDS = 2.0
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
MAX_ATTACHMENTS = 4  # per message
UPLOAD_TIMEOUT = httpx.Timeout(30, read=120)
# No system files, so a guess doesn't change from one machine to another.
STANDARD_TYPES = mimetypes.MimeTypes(filenames=())
# Plow's spelling of the types the standard library misses or names differently.
CONTENT_TYPES = {
    ".3gp": "video/3gpp",
    ".amr": "audio/amr",
    ".avi": "video/x-msvideo",
    ".caf": "audio/x-caf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".epub": "application/epub+zip",
    ".gz": "application/x-gzip",
    ".ico": "image/x-icon",
    ".ics": "text/calendar",
    ".key": "application/x-iwork-keynote-sffkey",
    ".m4a": "audio/x-m4a",
    ".m4v": "video/x-m4v",
    ".mid": "audio/midi",
    ".midi": "audio/midi",
    ".numbers": "application/x-iwork-numbers-sffnumbers",
    ".pages": "application/x-iwork-pages-sffpages",
    ".pkpass": "application/vnd.apple.pkpass",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".rtf": "text/rtf",
    ".vcf": "text/vcard",
    ".wav": "audio/x-wav",
    ".webp": "image/webp",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def log(msg: str):
    print(f"plow-chat: {msg}", file=sys.stderr, flush=True)


def read_credentials(directory: Path) -> dict:
    """`PLOW_API_BASE` and `PLOW_AGENT_TOKEN`, from the env file `plow-agents mint` writes."""
    lines = (directory / CREDENTIALS).read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


def content_type_of(file: Path) -> str:
    """The type to declare for `file`; Plow decides whether it accepts it."""
    content_type = CONTENT_TYPES.get(file.suffix.lower()) or STANDARD_TYPES.guess_type(file.name)[0]
    if content_type is None:
        raise ValueError(f"can't tell the file type of {file.name} from its extension; not sent")
    return content_type


def attachable(path: str) -> Path:
    """The file at `path`, once it passes every check that needs no Plow."""
    file = Path(path).expanduser()
    if not file.is_file():
        raise ValueError(f"{file} is not a file; not sent")
    size = file.stat().st_size
    if not 0 < size <= MAX_ATTACHMENT_BYTES:
        raise ValueError(f"{file.name} is {size} bytes; Plow takes 1 byte up to 100 MiB; not sent")
    content_type_of(file)
    return file


def sent(r: httpx.Response, what: str) -> str:
    """Report a message send, failing unless Plow will deliver it. A held send
    answers 200 and is never delivered."""
    if r.is_error:
        raise RuntimeError(f"Plow refused the {what} with HTTP {r.status_code}: {r.text}")
    message = r.json()
    if message.get("held"):
        raise RuntimeError(f"Plow held the {what} and will never deliver it: {message['reason']}")
    return f"sent {message['uid']}"


def speaker(message: dict) -> tuple:
    return message["chat_uid"], message["sender"]["type"], message["sender"].get("provider_key")


@dataclass
class Bridge:
    client: httpx.AsyncClient
    deliver: callable  # async (content, meta) -> None
    # Chat -> uids of its latest messages taken, persisted so neither the
    # socket/catch-up overlap nor a restart takes one twice. Uids, not a
    # high-water mark: a text sent earlier can land later and sort below one
    # already taken.
    cursor: Path
    taken: dict = field(init=False)
    burst: list = field(init=False, default_factory=list)  # one speaker's texts not yet handed over
    due: float = 0.0  # loop time the burst closes at

    def __post_init__(self):
        self.taken = json.loads(self.cursor.read_text()) if self.cursor.exists() else {}

    async def take(self, message: dict):
        """Add a message to the burst once, handing the burst over first if
        the message is someone else's or in another chat."""
        uids = self.taken.setdefault(message["chat_uid"], [])
        if message["uid"] in uids or message["uid"] in [m["uid"] for m in self.burst]:
            return
        if self.burst and speaker(self.burst[0]) != speaker(message):
            await self.flush()
        self.burst.append(message)
        self.due = asyncio.get_running_loop().time() + BURST_SECONDS

    async def flush(self):
        """Hand a member's burst to the sous as one text; our own sends,
        Plow's system notes and text-less messages (tapbacks, bare
        attachments) are only recorded."""
        first = self.burst[0]
        body = "\n\n".join(m["body"] for m in self.burst if m["body"].strip())
        if body and first["sender"]["type"] == "member":
            await self.deliver(body, {"from": first["sender"]["display_name"], "chat": first["chat_uid"]})
        uids = self.taken[first["chat_uid"]]
        uids += [m["uid"] for m in self.burst]  # after delivery, so a crash mid-send can't suppress it
        del uids[:-2 * PAGE]
        atomic_write_json(self.cursor, self.taken)
        self.burst = []

    async def catch_up(self, chat: str):
        """Take what reached `chat` while the socket was down: the newest
        page's messages above the newest one taken. Plow pages only backwards,
        so a gap of more than one page loses its oldest texts; what sits below a
        taken text is history, never replayed; a text still in the burst counts
        as taken. A busy or failing Plow raises into the reconnect retry."""
        r = await self.client.get(f"/v1/chats/{chat}/messages", params={"limit": PAGE})
        if r.status_code == 429 or r.status_code >= 500:
            r.raise_for_status()
        if r.is_error:
            log(f"HTTP {r.status_code} reading chat {chat}; skipped")
            return
        uids = self.taken[chat] + [m["uid"] for m in self.burst]
        for message in reversed(list(takewhile(lambda m: m["uid"] not in uids, r.json()["data"]))):
            await self.take(message)

    async def reply(self, chat: str, text: str) -> str:
        if not text.strip():
            raise ValueError("reply text is empty; not sent")
        return sent(await self.client.post(f"/v1/chats/{chat}/messages", json={"body": text}), "reply")

    async def upload(self, chat: str, file: Path) -> str:
        """Declare `file` on `chat` and upload its bytes; returns its attachment uid."""
        data = await asyncio.to_thread(file.read_bytes)
        r = await self.client.post(f"/v1/chats/{chat}/attachments", json={
            "filename": file.name, "content_type": content_type_of(file), "size_bytes": len(data),
        })
        if r.is_error:
            raise RuntimeError(f"Plow refused {file.name} with HTTP {r.status_code}: {r.text}; nothing sent")
        attachment = r.json()
        # Its own client: the signed URL is the provider's, and the shared client
        # would hand it the Plow token.
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as provider:
            r = await provider.put(attachment["upload_url"], content=data, headers=attachment["upload_headers"])
        # By status alone: an httpx error would print the signed URL, and Plow
        # sends without checking the upload landed.
        if not r.is_success:
            raise RuntimeError(f"uploading {file.name} failed with HTTP {r.status_code}; nothing sent")
        return attachment["uid"]

    async def send_file(self, chat: str, paths: list[str], caption: str = "") -> str:
        """Send local files on `chat` as one message, or nothing if any fails."""
        if not 1 <= len(paths) <= MAX_ATTACHMENTS:
            raise ValueError(f"{len(paths)} files given; a message takes 1 to {MAX_ATTACHMENTS}; nothing sent")
        files = [attachable(path) for path in paths]  # all of them before Plow hears of any
        uids = [await self.upload(chat, file) for file in files]
        return sent(await self.client.post(f"/v1/chats/{chat}/messages", json={
            "attachment_uids": uids, "body": caption,
        }), "file")

    async def run(self):
        """Hold the socket forever: a fresh ticket per connect, catch up every
        known chat once it is open, take each message frame as it lands, and
        hand the burst over once it goes quiet."""
        ws_url = str(self.client.base_url).rstrip("/").replace("http", "ws", 1) + "/v1/ws"
        while True:
            try:
                r = await self.client.post("/v1/ws/ticket", json={})
                r.raise_for_status()
                async with websockets.connect(f"{ws_url}?ticket={r.json()['ticket']}") as ws:
                    for chat in list(self.taken):
                        await self.catch_up(chat)
                    log("connected")
                    while True:
                        quiet = max(0, self.due - asyncio.get_running_loop().time()) if self.burst else None
                        try:
                            raw = await asyncio.wait_for(ws.recv(), quiet)
                        except TimeoutError:
                            await self.flush()
                            continue
                        event = json.loads(raw)
                        if event.get("event_type") == "message_received":
                            await self.take(event["data"]["message"])
            except Exception as e:
                # The supervisor is a detached task: it must never die quietly
                # while the sous waits for texts.
                log(f"reconnecting after {type(e).__name__}: {e}")
            await asyncio.sleep(RECONNECT_DELAY)


# A tool here runs the Bridge method of the same name, with its arguments as keywords.
TOOLS = {tool.name: tool for tool in (
    types.Tool(
        name="reply",
        description=(
            "Send a text message to a Plow chat (iMessage); pass the `chat` "
            "attribute of the <channel source=\"plow-chat\"> tag you are answering."
        ),
        inputSchema={
            "type": "object",
            "properties": {"chat": {"type": "string"}, "text": {"type": "string"}},
            "required": ["chat", "text"],
        },
    ),
    types.Tool(
        name="send_file",
        description=(
            "Send 1 to 4 local files (images, PDFs, video, audio or documents, up to 100 MiB "
            "each) to a Plow chat (iMessage) as one message, with an optional caption; pass "
            "the `chat` attribute of the <channel source=\"plow-chat\"> tag you are answering. "
            "If any file fails, nothing is sent."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_ATTACHMENTS},
                "caption": {"type": "string"},
            },
            "required": ["chat", "paths"],
        },
    ),
)}


async def call(bridge: Bridge, name: str, arguments: dict) -> str:
    if name not in TOOLS:
        raise ValueError(f"unknown tool {name}")
    return await getattr(bridge, name)(**arguments)


async def run_server(kitchen: str):
    directory = plow_chat_dir(kitchen)
    credentials = read_credentials(directory)
    server = Server("plow-chat")
    bridge = None  # bound once the HTTP client and stdio streams are open

    @server.list_tools()
    async def list_tools():
        return list(TOOLS.values())

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        return [types.TextContent(type="text", text=await call(bridge, name, arguments))]

    init_options = server.create_initialization_options(
        notification_options=NotificationOptions(),
        experimental_capabilities={CHANNEL_CAPABILITY: {}},
    )
    _refuse_if_deaf(init_options.capabilities)

    headers = {"Authorization": f"Bearer {credentials['PLOW_AGENT_TOKEN']}"}
    async with httpx.AsyncClient(base_url=credentials["PLOW_API_BASE"], headers=headers, timeout=30) as client, \
            stdio_server() as (read_stream, write_stream):
        bridge = Bridge(client, lambda content, meta: push(write_stream, content, meta), directory / "last_seen.json")
        supervisor = asyncio.create_task(bridge.run())
        try:
            await server.run(read_stream, write_stream, init_options)
        finally:
            supervisor.cancel()


def main(kitchen: str):
    asyncio.run(run_server(kitchen))
