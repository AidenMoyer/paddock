"""The ways a phone's photo could fail to arrive, or seem to: odd names, HEIC in all its spellings, old attempts, the trash."""

import asyncio
import hashlib
import io

import httpx
import pytest
from test_access import login


def send(c, data: bytes, name: str, **extra):
    sha = hashlib.sha256(data).hexdigest()
    r = c.post("/api/uploads", json={"filename": name, "size": len(data), "sha256": sha, "taken_at": 1767268800, **extra})
    assert r.status_code == 200, r.text
    start = r.json()
    if start["status"] == "duplicate":
        return start
    assert c.put(f"/api/uploads/{start['upload_id']}", params={"offset": start["offset"]}, content=data[start["offset"]:]).status_code == 200
    done = c.post(f"/api/uploads/{start['upload_id']}/complete")
    assert done.status_code == 200, done.text
    return done.json()


@pytest.fixture(scope="module")
def heic():
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    from PIL import Image

    out = io.BytesIO()
    try:
        Image.effect_mandelbrot((320, 240), (-2, -1.5, 1, 1.5), 20).convert("RGB").save(out, format="HEIF", quality=60)
    except Exception as e:  # a pillow-heif built without an encoder can still read
        pytest.skip(f"Can't encode HEIC here: {e}")
    return out.getvalue()


@pytest.mark.parametrize("n,name", list(enumerate(["IMG_1.heic", "IMG_2.HEIC", "IMG_3.heif", "IMG_4.hif"])))
def test_heic_arrives_in_every_spelling(admin, heic, n, name):
    done = send(admin, heic + bytes([n]), name)
    assert done["status"] == "done"
    assert admin.get(f"/api/items/{done['item_id']}").json()["kind"] == "photo"
    preview = admin.get(f"/api/items/{done['item_id']}/preview")
    assert preview.status_code == 200 and preview.headers["content-type"].startswith("image/")


@pytest.mark.parametrize("n,name", list(enumerate(["1000012345", "1000012346.null", "1000012347.heic-sequence"])))
def test_a_photo_with_a_useless_name_goes_by_its_type(admin, heic, n, name):
    done = send(admin, heic + bytes([100 + n]), name, mime="image/heic")
    assert done["path"].endswith(".heic")
    assert admin.get(f"/api/items/{done['item_id']}").json()["kind"] == "photo"


def test_a_known_extension_is_left_alone_whatever_the_type_says(admin):
    done = send(admin, b"\xff\xd8\xff" + b"keep-my-name" * 20, "IMG_9.jpg", mime="image/heic")
    assert done["path"].endswith("IMG_9.jpg")


@pytest.mark.parametrize("name,mime,says", [
    ("1000012348", None, "no extension"),
    ("1000012349", "application/octet-stream", "no extension"),
    ("notes.xyz", None, "doesn't keep .xyz files"),
    ("notes.xyz", "application/x-whatever", "doesn't keep .xyz files"),
])
def test_refusals_say_why_in_words(admin, name, mime, says):
    r = admin.post("/api/uploads", json={"filename": name, "size": 10, "sha256": "a" * 64, "mime": mime})
    assert r.status_code == 415 and says in r.json()["detail"]


def test_an_old_attempt_with_the_wrong_size_doesnt_block_the_file_forever(admin):
    data = b"\xff\xd8\xff" + b"measured-wrong" * 30
    sha = hashlib.sha256(data).hexdigest()
    first = admin.post("/api/uploads", json={"filename": "IMG_10.jpg", "size": 0, "sha256": sha}).json()
    assert first["status"] == "new"
    second = admin.post("/api/uploads", json={"filename": "IMG_10.jpg", "size": len(data), "sha256": sha}).json()
    assert second["status"] == "new" and second["upload_id"] != first["upload_id"]
    assert admin.put(f"/api/uploads/{second['upload_id']}", params={"offset": 0}, content=data).status_code == 200
    assert admin.post(f"/api/uploads/{second['upload_id']}/complete").json()["status"] == "done"
    # the same size does still resume
    third = b"\xff\xd8\xff" + b"resume-me" * 30
    sha3 = hashlib.sha256(third).hexdigest()
    a = admin.post("/api/uploads", json={"filename": "IMG_11.jpg", "size": len(third), "sha256": sha3}).json()
    b = admin.post("/api/uploads", json={"filename": "IMG_11.jpg", "size": len(third), "sha256": sha3}).json()
    assert b["status"] == "resume" and b["upload_id"] == a["upload_id"]


