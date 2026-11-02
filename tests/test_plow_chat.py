"""Tests for the plow-chat channel server, against a fake Plow API."""
import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest

from claude_kitchen import plow_chat
from claude_kitchen.plow_chat import Bridge, read_credentials
from tests.fake_plow import TOKEN, FakePlow

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
