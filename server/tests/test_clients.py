"""The Android app a server release carries, offered to phones for in-app updates."""

import hashlib
import json

from paddock.config import settings


def test_android_update_is_offered_from_the_bundled_release(admin, app, tmp_path, monkeypatch):
    from conftest import client

    monkeypatch.setattr(settings, "android_dir", tmp_path)
    assert admin.get("/api/app/android").json() == {"available": False}
    assert admin.get("/api/app/android/paddock.apk").status_code == 404

    apk = b"PK\x03\x04 not really an apk" * 100
    (tmp_path / "paddock.apk").write_bytes(apk)
    (tmp_path / "release.json").write_text(json.dumps({"version_name": "0.3.0", "version_code": 3000,
                                                        "sha256": hashlib.sha256(apk).hexdigest(), "size": len(apk)}))
    info = admin.get("/api/app/android").json()
    assert info["available"] and info["version_code"] == 3000 and info["size"] == len(apk)
    assert admin.get("/download/android").content == apk                 # first install, before pairing
    r = admin.get(info["url"])
    assert r.status_code == 200 and r.content == apk
    assert r.headers["content-type"] == "application/vnd.android.package-archive"
    assert "Paddock-0.3.0.apk" in r.headers["content-disposition"]

    assert client(app).get("/api/app/android").status_code == 401        # signed-in users and paired devices only

    (tmp_path / "paddock.apk").write_bytes(apk[:-10])                    # a half-copied file is never offered
    assert admin.get("/api/app/android").json() == {"available": False}