def test_uploading_what_is_in_the_trash_brings_it_back(admin):
    data = b"\xff\xd8\xff" + b"deleted-then-wanted" * 30
    sha = hashlib.sha256(data).hexdigest()
    item = send(admin, data, "IMG_12.jpg")["item_id"]
    assert admin.post("/api/items/trash", json={"ids": [item]}).status_code == 200
    assert item in [i["id"] for i in admin.get("/api/trash").json()]
    # automatic backup checks first, and leaves what was deliberately deleted alone
    assert admin.post("/api/uploads/check", json={"sha256": [sha]}).json()["have"] == [sha]
    # picking the file and uploading it means they want it
    again = send(admin, data, "IMG_12.jpg")
    assert again["status"] == "duplicate" and again["restored"] is True and again["item_id"] == item
    assert item not in [i["id"] for i in admin.get("/api/trash").json()]
    assert send(admin, data, "IMG_12.jpg")["restored"] is False


def test_someone_elses_private_copy_doesnt_stop_your_upload(app, admin):
    libs = {l["kind"]: l for l in admin.get("/api/libraries").json()}
    assert "photos" in libs and "home_videos" in libs
    private, shared = libs["photos"], libs["home_videos"]
    data = b"\xff\xd8\xff" + b"both-of-us-have-this" * 30
    sha = hashlib.sha256(data).hexdigest()
    theirs = send(admin, data, "IMG_13.jpg", library_id=private["id"])["item_id"]
    me = login(app, admin, "newcomer", "192.168.1.103")
    community = admin.post("/api/communities", json={"name": "Uploaders", "members": [me.user_id]}).json()["id"]
    try:
        admin.put(f"/api/libraries/{private['id']}/access", json={"visibility": "communities", "communities": []})
        admin.put(f"/api/libraries/{shared['id']}/access", json={"visibility": "communities", "communities": [{"id": community, "access": "write"}]})
        assert me.post("/api/uploads/check", json={"sha256": [sha]}).json()["have"] == []
        mine = send(me, data, "IMG_13.jpg", library_id=shared["id"])
        assert mine["status"] == "done" and mine["item_id"] != theirs
        assert me.get(f"/api/items/{mine['item_id']}").status_code == 200
        assert me.post("/api/uploads/check", json={"sha256": [sha]}).json()["have"] == [sha]
    finally:
        for lib in (private, shared):
            admin.put(f"/api/libraries/{lib['id']}/access", json={"visibility": "everyone", "communities": []})
        admin.delete(f"/api/communities/{community}")
        me.__exit__(None, None, None)


def overlapping_puts(app, admin, uid, offset, chunks):
    """Two senders hitting the same upload at the same moment, as two backup jobs on a phone did."""
    async def run():
        transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 50000))
        cookie = "; ".join(f"{x.name}={x.value}" for x in admin.cookies.jar)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     headers={**admin.headers, "cookie": cookie}) as c:
            return await asyncio.gather(*(c.put(f"/api/uploads/{uid}", params={"offset": offset}, content=x) for x in chunks))
    return asyncio.run(run())


def test_two_senders_at_once_cant_corrupt_an_upload(app, admin):
    data = b"\xff\xd8\xff" + b"sent-twice-at-once" * 5000
    sha = hashlib.sha256(data).hexdigest()
    uid = admin.post("/api/uploads", json={"filename": "IMG_14.jpg", "size": len(data), "sha256": sha}).json()["upload_id"]
    replies = overlapping_puts(app, admin, uid, 0, [data, data])
    assert sorted(r.status_code for r in replies)[0] == 200
    # however they interleaved, what's on the server is exactly the file, and it finishes
    assert admin.get(f"/api/uploads/{uid}").json()["offset"] == len(data)
    done = admin.post(f"/api/uploads/{uid}/complete")
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "done"


