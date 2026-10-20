"""Watch parties (synced playback) and voice rooms, over real WebSockets."""

import json
import struct
import time

import pytest
from conftest import LAN, PASSWORD, client, item_by_title
from starlette.websockets import WebSocketDisconnect

from paddock.party import parties


def next_of(ws, kind, limit=20, action=None):
    for _ in range(limit):
        msg = json.loads(ws.receive_text())
        if msg["type"] == kind and (action is None or msg.get("action") == action):
            return msg
    raise AssertionError(f"no {kind} message")


@pytest.fixture(scope="module")
def friend(app, admin):
    """A second household member, signed in on another device."""
    r = admin.post("/api/users", json={"username": "sam", "password": PASSWORD, "is_admin": False})
    assert r.status_code == 200, r.text
    c = client(app, ip="192.168.1.88", headers={"x-paddock": "1"})
    c.__enter__()
    assert c.post("/api/auth/login", json={"username": "sam", "password": PASSWORD}).status_code == 200
    yield c
    c.__exit__(None, None, None)


@pytest.fixture(scope="module")
def outsider(app, admin):
    r = admin.post("/api/users", json={"username": "lee", "password": PASSWORD, "is_admin": False})
    assert r.status_code == 200, r.text
    c = client(app, ip="192.168.1.89", headers={"x-paddock": "1"})
    c.__enter__()
    assert c.post("/api/auth/login", json={"username": "lee", "password": PASSWORD}).status_code == 200
    yield c
    c.__exit__(None, None, None)


def test_watch_party_keeps_everyone_in_sync(admin, friend, outsider):
    movie = item_by_title(admin, "launch-day")
    p = admin.post("/api/parties", json={"item_id": movie["id"], "everyone": False, "invite": ["sam"]}).json()
    pid = p["id"]
    assert [x["id"] for x in friend.get("/api/parties").json()] == [pid]
    assert outsider.get("/api/parties").json() == []
    assert outsider.get(f"/api/parties/{pid}").status_code == 404

    with admin.websocket_connect(f"/api/parties/{pid}/ws") as host, friend.websocket_connect(f"/api/parties/{pid}/ws") as guest:
        hello = next_of(guest, "hello")
        assert hello["state"]["item_id"] == movie["id"] and hello["can_control"] is True
        assert {m["username"] for m in next_of(host, "members")["members"]} >= {"aiden"}

        # clock sync
        guest.send_text(json.dumps({"type": "ping", "t0": 123}))
        pong = next_of(guest, "pong")
        assert pong["t0"] == 123 and abs(pong["server"] - time.time()) < 5

        # the guest presses play at 42 s: the host's player is told where and when
        guest.send_text(json.dumps({"type": "play", "position": 42.0}))
        st = next_of(host, "state", action="play")
        assert st["state"]["playing"] and st["state"]["position"] == 42.0 and st["by"] == "sam"
        assert st["state"]["at"] >= st["server"]          # starts a moment in the future so both begin together
        assert next_of(guest, "state", action="play")  # the sender gets the authoritative clock too

        # someone buffers: everyone holds, then resumes together
        host.send_text(json.dumps({"type": "buffering", "value": True}))
        held = next_of(guest, "state", action="hold")
        assert held["action"] == "hold" and not held["state"]["playing"] and held["state"]["waiting_for"] == ["aiden"]
        host.send_text(json.dumps({"type": "buffering", "value": False}))
        resumed = next_of(guest, "state", action="resume")
        assert resumed["action"] == "resume" and resumed["state"]["playing"]

        # pause and seek go to everyone
        host.send_text(json.dumps({"type": "pause", "position": 50.5}))
        st = next_of(guest, "state", action="pause")
        assert not st["state"]["playing"] and st["state"]["position"] == 50.5
        guest.send_text(json.dumps({"type": "seek", "position": 10}))
        assert next_of(host, "state", action="seek")["state"]["position"] == 10

        # chat and reactions
        guest.send_text(json.dumps({"type": "chat", "text": "this part!"}))
        assert next_of(host, "chat")["text"] == "this part!"
        host.send_text(json.dumps({"type": "reaction", "emoji": "😂"}))
        assert next_of(guest, "reaction")["emoji"] == "😂"

        # the host ends it
        assert friend.delete(f"/api/parties/{pid}").status_code == 403
        assert admin.delete(f"/api/parties/{pid}").status_code == 200
        assert next_of(guest, "ended")["type"] == "ended"
    assert friend.get("/api/parties").json() == []


