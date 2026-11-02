"""Tests for the plow-chat channel server, against a fake Plow API."""
import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest

from claude_kitchen import plow_chat
from claude_kitchen.plow_chat import Bridge, call, content_type_of, read_credentials
from tests.fake_plow import SIGNATURE, TOKEN, FakePlow

SAM, PLUCAS, STRANGER = "+15550000001", "+15550000002", "+15550000099"
HOME = "cht_home"


@pytest.fixture(autouse=True)
def quick_reconnect(monkeypatch):
    monkeypatch.setattr(plow_chat, "RECONNECT_DELAY", 0.05)
    monkeypatch.setattr(plow_chat, "BURST_SECONDS", 0.1)


async def eventually(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def run_bridge(scenario, fake=None, cursor=None):
    """Run `scenario(fake, bridge, delivered)` with the bridge's socket
    supervisor live against `fake` (a fresh one by default). Pass the same
    fake and cursor twice to restart the server against the same chat."""
    fake = fake or FakePlow()
    cursor = cursor or Path(tempfile.mkdtemp()) / "last_seen.json"

    async def main():
        base_url, server = await fake.serve()
        delivered = []

        async def deliver(content, meta):
            delivered.append((content, meta))

        async with httpx.AsyncClient(base_url=base_url, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
            bridge = Bridge(client, deliver, cursor)
            supervisor = asyncio.create_task(bridge.run())
            await eventually(lambda: fake.sockets)
            try:
                await scenario(fake, bridge, delivered)
            finally:
                supervisor.cancel()
                server.should_exit = True
    asyncio.run(main())


@pytest.mark.parametrize("handle, name", [(PLUCAS, "Patrick"), (STRANGER, STRANGER)], ids=["named", "unnamed"])
def test_any_member_is_delivered_by_name_without_the_chats_history(handle, name):
    async def scenario(fake, bridge, delivered):
        await fake.inbound(HOME, handle, "can you look at the mba?")
        await eventually(lambda: delivered)
        await fake.drop_sockets()  # the catch-up after reconnecting must not replay it either
        await eventually(lambda: fake.sockets)
        await fake.inbound(HOME, SAM, "marker")
        await eventually(lambda: len(delivered) >= 2)
        await asyncio.sleep(0.2)
        assert delivered == [("can you look at the mba?", {"from": name, "chat": HOME}),
                             ("marker", {"from": "Sam", "chat": HOME})]

    fake = FakePlow()
    fake.add(HOME, SAM, "said before the sous was listening")
    run_bridge(scenario, fake)


def test_a_reconnect_inside_a_new_chats_first_burst_never_replays_its_history(monkeypatch):
    monkeypatch.setattr(plow_chat, "BURST_SECONDS", 0.5)

    async def scenario(fake, bridge, delivered):
        await fake.inbound(HOME, SAM, "fresh")
        await asyncio.sleep(0.05)  # heard, but its burst is still open
        await fake.drop_sockets()
        await eventually(lambda: delivered)
        await asyncio.sleep(0.3)
        assert [c for c, _ in delivered] == ["fresh"]

    fake = FakePlow()
    fake.add(HOME, SAM, "said before the sous was listening")
    run_bridge(scenario, fake)


def test_reply_sends_and_its_echo_never_reaches_the_sous():
    async def scenario(fake, bridge, delivered):
        await bridge.reply(HOME, "on it")
        assert fake.sent == [(HOME, "on it")]
        await fake.inbound(HOME, SAM, "marker")
        await eventually(lambda: delivered)
        assert [c for c, _ in delivered] == ["marker"]
    run_bridge(scenario)


@pytest.mark.parametrize("flaky", [False, True], ids=["normal", "503-then-retry"])
def test_reconnect_catches_up_without_duplicates(flaky):
    async def scenario(fake, bridge, delivered):
        await fake.inbound(HOME, SAM, "before")
        await eventually(lambda: delivered)
        await fake.drop_sockets()
        fake.add(HOME, SAM, "during the gap")
        if flaky:
            fake.flaky.add(HOME)  # the first catch-up read answers 503
        await eventually(lambda: len(delivered) == 2)
        await fake.inbound(HOME, SAM, "after")
        await eventually(lambda: len(delivered) == 3)
        await asyncio.sleep(0.2)
        assert [c for c, _ in delivered] == ["before", "during the gap", "after"]
    run_bridge(scenario)


def test_restart_delivers_texts_sent_while_down_exactly_once(tmp_path):
    fake, cursor = FakePlow(), tmp_path / "last_seen.json"

    async def before_restart(fake, bridge, delivered):
        await fake.inbound(HOME, SAM, "before restart")
        await eventually(lambda: delivered)

    async def after_restart(fake, bridge, delivered):
        await eventually(lambda: delivered)
        await asyncio.sleep(0.2)
        assert [c for c, _ in delivered] == ["while down"]

    run_bridge(before_restart, fake, cursor)
    fake.add(HOME, SAM, "while down")
    run_bridge(after_restart, fake, cursor)
    assert cursor.stat().st_mode & 0o777 == 0o600


def test_a_late_text_never_replays_an_earlier_one_after_restart(tmp_path):
    fake, cursor = FakePlow(), tmp_path / "last_seen.json"

    async def before_restart(fake, bridge, delivered):
        await fake.inbound(HOME, SAM, "sent second, landed first")
        await fake.inbound(HOME, PLUCAS, "sent first, landed second", late=True)
        await eventually(lambda: len(delivered) == 2)

    async def after_restart(fake, bridge, delivered):
        await fake.inbound(HOME, SAM, "marker")
        await eventually(lambda: delivered)
        await asyncio.sleep(0.2)
        assert [c for c, _ in delivered] == ["marker"]

    run_bridge(before_restart, fake, cursor)
    run_bridge(after_restart, fake, cursor)


@pytest.mark.parametrize("live", [True, False], ids=["socket", "catch-up"])
def test_one_speakers_back_to_back_texts_reach_the_sous_as_one(live, tmp_path):
    fake, cursor = FakePlow(), tmp_path / "last_seen.json"
    texts = [(SAM, "add this to the page"), (SAM, "https://example.com/paper"), (PLUCAS, "me too")]

    async def first_contact(fake, bridge, delivered):  # catch-up reads only chats the bridge has heard from
        await fake.inbound(HOME, SAM, "hi")
        await eventually(lambda: delivered)

    async def burst(fake, bridge, delivered):
        if live:
            for handle, body in texts:
                await fake.inbound(HOME, handle, body)
        await eventually(lambda: len(delivered) >= 2)
        await asyncio.sleep(0.3)
        assert [(c, m["from"]) for c, m in delivered] == [
            ("add this to the page\n\nhttps://example.com/paper", "Sam"), ("me too", "Patrick")]

    run_bridge(first_contact, fake, cursor)
    if not live:
        for handle, body in texts:
            fake.add(HOME, handle, body)
    run_bridge(burst, fake, cursor)


def test_reads_the_credential_file_plow_agents_mint_writes(tmp_path):
    (tmp_path / "plow-credentials").write_text(
        f"PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN={TOKEN}\n# plow-agent-uid: 73f5\n")
    assert read_credentials(tmp_path) == {"PLOW_API_BASE": "https://api.plow.co", "PLOW_AGENT_TOKEN": TOKEN}


@pytest.fixture
def shot(tmp_path):
    file = tmp_path / "shot.png"
    file.write_bytes(b"\x89PNG fake image")
    return file


def test_send_file_uploads_without_the_plow_token_and_sends_the_attachment(shot):
    async def scenario(fake, bridge, delivered):
        result = await call(bridge, "send_file", {"chat": HOME, "path": str(shot), "caption": "the bug"})

        (uid, attachment), = fake.attachments.items()
        assert (attachment["filename"], attachment["content_type"]) == ("shot.png", "image/png")
        assert attachment["size_bytes"] == shot.stat().st_size
        uploaded, headers = attachment["upload"]
        assert uploaded == shot.read_bytes()
        assert "authorization" not in headers
        assert headers["x-provider-grant"] == uid  # every returned header goes back
        assert attachment["sent_with"] == "the bug"
        assert result.startswith("sent msg_")
        await asyncio.sleep(0.1)
        assert delivered == []  # the echo of our own send stays out of the sous
    run_bridge(scenario)


def test_send_file_needs_no_caption(shot):
    async def scenario(fake, bridge, delivered):
        await call(bridge, "send_file", {"chat": HOME, "path": str(shot)})
        assert fake.sent == [(HOME, "")]
    run_bridge(scenario)


@pytest.mark.parametrize("status", [403, 302])
def test_a_failed_upload_is_never_sent_and_keeps_the_signed_url_out_of_the_error(shot, status):
    async def scenario(fake, bridge, delivered):
        fake.upload_status = status
        with pytest.raises(RuntimeError, match=f"HTTP {status}") as failure:
            await bridge.send_file(HOME, str(shot))
        assert SIGNATURE not in str(failure.value)
        assert fake.sent == []
    run_bridge(scenario)


@pytest.mark.parametrize("send", [
    lambda bridge, file: bridge.reply(HOME, "on it"),
    lambda bridge, file: bridge.send_file(HOME, str(file)),
], ids=["reply", "send_file"])
def test_a_held_send_is_a_failure(shot, send):
    async def scenario(fake, bridge, delivered):
        fake.hold = True
        with pytest.raises(RuntimeError, match="never deliver"):
            await send(bridge, shot)
    run_bridge(scenario)


def test_a_type_plow_refuses_says_why(tmp_path):
    file = tmp_path / "movie.txt"
    file.write_text("words")

    async def scenario(fake, bridge, delivered):
        with pytest.raises(RuntimeError, match="movie.txt.*415.*unsupported_content_type"):
            await bridge.send_file(HOME, str(file))
        assert fake.sent == []
    run_bridge(scenario)


@pytest.mark.parametrize("name, size, why", [
    ("huge.png", plow_chat.MAX_ATTACHMENT_BYTES + 1, "100 MiB"),
    ("empty.png", 0, "100 MiB"),
    ("mystery.xyzzy", 5, "file type"),
])
def test_a_file_plow_cannot_take_fails_before_reaching_plow(tmp_path, name, size, why):
    file = tmp_path / name
    with file.open("wb") as f:
        f.truncate(size)  # sparse, so the oversized case costs no disk

    async def scenario(fake, bridge, delivered):
        with pytest.raises(ValueError, match=why):
            await bridge.send_file(HOME, str(file))
        assert fake.attachments == {}
    run_bridge(scenario)


def test_a_missing_file_fails_clearly(tmp_path):
    async def scenario(fake, bridge, delivered):
        with pytest.raises(ValueError, match="not a file"):
            await bridge.send_file(HOME, str(tmp_path / "gone.png"))
    run_bridge(scenario)


@pytest.mark.parametrize("name, expected", [
    ("a.PNG", "image/png"),
    ("a.jpg", "image/jpeg"),
    ("a.heic", "image/heic"),
    ("a.webp", "image/webp"),
    ("a.ico", "image/x-icon"),
    ("a.pdf", "application/pdf"),
    ("a.vcf", "text/vcard"),
    ("a.wav", "audio/x-wav"),  # Python 3.14 guesses audio/vnd.wave
    ("a.avi", "video/x-msvideo"),  # Python 3.14 guesses video/vnd.avi
    ("a.midi", "audio/midi"),
    ("a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("a.tar.gz", "application/x-gzip"),
])
def test_declares_the_type_plow_names(name, expected):
    assert content_type_of(Path(name)) == expected


def test_only_named_tools_reach_the_bridge():
    async def scenario(fake, bridge, delivered):
        with pytest.raises(ValueError, match="unknown tool run"):
            await call(bridge, "run", {})
        assert (await call(bridge, "reply", {"chat": HOME, "text": "hi"})).startswith("sent ")
    run_bridge(scenario)