def test_the_sender_that_lost_is_told_where_things_stand(app, admin):
    data = b"\xff\xd8\xff" + b"lost-the-race" * 5000
    sha = hashlib.sha256(data).hexdigest()
    uid = admin.post("/api/uploads", json={"filename": "IMG_15.jpg", "size": len(data), "sha256": sha}).json()["upload_id"]
    replies = overlapping_puts(app, admin, uid, 0, [data, data])
    assert sorted(r.status_code for r in replies) == [200, 409]
    assert int(next(r for r in replies if r.status_code == 409).headers["upload-offset"]) in (0, len(data))


def test_a_sender_overtaken_halfway_through_a_chunk_leaves_no_trace(app, admin):
    """A phone that walks out of Wi-Fi mid-chunk, comes back and starts over while the server still thinks it's connected."""
    data = b"\xff\xd8\xff" + b"gone-and-back-again" * 5000
    sha = hashlib.sha256(data).hexdigest()
    uid = admin.post("/api/uploads", json={"filename": "IMG_18.jpg", "size": len(data), "sha256": sha}).json()["upload_id"]

    async def run():
        halfway, second_done = asyncio.Event(), asyncio.Event()

        async def old_sender():
            yield data[:len(data) // 2]
            halfway.set()
            await second_done.wait()
            yield data[len(data) // 2:]

        transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 50000))
        cookie = "; ".join(f"{x.name}={x.value}" for x in admin.cookies.jar)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     headers={**admin.headers, "cookie": cookie}) as c:
            url = f"/api/uploads/{uid}"
            first = asyncio.create_task(c.put(url, params={"offset": 0}, content=old_sender()))
            await halfway.wait()
            await asyncio.sleep(0.05)      # let the old sender write what it has
            second = await c.put(url, params={"offset": 0}, content=data)
            second_done.set()
            return await first, second

    first, second = asyncio.run(run())
    assert second.status_code == 200, second.text
    assert first.status_code == 409, first.text
    done = admin.post(f"/api/uploads/{uid}/complete")
    assert done.status_code == 200, done.text       # the checksum proves nothing of the old sender's survived


def test_an_upload_already_corrupted_this_way_repairs_itself(app, admin):
    from paddock import db
    from paddock.config import settings

    data = b"\xff\xd8\xff" + b"stuck-forever" * 500
    sha = hashlib.sha256(data).hexdigest()
    uid = admin.post("/api/uploads", json={"filename": "IMG_16.jpg", "size": len(data), "sha256": sha}).json()["upload_id"]
    # what an older server left behind: every byte counted as received, but too many bytes in the file
    (settings.uploads_tmp_dir / f"{uid}.part").write_bytes(data + data[:999])
    db.x("UPDATE uploads SET received=? WHERE id=?", (len(data), uid))

    stuck = admin.post(f"/api/uploads/{uid}/complete")
    assert stuck.status_code == 409 and "incomplete" in stuck.json()["detail"]
    # asking to start again no longer resumes the bad copy
    again = admin.post("/api/uploads", json={"filename": "IMG_16.jpg", "size": len(data), "sha256": sha}).json()
    assert again["status"] == "new" and again["upload_id"] != uid
    assert not (settings.uploads_tmp_dir / f"{uid}.part").exists()
    assert send(admin, data, "IMG_16.jpg")["status"] == "done"


def test_an_upload_that_really_is_unfinished_can_still_resume(app, admin):
    data = b"\xff\xd8\xff" + b"only-half-sent" * 500
    sha = hashlib.sha256(data).hexdigest()
    uid = admin.post("/api/uploads", json={"filename": "IMG_17.jpg", "size": len(data), "sha256": sha}).json()["upload_id"]
    half = len(data) // 2
    admin.put(f"/api/uploads/{uid}", params={"offset": 0}, content=data[:half])
    assert admin.post(f"/api/uploads/{uid}/complete").status_code == 409
    again = admin.post("/api/uploads", json={"filename": "IMG_17.jpg", "size": len(data), "sha256": sha}).json()
    assert again["status"] == "resume" and again["upload_id"] == uid and again["offset"] == half


def blanked_location(path):
    """A photo the way Android's photo picker hands it over: the location written as 0/0."""
    from PIL import Image
    from PIL.TiffImagePlugin import IFDRational

    exif = Image.Exif()
    exif[0x0132] = "2026:11:02 12:00:00"
    gps = exif.get_ifd(0x8825)
    zero = (IFDRational(0, 0), IFDRational(0, 0), IFDRational(0, 0))
    gps[1], gps[2], gps[3], gps[4] = "N", zero, "W", zero
    Image.new("RGB", (64, 48), (200, 80, 40)).save(path, "JPEG", exif=exif)
    return path


