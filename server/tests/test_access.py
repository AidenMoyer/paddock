"""Communities, library access, media-only mode, comments and calls: who sees and changes what."""

import json

import pytest
from conftest import PASSWORD, client

from paddock import db


def login(app, admin, name, ip):
    r = admin.post("/api/users", json={"username": name, "password": PASSWORD, "is_admin": False})
    assert r.status_code == 200, r.text
    c = client(app, ip=ip, headers={"x-paddock": "1"})
    c.__enter__()
    assert c.post("/api/auth/login", json={"username": name, "password": PASSWORD}).status_code == 200
    c.user_id = r.json()["id"]
    return c


@pytest.fixture(scope="module")
def people(app, admin):
    kid, pal = login(app, admin, "junior", "192.168.1.101"), login(app, admin, "buddy", "192.168.1.102")
    libs = {l["kind"]: l for l in admin.get("/api/libraries").json()}
    yield kid, pal, libs
    # leave the shared test server as other tests expect it: everything open, nobody limited
    for l in libs.values():
        admin.put(f"/api/libraries/{l['id']}/access", json={"visibility": "everyone", "communities": []})
    for c in admin.get("/api/communities").json():
        admin.delete(f"/api/communities/{c['id']}")
    db.x("DELETE FROM user_settings")
    db.set_setting("media_only_everyone", False)
    for c in (kid, pal):
        c.__exit__(None, None, None)


def ids(c, **params):
    return {i["id"] for i in c.get("/api/items", params={"limit": 500, **params}).json()}


def test_libraries_can_be_limited_to_communities(admin, people):
    kid, pal, libs = people
    eng, movies = libs["engineering"], libs["movies"]
    r = admin.post("/api/communities", json={"name": "Race team", "members": [pal.user_id]})
    assert r.status_code == 200, r.text
    team = r.json()["id"]
    assert admin.post("/api/communities", json={"name": "race TEAM"}).status_code == 409
    assert admin.put(f"/api/libraries/{eng['id']}/access",
                     json={"visibility": "communities", "communities": [{"id": team, "access": "read"}]}).status_code == 200
    eng_items = ids(admin, library_id=eng["id"])
    assert eng_items

    # kid isn't on the team: the library, its items, files and thumbnails don't exist for them
    assert eng["id"] not in [l["id"] for l in kid.get("/api/libraries").json()]
    assert not ids(kid, library_id=eng["id"]) and not (ids(kid) & eng_items)
    some = next(iter(eng_items))
    for path in (f"/api/items/{some}", f"/api/items/{some}/file", f"/api/items/{some}/thumb", f"/api/items/{some}/comments"):
        assert kid.get(path).status_code == 404, path
    assert not any(r["key"] == "engineering" for r in kid.get("/api/home").json()["rows"])
    assert kid.post("/api/share", json={"item_id": some}).status_code == 404

    # pal sees it, read-only
    assert eng_items <= ids(pal)
    assert pal.get(f"/api/items/{some}").json()["can_write"] is False
    assert pal.patch(f"/api/items/{some}", json={"title": "renamed"}).status_code == 403
    assert pal.post("/api/items/trash", json={"ids": [some]}).status_code == 403
    assert pal.patch(f"/api/items/{some}", json={"favorite": True}).status_code == 200      # favorites are personal

    # write access lets them change things; open libraries stay open to everyone
    admin.put(f"/api/libraries/{eng['id']}/access", json={"visibility": "communities", "communities": [{"id": team, "access": "write"}]})
    assert pal.get(f"/api/items/{some}").json()["can_write"] is True
    assert ids(kid, library_id=movies["id"])


