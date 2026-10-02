"""Local network blackjack room server. Run: python3 server.py"""
from __future__ import annotations

import asyncio
import json
import secrets
import socket
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from aiohttp import web, WSMsgType

from blackjack import MAX_PLAYERS, IDLE, Player, Table, money

ROOT = Path(__file__).parent
ROOMS = {}
SESSIONS = {}


def code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(6))
        if value not in ROOMS:
            return value


def validate_player(data):
    name = str(data.get("name", "")).strip()[:18]
    amount = data.get("buyin")
    if not name:
        raise ValueError("请填写昵称")
    if type(amount) is not int or amount < 5 or amount > 1000 or amount % 5:
        raise ValueError("带入金额须为 5～1000 元，且为 5 的倍数")
    return name, amount


class Room:
    def __init__(self, room_code, host):
        self.code = room_code
        self.host = host.id
        self.table = Table()
        self.table.add(host)
        self.members = {host.id: host}
        self.return_keys = {}
        self.closed = False
        self.sockets = {}
        self.bot_task = None
        self.bot_delay_ms = 1100
        self.table.events.append(f"{host.name} 创建房间")
        self.event_times = []
        self.chat_messages = []
        self.chat_sequence = 0

    def event_log(self):
        while len(self.event_times) < len(self.table.events):
            self.event_times.append(datetime.now().strftime("%H:%M:%S"))
        return [{"id": index + 1, "time": self.event_times[index], "text": event}
                for index, event in enumerate(self.table.events)]

    def payload(self, viewer):
        state = self.table.view(viewer)
        player = self.members.get(viewer)
        queued = sum(not p.departed and p.pending_buyin > 0 and p.connected
                     for p in self.table.players)
        can_rebuy = bool(player and not player.departed
                         and player.stack == 0 and not player.pending_buyin
                         and (self.table.phase in ("waiting", "complete") or not player.hands))
        state.update({"room": self.code, "you": viewer, "host": self.host,
                      "botDelayMs": self.bot_delay_ms,
                      "canRebuy": can_rebuy,
                      "events": self.event_log(), "chat": self.chat_messages[-200:],
                      "canStart": self.table.phase in ("waiting", "complete")
                      and len(self.table.eligible()) + queued >= 1})
        return state

    def issue_return_key(self, player):
        key = secrets.token_urlsafe(32)
        self.return_keys[key] = player.id
        return key

    def restore_member(self, key):
        player = self.members.get(self.return_keys.get(key)) if isinstance(key, str) else None
        if not player:
            raise ValueError("回房凭证无效，请重新加入")
        if player.departed:
            if sum(not p.departed for p in self.table.players) >= MAX_PLAYERS:
                raise ValueError("房间已满（闲家最多 5 人）")
            if not any(p is player for p in self.table.players):
                # A past round has already settled. Never reintroduce its hands
                # into the current round when a departed member returns.
                player.hands = []
                player.in_hand = False
                player.bet_decided = True
                player.insurance_decided = True
                player.round_start = player.stack
                self.table.add(player)
            player.departed = False
            player.connected = False
            player.last_action = "等待下一局" if self.table.phase not in ("waiting", "complete") else "已回房"
            self.table.events.append(f"{player.name} 回到房间")
        return player

    def rebuy(self, pid, amount):
        player = self.members.get(pid)
        if not player or player.departed or player.stack != 0:
            raise ValueError("当前不能重新入座")
        if player.pending_buyin:
            raise ValueError("已经申请重新入座")
        if self.table.phase not in ("waiting", "complete") and player.hands:
            raise ValueError("当前仍在牌局中，请等待本局结算")
        if type(amount) is not int or amount < 5 or amount > 1000 or amount % 5:
            raise ValueError("带入金额须为 5～1000 元，且为 5 的倍数")
        if self.table.phase in ("waiting", "complete"):
            player.stack = amount * 100
            player.buyin += amount * 100
            player.last_action = "重新入座"
            self.table.events.append(f"{player.name} 重新入座，带入 ¥{amount}")
        else:
            player.pending_buyin = amount * 100
            player.last_action = "等待下一局"
            self.table.events.append(f"{player.name} 已申请下一局重新入座，带入 ¥{amount}")

    def start_hand(self):
        if self.table.phase not in ("waiting", "complete"):
            raise ValueError("当前牌局尚未结束")
        ready = self.table.eligible()
        pending = [p for p in self.table.players if not p.departed and p.pending_buyin
                   and p.connected]
        if len(ready) + len(pending) < 1:
            raise ValueError("至少需要一位在线且有筹码的玩家")
        for player in pending:
            player.stack = player.pending_buyin
            player.buyin += player.pending_buyin
            self.table.events.append(f"{player.name} 重新入座，带入 ¥{money(player.pending_buyin):g}")
            player.pending_buyin = 0
        self.table.start()

    def ranking(self):
        players = sorted(self.members.values(), key=lambda p: (p.stack - p.buyin, p.stack, p.name), reverse=True)
        return [{"rank": index, "name": p.name, 
                 "buyin": money(p.buyin), "stack": money(p.stack), "net": money(p.stack - p.buyin)}
                for index, p in enumerate(players, 1)]

    async def finalize(self):
        if self.table.phase not in ("waiting", "complete"):
            raise ValueError("请等待当前牌局结算后再结束游戏")
        self.closed = True
        self.table.events.append("房主结束游戏并结算排行")
        ranking = self.ranking()
        if self.bot_task and not self.bot_task.done():
            self.bot_task.cancel()
            self.bot_task = None
        sockets = list(self.sockets.values())
        self.sockets.clear()
        ROOMS.pop(self.code, None)
        for token, value in list(SESSIONS.items()):
            if value[0] == self.code:
                del SESSIONS[token]
        for ws in sockets:
            if not ws.closed:
                try:
                    await ws.send_json({"type": "settlement", "ranking": ranking,
                                        "houseNet": money(self.table.house_net)})
                except ConnectionError:
                    pass
        await asyncio.gather(*(ws.close() for ws in sockets if not ws.closed))

    async def send_chat(self, pid, text):
        player = next((p for p in self.table.players if p.id == pid and not p.departed), None)
        if player is None:
            raise ValueError("玩家不在房间内")
        if type(text) is not str or not text.strip() or len(text.strip()) > 200:
            raise ValueError("聊天内容须为 1～200 个字符")
        self.chat_sequence += 1
        entry = {"id": self.chat_sequence, "senderId": pid, "sender": player.name,
                 "text": text.strip(), "time": datetime.now().strftime("%H:%M")}
        self.chat_messages.append(entry)
        self.chat_messages = self.chat_messages[-200:]
        for ws in list(self.sockets.values()):
            if not ws.closed:
                try:
                    await ws.send_json({"type": "chat", "message": entry})
                except ConnectionError:
                    pass

    async def broadcast(self):
        for pid, ws in list(self.sockets.items()):
            if not ws.closed:
                try:
                    await ws.send_json({"type": "state", "state": self.payload(pid)})
                except ConnectionError:
                    pass
        self.schedule_bot()

    def schedule_bot(self):
        if not self.closed and self.table.phase == "dealer" and (not self.bot_task or self.bot_task.done()):
            self.bot_task = asyncio.create_task(self.run_bot())

    async def run_bot(self):
        await asyncio.sleep(self.bot_delay_ms / 1000)
        if self.closed:
            return
        self.table.dealer_step()
        self.bot_task = None
        await self.broadcast()

    async def remove_member(self, pid, reason=None):
        player = next((p for p in self.table.players if p.id == pid and not p.departed), None)
        if player is None:
            raise ValueError("玩家不在房间内")
        ws = self.sockets.pop(pid, None)
        if ws and not ws.closed:
            if reason:
                await ws.send_json({"type": "kicked", "reason": reason})
            await ws.close()
        for token, value in list(SESSIONS.items()):
            if value == (self.code, pid):
                del SESSIONS[token]
        if reason:
            for key, member_id in list(self.return_keys.items()):
                if member_id == pid:
                    del self.return_keys[key]
        self.table.remove(pid, "被房主移出房间" if reason else "离开房间")
        if pid == self.host:
            successor = next((p for p in self.table.players if not p.departed and p.connected), None)
            if successor is None:
                successor = next((p for p in self.table.players if not p.departed), None)
            if successor:
                self.host = successor.id
            else:
                self.host = pid
        await self.broadcast()