def test_a_photo_whose_location_was_blanked_can_be_opened(admin, tmp_path):
    from paddock import probe

    photo = blanked_location(tmp_path / "picked.jpg")
    info = probe.photo_info(photo)
    assert "lat" not in info and "lon" not in info
    done = send(admin, photo.read_bytes(), "1000000099.jpg")
    assert done["status"] == "done"
    r = admin.get(f"/api/items/{done['item_id']}")
    assert r.status_code == 200, r.text
    assert r.json().get("lat") is None


def test_numbers_that_older_scans_stored_dont_break_a_page(admin):
    import json

    from paddock import db

    data = b"\xff\xd8\xff" + b"scanned-long-ago" * 40
    item = send(admin, data, "OLD_1.jpg")["item_id"]
    db.x("UPDATE items SET meta=? WHERE id=?", ('{"lat": NaN, "lon": NaN, "fps": Infinity, "keep": 7}', item))
    r = admin.get(f"/api/items/{item}")
    assert r.status_code == 200, r.text
    assert json.loads(r.text, parse_constant=lambda c: pytest.fail(f"{c} reached a client"))["meta"]["keep"] == 7


def test_a_file_added_to_a_project_survives_a_rescan(admin):
    from paddock import db, jobs, scanner

    pid = admin.post("/api/projects", json={"name": "Keeps Files", "goal": "x"}).json()["id"]
    photos = next(l for l in admin.get("/api/libraries").json() if l["kind"] == "photos")
    admin.patch(f"/api/projects/{pid}", json={"upload_library_id": photos["id"]})
    db.x("UPDATE projects SET upload_library_id=? WHERE id=?", (photos["id"], pid))
    done = send(admin, b"notes for the project, nothing a photo library would collect", "plan.txt", project_id=pid)
    assert done["status"] == "done"
    assert [i["id"] for i in admin.get(f"/api/projects/{pid}").json()["items"]] == [done["item_id"]]
    scanner.scan_library(photos["id"])
    assert [i["id"] for i in admin.get(f"/api/projects/{pid}").json()["items"]] == [done["item_id"]]
    assert admin.get(f"/api/items/{done['item_id']}").status_code == 200


def test_an_upload_left_to_choose_goes_where_that_kind_of_file_belongs(admin):
    def library_of(data, name):
        item = send(admin, data, name)["item_id"]
        return admin.get(f"/api/items/{item}").json()["library_kind"]

    assert library_of(b"ID3" + b"a-song-not-a-photo" * 20, "song.mp3") == "music"
    assert library_of(b"solid x\nendsolid x\n" * 3, "part.stl") == "engineering"
    assert library_of(b"\xff\xd8\xff" + b"still-a-photo" * 20, "snap.jpg") in ("photos", "home_videos", "mixed")


def test_nowhere_for_that_kind_of_file_says_so(app, admin):
    from conftest import PASSWORD, client

    libs = admin.get("/api/libraries").json()
    r = admin.post("/api/users", json={"username": "nolibs", "password": PASSWORD, "is_admin": False})
    c = client(app, ip="192.168.1.104", headers={"x-paddock": "1"})
    c.__enter__()
    c.post("/api/auth/login", json={"username": "nolibs", "password": PASSWORD})
    try:
        for l in libs:
            admin.put(f"/api/libraries/{l['id']}/access", json={"visibility": "communities", "communities": []})
        reply = c.post("/api/uploads", json={"filename": "song.mp3", "size": 5, "sha256": "b" * 64})
        assert reply.status_code == 409 and "music files" in reply.json()["detail"]
        reply = c.post("/api/uploads", json={"filename": "snap.heic", "size": 5, "sha256": "c" * 64})
        assert reply.status_code == 409 and "Photos library" in reply.json()["detail"]
    finally:
        for l in libs:
            admin.put(f"/api/libraries/{l['id']}/access", json={"visibility": "everyone", "communities": []})
        c.__exit__(None, None, None)
        admin.delete(f"/api/users/{r.json()['id']}")
