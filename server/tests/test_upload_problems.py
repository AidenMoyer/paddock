"""The ways a phone's photo could fail to arrive, or seem to: odd names, HEIC in all its spellings, old attempts, the trash."""

import hashlib
import io
import time

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