def test_media_only_mode(admin, people):
    kid, pal, libs = people
    assert admin.patch(f"/api/users/{kid.user_id}", json={"media_only": True}).status_code == 200
    me = kid.get("/api/auth/state").json()["user"]
    assert me["media_only"] is True
    kinds = {i["library_kind"] for i in kid.get("/api/items", params={"limit": 500}).json()}
    assert kinds and kinds <= {"movies", "shows", "music", "photos", "home_videos"}
    home = kid.get("/api/home").json()
    assert home["media_only"] and not any(r["key"] in ("projects", "engineering") for r in home["rows"])
    assert kid.get("/api/projects").json() == []
    assert kid.post("/api/projects", json={"name": "Nope"}).status_code == 403
    assert kid.get("/api/ai/info").json()["available"] is False
    assert kid.post("/api/ai/chat", json={"message": "hi"}).status_code == 403
    movie = next(iter(ids(kid, library_id=libs["movies"]["id"])))
    assert kid.patch(f"/api/items/{movie}", json={"title": "x"}).status_code == 403
    # phone backups still arrive
    r = kid.post("/api/uploads", json={"filename": "IMG_1.jpg", "size": 10, "sha256": "a" * 64})
    assert r.status_code == 200 and r.json()["status"] == "new", r.text
    # and everyone-but-admins at once
    admin.patch(f"/api/users/{kid.user_id}", json={"media_only": False})
    admin.put("/api/admin/general", json={"media_only_everyone": True})
    assert pal.get("/api/auth/state").json()["user"]["media_only"] is True
    assert admin.get("/api/auth/state").json()["user"]["media_only"] is False
    admin.put("/api/admin/general", json={"media_only_everyone": False})
    assert pal.get("/api/auth/state").json()["user"]["media_only"] is False


def test_projects_follow_communities(admin, people):
    kid, pal, _ = people
    team = next(c["id"] for c in admin.get("/api/communities").json() if c["name"] == "Race team")
    pid = pal.post("/api/projects", json={"name": "Turbo build", "kind": "build"}).json()["id"]
    assert pid in [p["id"] for p in kid.get("/api/projects").json()]          # not shared with anyone: open to all
    assert pal.put(f"/api/projects/{pid}/access", json={"communities": [{"id": team, "access": "write"}]}).status_code == 200
    assert pid not in [p["id"] for p in kid.get("/api/projects").json()]
    assert kid.get(f"/api/projects/{pid}").status_code == 404
    assert kid.post(f"/api/projects/{pid}/notes", json={"body": "hi"}).status_code == 404
    assert pal.get(f"/api/projects/{pid}").json()["kind"] == "build"
    assert pal.post(f"/api/projects/{pid}/notes", json={"body": "ordered the turbo"}).status_code == 200


def test_comments_stay_in_their_community(admin, people):
    kid, pal, libs = people
    fam = admin.post("/api/communities", json={"name": "Family", "members": [kid.user_id]}).json()["id"]
    movie = next(iter(ids(admin, library_id=libs["movies"]["id"])))
    r = kid.post(f"/api/items/{movie}/comments", json={"body": "best scene at 1:23", "community_id": fam, "at": 83})
    assert r.status_code == 200, r.text
    assert kid.post(f"/api/items/{movie}/comments", json={"body": "x", "community_id": 999}).status_code == 403
    assert pal.post(f"/api/items/{movie}/comments", json={"body": "sneaky", "community_id": fam}).status_code == 403
    assert kid.post(f"/api/items/{movie}/comments", json={"body": "hello everyone"}).status_code == 200

    seen = lambda c: [x["body"] for x in c.get(f"/api/items/{movie}/comments").json()["comments"]]
    assert seen(kid) == ["best scene at 1:23", "hello everyone"]
    assert seen(pal) == ["hello everyone"]                   # pal isn't in Family
    assert len(seen(admin)) == 2
    assert {p["name"] for p in kid.get(f"/api/items/{movie}/comments").json()["places"]} == {"Everyone", "Family"}

    # a community can switch comments off
    admin.patch(f"/api/communities/{fam}", json={"settings": {"comments": False}})
    assert seen(kid) == ["hello everyone"]
    assert kid.post(f"/api/items/{movie}/comments", json={"body": "x", "community_id": fam}).status_code == 403
    # only the author (or an admin) edits; others can't delete
    mine = kid.get(f"/api/items/{movie}/comments").json()["comments"][0]
    assert pal.delete(f"/api/comments/{mine['id']}").status_code == 403
    assert kid.patch(f"/api/comments/{mine['id']}", json={"body": "hello all"}).json()["body"] == "hello all"
    assert admin.delete(f"/api/comments/{mine['id']}").status_code == 200