async def read_json(request):
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("请求必须是对象")
        return data
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text="无效请求")


def fail(error):
    return web.json_response({"error": str(error)}, status=400)


async def create(request):
    try:
        name, buyin = validate_player(await read_json(request))
        player = Player(secrets.token_urlsafe(12), name, buyin * 100, buyin * 100, connected=False)
        room_code = code()
        ROOMS[room_code] = Room(room_code, player)
        return_key = ROOMS[room_code].issue_return_key(player)
        token = secrets.token_urlsafe(32)
        SESSIONS[token] = (room_code, player.id)
        return web.json_response({"room": room_code, "token": token, "returnKey": return_key})
    except ValueError as error:
        return fail(error)


async def join(request):
    try:
        data = await read_json(request)
        room_code = str(data.get("room", "")).strip().upper()
        room = ROOMS.get(room_code)
        if not room:
            raise ValueError("找不到该房间")
        return_key = data.get("returnKey")
        if return_key:
            player = room.restore_member(return_key)
        else:
            name, buyin = validate_player(data)
            if any(p.name.casefold() == name.casefold() for p in room.members.values()):
                raise ValueError("该昵称已在房间中使用；原玩家请从同一浏览器返回房间")
            player = Player(secrets.token_urlsafe(12), name, buyin * 100, buyin * 100, connected=False)
            room.table.add(player)
            room.members[player.id] = player
            room.table.events.append(f"{player.name} 加入房间")
            return_key = room.issue_return_key(player)
        if room.members[room.host].departed:
            room.host = player.id
        token = secrets.token_urlsafe(32)
        SESSIONS[token] = (room_code, player.id)
        await room.broadcast()
        return web.json_response({"room": room_code, "token": token, "returnKey": return_key})
    except ValueError as error:
        return fail(error)


