"""Scanning, metadata extraction, grouping, search, engineering filing."""

from pathlib import Path

from conftest import item_by_title, wait_jobs

from paddock import scanner, thumbs


def test_counts(admin):
    libs = {l["kind"]: l for l in admin.get("/api/libraries").json()}
    assert libs["movies"]["item_count"] == 2
    assert libs["shows"]["item_count"] == 2
    assert libs["music"]["item_count"] == 2
    assert libs["photos"]["item_count"] >= 3
    assert libs["red"]["item_count"] == 2           # 2 clips, spans + proxies folded in
    assert libs["engineering"]["item_count"] >= 5


def test_movie_title_parsing(admin):
    m = item_by_title(admin, "The Matrix")
    assert m["year"] == 1999 and m["vcodec"] == "hevc"
    b = item_by_title(admin, "Big Buck")
    assert b["year"] == 2008


def test_episode_parsing(admin):
    ep = item_by_title(admin, "Turbo Time")
    assert ep["show"] == "Garage Builds" and ep["season"] == 1 and ep["episode"] == 2
    groups = admin.get("/api/groups", params={"prefix": "show:"}).json()
    assert groups[0]["name"] == "Garage Builds" and groups[0]["count"] == 2


def test_music_tags_and_album_group(admin):
    s = item_by_title(admin, "Launch Control")
    assert s["artist"] == "The Pistons" and s["album"] == "Redline"
    albums = admin.get("/api/groups", params={"prefix": "album:"}).json()
    assert albums and albums[0]["count"] == 2


def test_photo_exif(admin):
    p = admin.get("/api/items", params={"kind": "photo", "sort": "oldest"}).json()[0]
    detail = admin.get(f"/api/items/{p['id']}").json()
    assert abs(detail["lat"] - 37.7749) < 0.001 and abs(detail["lon"] + 122.4194) < 0.001
    assert detail["date"].startswith("2024-07-04")
    assert detail["meta"]["model"] == "Galaxy S25"
    shot = item_by_title(admin, "Screenshot 20250101")
    assert "Screenshot" in shot["tags"]


def test_video_location_and_date(admin):
    v = item_by_title(admin, "launch-day")
    assert v["date"].startswith("2024-07-04") and v["lat"] is not None


def test_red_clip_catalog(admin):
    clips = admin.get("/api/items", params={"kind": "red_clip"}).json()
    assert len(clips) == 2
    c = admin.get(f"/api/items/{clips[0]['id']}").json()
    assert c["meta"]["spans"] == 2 and c["meta"]["camera"] == "A" and c["meta"]["reel"] == 1
    assert c["meta"]["iso"] == "800" and c["meta"]["proxy"]
    assert c["has_thumb"]  # from the proxy
    assert c["group"].startswith("shoot:")
    assert "R3D" in c["tags"]


def test_engineering_metadata(admin):
    g = admin.get("/api/items", params={"kind": "gcode"}).json()[0]
    d = admin.get(f"/api/items/{g['id']}").json()["meta"]
    assert d["filament_g"] == 3.71 and d["filament_type"] == "PETG" and d["printer"] == "MK4S"
    assert d["print_time"].startswith("1h 2m")
    assert g["has_thumb"]
    step = admin.get("/api/items", params={"kind": "cad", "query": "flange"}).json()[0]
    sm = admin.get(f"/api/items/{step['id']}").json()["meta"]
    assert sm["schema"] == "AP242" and sm["originating_system"] == "Autodesk Fusion 360" and sm["revision"] == "2"
    stl = item_by_title(admin, "flange v3")
    assert stl["has_thumb"] and abs(stl["size_x"] - 60) < 0.5
    pcb = admin.get("/api/items", params={"kind": "pcb"}).json()[0]
    assert admin.get(f"/api/items/{pcb['id']}").json()["meta"]["kicad_title"] == "ECU breakout"


def test_part_family_groups_revisions_and_exports(admin):
    fams = admin.get("/api/groups", params={"prefix": "eng:"}).json()
    flange = next(f for f in fams if f["group_key"] == "eng:Turbo Manifold/flange")
    assert flange["count"] == 4   # step v2 + stl v2 + stl v3 + the sliced g-code


def test_inbox_filing(admin, libs):
    scanner.file_all_inboxes()
    wait_jobs()
    moved = libs["engineering"] / "Go Kart" / "3D Print" / "Models" / "axle_spacer.stl"
    assert moved.exists()
    assert not (libs["engineering"] / "_Inbox" / "Go Kart").exists()
    assert item_by_title(admin, "axle spacer")["path"] == "Go Kart/3D Print/Models/axle_spacer.stl"


def test_search(admin):
    hits = admin.get("/api/items", params={"query": "matrix"}).json()
    assert hits and hits[0]["title"] == "The Matrix"
    assert admin.get("/api/items", params={"query": "petg"}).json()  # tags/metadata are searchable
    sug = admin.get("/api/search/suggest", params={"q": "gar"}).json()
    assert any("Garage" in (i["path"]) for i in sug["items"])


def test_thumbnails_served(admin):
    for i in admin.get("/api/items", params={"limit": 100}).json():
        if i["kind"] in ("video", "photo", "model3d", "gcode", "red_clip"):
            assert i["has_thumb"], i["title"]
            r = admin.get(f"/api/items/{i['id']}/thumb")
            assert r.status_code == 200 and r.headers["content-type"] == "image/webp"


def test_home_feed(admin):
    rows = {r["key"]: r for r in admin.get("/api/home").json()["rows"]}
    assert "photos" in rows and "shows" in rows and "engineering" in rows and "shoots" in rows


def test_rescan_detects_removal(admin, libs):
    extra = libs["photos"] / "Camera" / "temp.jpg"
    import media_fixtures

    media_fixtures.photo(extra, taken="2023:01:01 00:00:00")
    lib = next(l for l in admin.get("/api/libraries").json() if l["kind"] == "photos")
    admin.post(f"/api/libraries/{lib['id']}/scan")
    wait_jobs()
    assert item_by_title(admin, "temp")
    extra.unlink()
    admin.post(f"/api/libraries/{lib['id']}/scan")
    wait_jobs()
    assert not [i for i in admin.get("/api/items", params={"limit": 500}).json() if i["title"] == "temp"]


def test_mesh_endpoint(admin):
    stl = item_by_title(admin, "flange v3")
    r = admin.get(f"/api/items/{stl['id']}/mesh.stl")
    assert r.status_code == 200 and len(r.content) > 84