def test_calls_ring_people_in_your_communities(admin, people):
    kid, pal, _ = people
    with pal.websocket_connect("/api/events/ws") as ev:
        assert json.loads(ev.receive_text())["type"] == "hello"
        # kid is in Family, pal isn't: no shared community, no call
        assert kid.post("/api/calls", json={"user_ids": [pal.user_id]}).status_code == 403
        fam = next(c for c in admin.get("/api/communities").json() if c["name"] == "Family")
        admin.put(f"/api/communities/{fam['id']}/members",
                  json={"members": [{"user_id": kid.user_id}, {"user_id": pal.user_id}]})
        admin.patch(f"/api/communities/{fam['id']}", json={"settings": {"voice": True}})
        r = kid.post("/api/calls", json={"user_ids": [pal.user_id]})
        assert r.status_code == 200, r.text
        call = r.json()
        ring = json.loads(ev.receive_text())
        assert ring["type"] == "ring" and ring["call"]["id"] == call["id"] and ring["call"]["host"] == "junior"
        places = pal.get("/api/voice/places").json()
        assert any(c["id"] == call["id"] for c in places["calls"])
        assert any(l["name"] == "Family" for l in places["lounges"])
        # only the people in the call can enter its room
        with pal.websocket_connect(f"/api/voice/{call['room']}/ws") as v:
            assert json.loads(v.receive_text())["type"] == "hello"
        assert pal.delete(f"/api/calls/{call['id']}").status_code == 403
        assert kid.delete(f"/api/calls/{call['id']}").status_code == 200
        assert json.loads(ev.receive_text())["type"] == "call_ended"


def test_community_watch_party_with_a_queue(admin, people):
    kid, pal, libs = people
    fam = next(c for c in admin.get("/api/communities").json() if c["name"] == "Family")
    admin.put(f"/api/communities/{fam['id']}/members", json={"members": [{"user_id": kid.user_id}]})
    movies = sorted(ids(admin, library_id=libs["movies"]["id"]))
    with kid.websocket_connect("/api/events/ws") as ev:
        json.loads(ev.receive_text())
        r = admin.post("/api/parties", json={"item_id": movies[0], "community_id": fam["id"]})
        assert r.status_code == 200, r.text
        party = r.json()
        note = json.loads(ev.receive_text())
        assert note["type"] == "party" and note["party"]["community"] == "Family"
    assert party["id"] in [p["id"] for p in kid.get("/api/parties").json()]
    assert party["id"] not in [p["id"] for p in pal.get("/api/parties").json()]      # pal isn't in Family
    assert pal.get(f"/api/parties/{party['id']}").status_code == 404
    with kid.websocket_connect(f"/api/parties/{party['id']}/ws") as ws:
        hello = json.loads(ws.receive_text())
        assert hello["party"]["queue"] == []
        ws.send_text(json.dumps({"type": "queue_add", "item_id": movies[1]}))
        while (msg := json.loads(ws.receive_text()))["type"] != "queue":
            pass
        assert [q["item_id"] for q in msg["queue"]] == [movies[1]] and msg["by"] == "junior"
        ws.send_text(json.dumps({"type": "next", "from": movies[0]}))
        while (msg := json.loads(ws.receive_text()))["type"] != "state":
            pass
        assert msg["state"]["item_id"] == movies[1] and msg["state"]["intent_playing"] is True
        ws.send_text(json.dumps({"type": "next", "from": movies[1]}))
        while (msg := json.loads(ws.receive_text()))["type"] not in ("queue_empty", "error"):
            pass
        assert msg["type"] == "queue_empty"
    admin.delete(f"/api/parties/{party['id']}")