async def leave_room(request):
    data = await read_json(request)
    session = SESSIONS.get(data.get("token"))
    if not session:
        raise web.HTTPUnauthorized(text="会话已失效")
    room = ROOMS.get(session[0])
    if not room:
        raise web.HTTPNotFound(text="房间已关闭")
    await room.remove_member(session[1])
    return web.json_response({"ok": True})


async def websocket(request):
    session = SESSIONS.get(request.query.get("token"))
    if not session:
        raise web.HTTPUnauthorized(text="登录已失效")
    room = ROOMS.get(session[0])
    if not room:
        raise web.HTTPNotFound(text="房间已关闭")
    pid = session[1]
    player = next((p for p in room.table.players if p.id == pid), None)
    if not player or player.departed:
        raise web.HTTPUnauthorized(text="已离开房间")
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    old = room.sockets.get(pid)
    room.sockets[pid] = ws
    if old and not old.closed:
        await old.close()
    if not player.connected:
        room.table.events.append(f"{player.name} 重新连接")
    player.connected = True
    await room.broadcast()
    async for message in ws:
        if message.type != WSMsgType.TEXT:
            continue
        try:
            data = json.loads(message.data)
            if not isinstance(data, dict):
                raise ValueError("无效操作")
            if room.closed or room.sockets.get(pid) is not ws or player.departed:
                raise ValueError("会话已失效")
            kind = data.get("type")
            if kind == "action":
                room.table.act(pid, data.get("action"), data.get("amount"))
            elif kind == "chat":
                await room.send_chat(pid, data.get("text"))
                continue
            elif kind == "start":
                if pid != room.host:
                    raise ValueError("只有房主可以发牌")
                room.start_hand()
            elif kind == "rebuy":
                room.rebuy(pid, data.get("buyin"))
            elif kind == "settle":
                if pid != room.host:
                    raise ValueError("只有房主可以结算")
                await room.finalize()
                continue
            elif kind == "bot_speed":
                if pid != room.host:
                    raise ValueError("只有房主可以调整电脑速度")
                delay = data.get("delayMs")
                if type(delay) is not int or delay < 200 or delay > 3000 or delay % 100:
                    raise ValueError("电脑速度参数无效")
                room.bot_delay_ms = delay
                if room.bot_task and not room.bot_task.done():
                    room.bot_task.cancel()
                    room.bot_task = None
            elif kind == "kick":
                if pid != room.host:
                    raise ValueError("只有房主可以踢出玩家")
                target = data.get("playerId")
                if target == pid:
                    raise ValueError("请使用退出房间按钮")
                victim = next((p for p in room.table.players if p.id == target and not p.departed), None)
                if victim is None:
                    raise ValueError("玩家不在房间内")
                await room.remove_member(target, "你已被房主移出房间")
                continue
            else:
                raise ValueError("未知操作")
            await room.broadcast()
        except (ValueError, json.JSONDecodeError) as error:
            await ws.send_json({"type": "error", "message": str(error)})
    if room.sockets.get(pid) is ws:
        room.sockets.pop(pid, None)
        player.connected = False
        room.table.events.append(f"{player.name} 断开连接")
        room.table.disconnect(pid)
        await room.broadcast()
    return ws


async def index(_request):
    return web.FileResponse(ROOT / "index.html")


async def network(_request):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            ip = probe.getsockname()[0]
    except OSError:
        ip = socket.gethostbyname(socket.gethostname())
    return web.json_response({"ip": ip})


async def session_status(request):
    session = SESSIONS.get(request.query.get("token"))
    room = ROOMS.get(session[0]) if session else None
    if not room or not any(p.id == session[1] and not p.departed for p in room.table.players):
        raise web.HTTPUnauthorized()
    return web.json_response({"ok": True})


async def health(_request):
    return web.json_response({"ok": True})


def app(auto_open=False):
    @web.middleware
    async def fresh_assets(request, handler):
        response = await handler(request)
        if request.path == "/" or request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    application = web.Application(client_max_size=1024 * 1024, middlewares=[fresh_assets])
    application.router.add_get("/", index)
    application.router.add_get("/ws", websocket)
    application.router.add_post("/api/create", create)
    application.router.add_post("/api/join", join)
    application.router.add_post("/api/leave", leave_room)
    application.router.add_get("/api/health", health)
    application.router.add_get("/api/network", network)
    application.router.add_get("/api/session", session_status)
    application.router.add_static("/static", ROOT / "static")
    async def open_host_page(_application):
        async def open_when_ready():
            await asyncio.sleep(0.5)
            webbrowser.open(f"http://localhost:8766/?fresh={time.time_ns()}")
        asyncio.create_task(open_when_ready())
    if auto_open:
        application.on_startup.append(open_host_page)
    async def cleanup(_application):
        tasks = [room.bot_task for room in ROOMS.values() if room.bot_task and not room.bot_task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    application.on_cleanup.append(cleanup)
    return application


if __name__ == "__main__":
    web.run_app(app(auto_open=True), host="0.0.0.0", port=8766, print=lambda text: print(text, flush=True))
