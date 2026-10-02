import asyncio
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from blackjack import Player
from server import ROOMS, SESSIONS, Room, app


async def receive(ws, kind, predicate=lambda _: True):
    for _ in range(50):
        msg = await ws.receive_json(timeout=3)
        if msg.get("type") == kind and predicate(msg):
            return msg
    raise AssertionError(f"未收到 {kind}")


@pytest.fixture(autouse=True)
def clear_rooms():
    ROOMS.clear()
    SESSIONS.clear()
    yield
    ROOMS.clear()
    SESSIONS.clear()


def test_http_websocket_full_game_and_host_permissions():
    async def flow():
        async with TestClient(TestServer(app())) as client:
            for body in ([], {"name": "", "buyin": 100}, {"name": "房主", "buyin": True}):
                response = await client.post("/api/create", json=body)
                assert response.status == 400
            host = await (await client.post("/api/create", json={"name": "房主", "buyin": 100})).json()
            guest = await (await client.post("/api/join", json={"room": host["room"], "name": "闲家", "buyin": 100})).json()
            room = ROOMS[host["room"]]
            room.bot_delay_ms = 1
            hw = await client.ws_connect("/ws?token=" + host["token"])
            gw = await client.ws_connect("/ws?token=" + guest["token"])
            hs = (await receive(hw, "state"))["state"]
            gs = (await receive(gw, "state"))["state"]
            assert hs["canStart"] and hs["host"] == hs["you"]
            assert gs["host"] != gs["you"]
            await gw.send_json({"type": "chat", "text": "<script>你好</script>", "sender": "冒名"})
            hc = await receive(hw, "chat")
            gc = await receive(gw, "chat")
            assert hc == gc
            assert hc["message"]["sender"] == "闲家"
            await gw.send_json({"type": "chat", "text": "x" * 201})
            await receive(gw, "error")
            for kind in ("start", "settle", "kick", "bot_speed"):
                await gw.send_json({"type": kind, "playerId": hs["you"], "delayMs": 200})
                await receive(gw, "error")
            await hw.send_json({"type": "bot"})
            await receive(hw, "error")
            await hw.send_json({"type": "start"})
            betting = (await receive(hw, "state", lambda m: m["state"]["phase"] == "betting"))["state"]
            assert betting["options"]["bet"]
            await hw.send_json({"type": "settle"})
            await receive(hw, "error")
            # Host 19, guest 16, dealer 17. No natural blackjack.
            room.table.pile = list(reversed([(10, "s"), (10, "h"), (10, "d"), (9, "s"), (6, "h"), (7, "d")]))
            await hw.send_json({"type": "action", "action": "bet", "amount": 10})
            await receive(hw, "state", lambda m: m["state"]["options"] is None)
            await gw.send_json({"type": "action", "action": "bet", "amount": 10})
            playing = (await receive(hw, "state", lambda m: m["state"]["phase"] == "playing"))["state"]
            assert playing["dealerCards"] == ["Td", "??"]
            assert playing["dealerTotal"] is None
            await gw.send_json({"type": "action", "action": "stand"})
            await receive(gw, "error")
            await hw.send_json({"type": "action", "action": []})
            await receive(hw, "error")
            await hw.send_json({"type": "action", "action": "stand"})
            await receive(gw, "state", lambda m: m["state"]["turn"] == gs["you"])
            await gw.send_json({"type": "action", "action": "stand"})
            complete = (await receive(hw, "state", lambda m: m["state"]["phase"] == "complete"))["state"]
            assert complete["dealerTotal"] == 17
            assert [p["stack"] for p in complete["players"]] == [110, 90]
            assert len(complete["chat"]) == 1
            assert any("第 1 局" in e["text"] for e in complete["events"])
            await hw.send_json({"type": "settle"})
            result = await receive(hw, "settlement")
            assert result == await receive(gw, "settlement")
            assert [p["net"] for p in result["ranking"]] == [10, -10]
            assert not ROOMS and not SESSIONS
            assert (await client.get("/api/session?token=" + host["token"])).status == 401
    asyncio.run(flow())


def test_bankruptcy_rebuy_return_host_transfer_and_capacity():
    async def flow():
        async with TestClient(TestServer(app())) as client:
            host = await (await client.post("/api/create", json={"name": "房主", "buyin": 5})).json()
            guests = []
            for i in range(4):
                guests.append(await (await client.post("/api/join", json={"room": host["room"], "name": str(i), "buyin": 100})).json())
            response = await client.post("/api/join", json={"room": host["room"], "name": "第六位", "buyin": 100})
            assert response.status == 400
            room = ROOMS[host["room"]]
            hw = await client.ws_connect("/ws?token=" + host["token"])
            await receive(hw, "state")
            await hw.send_json({"type": "start"})
            await receive(hw, "state", lambda m: m["state"]["phase"] == "betting")
            room.table.pile = list(reversed([(10, "s"), (10, "h"), (6, "s"), (7, "h")]))
            await hw.send_json({"type": "action", "action": "bet", "amount": 5})
            playing = (await receive(hw, "state", lambda m: m["state"]["phase"] == "playing"))["state"]
            assert playing["players"][0]["stack"] == 0
            assert not playing["canRebuy"]
            assert not playing["players"][0]["bankrupt"]
            await hw.send_json({"type": "rebuy", "buyin": 100})
            await receive(hw, "error")
            await hw.send_json({"type": "action", "action": "stand"})
            bankrupt = (await receive(hw, "state", lambda m: m["state"]["phase"] == "complete"))["state"]
            assert bankrupt["canRebuy"] and bankrupt["players"][0]["bankrupt"]
            await hw.send_json({"type": "rebuy", "buyin": 50})
            rebought = (await receive(hw, "state", lambda m: m["state"]["players"][0]["stack"] == 50))["state"]
            assert rebought["players"][0]["buyin"] == 55
            before = room.members[rebought["you"]]
            await client.post("/api/leave", json={"token": host["token"]})
            assert room.host != before.id
            assert (await client.get("/api/session?token=" + host["token"])).status == 401
            returned = await (await client.post("/api/join", json={"room": host["room"], "returnKey": host["returnKey"]})).json()
            assert room.members[SESSIONS[returned["token"]][1]] is before
            assert before.stack == 5000 and before.buyin == 5500
            assert room.host != before.id
            await hw.close()
    asyncio.run(flow())