def test_host_only_control(admin, friend):
    movie = item_by_title(admin, "launch-day")
    pid = admin.post("/api/parties", json={"item_id": movie["id"], "control": "host"}).json()["id"]
    with friend.websocket_connect(f"/api/parties/{pid}/ws") as guest:
        assert next_of(guest, "hello")["can_control"] is False
        guest.send_text(json.dumps({"type": "play"}))
        assert "Only aiden" in next_of(guest, "error")["error"]
    admin.delete(f"/api/parties/{pid}")


def test_voice_room_relays_audio_between_party_members(admin, friend, outsider):
    movie = item_by_title(admin, "launch-day")
    pid = admin.post("/api/parties", json={"item_id": movie["id"], "everyone": False, "invite": ["sam"]}).json()["id"]
    room = f"party:{pid}"
    with admin.websocket_connect(f"/api/voice/{room}/ws") as a, friend.websocket_connect(f"/api/voice/{room}/ws") as b:
        me_a = json.loads(a.receive_text())["you"]
        json.loads(b.receive_text())
        frame = bytes([1]) + struct.pack(">H", 7) + b"\x01\x02opus-ish"
        a.send_bytes(frame)
        got = None
        for _ in range(10):
            msg = b.receive()
            if msg.get("bytes"):
                got = msg["bytes"]
                break
        assert got is not None and struct.unpack(">H", got[:2])[0] == me_a and got[2:] == frame

        # muted speakers aren't relayed; the roster tells everyone
        a.send_text(json.dumps({"type": "state", "muted": True}))
        a.send_bytes(frame)
        b.send_text(json.dumps({"type": "ping", "t0": 1}))
        for _ in range(10):
            msg = b.receive()
            assert not msg.get("bytes"), "a muted speaker's audio was relayed"
            if msg.get("text") and json.loads(msg["text"])["type"] == "pong":
                break

    with pytest.raises(WebSocketDisconnect):
        with outsider.websocket_connect(f"/api/voice/{room}/ws") as ws:
            ws.receive_text()
    admin.delete(f"/api/parties/{pid}")


def test_live_sockets_need_a_signed_in_member_from_this_site(app, admin):
    movie = item_by_title(admin, "launch-day")
    pid = admin.post("/api/parties", json={"item_id": movie["id"]}).json()["id"]
    anon = client(app, ip=LAN[0])
    with pytest.raises(WebSocketDisconnect):
        with anon.websocket_connect(f"/api/parties/{pid}/ws") as ws:
            ws.receive_text()
    # a page on another site can't ride the user's cookie
    with pytest.raises(WebSocketDisconnect):
        with admin.websocket_connect(f"/api/parties/{pid}/ws", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_text()
    admin.delete(f"/api/parties/{pid}")
    parties.reap()


def test_party_moves_on_together(admin, friend):
    movie, nxt = item_by_title(admin, "launch-day"), item_by_title(admin, "Turbo Time")
    pid = admin.post("/api/parties", json={"item_id": movie["id"]}).json()["id"]
    with admin.websocket_connect(f"/api/parties/{pid}/ws") as host, friend.websocket_connect(f"/api/parties/{pid}/ws") as guest:
        next_of(guest, "hello")
        host.send_text(json.dumps({"type": "item", "item_id": nxt["id"], "title": nxt["title"], "play": True}))
        st = next_of(guest, "state", action="item")
        assert st["state"]["item_id"] == nxt["id"] and st["state"]["intent_playing"] and st["state"]["position"] == 0
        assert st["state"]["at"] - st["server"] > 2      # time for everyone to open the next video
    admin.delete(f"/api/parties/{pid}")