def test_queue_rebuy_and_no_duplicate_payout_when_returning_in_later_round():
    room = Room("ABCDEF", Player("h", "房主", 10000, 10000))
    p = Player("p", "闲家", 0, 10000)
    room.table.house_net = 10000  # Existing losses caused this member's bankruptcy.
    room.table.add(p)
    room.members[p.id] = p
    key = room.issue_return_key(p)
    room.start_hand()
    room.rebuy("p", 50)
    assert p.pending_buyin == 5000 and p.stack == 0 and p.buyin == 10000
    room.table.bet("h")
    room.start_hand()
    assert p.stack == 5000 and p.buyin == 15000 and p.pending_buyin == 0
    room.table.pile = list(reversed([(10, "s"), (10, "h"), (10, "d"), (9, "s"), (8, "h"), (7, "d")]))
    room.table.bet("h", 10)
    room.table.bet("p", 10)
    room.table.act("h", "stand")
    room.table.act("p", "stand")
    room.table.dealer_step()
    assert p.stack == 6000
    room.table.remove("p")
    room.start_hand()
    assert p not in room.table.players
    room.restore_member(key)
    assert not p.hands
    room.table.pile = list(reversed([(10, "s"), (10, "h"), (9, "s"), (7, "h")]))
    room.table.bet("h", 10)
    room.table.act("h", "stand")
    room.table.dealer_step()
    assert p.stack == 6000
    assert sum(p.stack - p.buyin for p in room.members.values()) + room.table.house_net == 0


def test_reconnect_replaces_socket_without_standing_and_disconnect_continues():
    async def flow():
        async with TestClient(TestServer(app())) as client:
            host = await (await client.post("/api/create", json={"name": "h", "buyin": 100})).json()
            room = ROOMS[host["room"]]
            room.bot_delay_ms = 1
            first = await client.ws_connect("/ws?token=" + host["token"])
            await receive(first, "state")
            await first.send_json({"type": "start"})
            await receive(first, "state", lambda m: m["state"]["phase"] == "betting")
            room.table.pile = list(reversed([(10, "s"), (10, "h"), (9, "s"), (7, "h")]))
            await first.send_json({"type": "action", "action": "bet", "amount": 10})
            await receive(first, "state", lambda m: m["state"]["phase"] == "playing")
            second = await client.ws_connect("/ws?token=" + host["token"])
            await first.receive()
            active = (await receive(second, "state"))["state"]
            assert active["phase"] == "playing" and active["turn"] == active["you"]
            await second.close()
            for _ in range(100):
                if room.table.phase == "complete":
                    break
                await asyncio.sleep(.01)
            assert room.table.phase == "complete"
            assert room.members[active["you"]].stack == 11000
    asyncio.run(flow())


def test_kick_invalidates_return_credentials_and_preserves_final_ranking():
    async def flow():
        async with TestClient(TestServer(app())) as client:
            host = await (await client.post("/api/create", json={"name": "h", "buyin": 100})).json()
            guest = await (await client.post("/api/join", json={"room": host["room"], "name": "g", "buyin": 100})).json()
            hw = await client.ws_connect("/ws?token=" + host["token"])
            gw = await client.ws_connect("/ws?token=" + guest["token"])
            gs = (await receive(gw, "state"))["state"]
            await hw.send_json({"type": "kick", "playerId": gs["you"]})
            await receive(gw, "kicked")
            response = await client.post("/api/join", json={"room": host["room"], "returnKey": guest["returnKey"]})
            assert response.status == 400
            room = ROOMS[host["room"]]
            assert len(room.ranking()) == 2
            await hw.close()
            await gw.close()
    asyncio.run(flow())


def test_assets_and_startup_url():
    async def flow():
        with patch("server.webbrowser.open") as browser:
            async with TestClient(TestServer(app(auto_open=True))) as client:
                for path, text in (("/", "电脑庄家"), ("/static/app.js", "blackjack-session"), ("/static/blackjack.css", ".blackjack-hand")):
                    response = await client.get(path)
                    assert response.status == 200
                    assert "no-store" in response.headers["Cache-Control"]
                    assert text in await response.text()
                await asyncio.sleep(.6)
            assert browser.call_args.args[0].startswith("http://localhost:8766/?fresh=")
    asyncio.run(flow())
