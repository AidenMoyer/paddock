"""What plays next. Random on purpose, so each rule is checked over many runs."""

import itertools
import json
import random
import time
from collections import Counter

import pytest

from paddock import db, probe, recommend, scanner

RUNS = 40
_made = itertools.count()


class Library:
    def __init__(self, kind: str):
        self.kind = f"{kind}{next(_made)}"
        self.id = db.x("INSERT INTO libraries(name, kind, path, options, created_at) VALUES(?,?,?,?,?)",
                       (f"Rec {self.kind}", kind, f"/rec/{self.kind}", "{}", time.time())).lastrowid

    def add(self, rel: str, kind="video", title=None, group=None, sort=None, meta=None, tags=(), duration=100.0, taken=None,
            lat=None, lon=None) -> dict:
        title = title or rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        item = db.x("INSERT INTO items(library_id, path, rel_path, kind, ext, size, mtime, title, taken_at, duration, lat, lon, group_key, "
                    "sort_key, meta, added_at, thumb_state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'ok')",
                    (self.id, f"/rec/{self.kind}/{rel}", rel, kind, ".x", 1, 1.0, title, taken or 1.7e9, duration, lat, lon, group,
                     sort, json.dumps(meta or {}), time.time())).lastrowid
        for t in tags:
            db.x("INSERT INTO tags(item_id, tag, source) VALUES(?,?, 'auto')", (item, t))
        return db.q1("SELECT * FROM items WHERE id=?", (item,))


@pytest.fixture
def library(app, admin):
    libs = []

    def make(kind):
        lib = Library(kind)
        libs.append(lib)
        return lib

    yield make
    for lib in libs:
        db.x("DELETE FROM libraries WHERE id=?", (lib.id,))


@pytest.fixture
def me(admin):
    return db.q1("SELECT id FROM users WHERE is_admin=1 ORDER BY id")["id"]


def song(lib, artist, album, title, genre=None, year=2020):
    return lib.add(f"{artist}/{album}/{title}.mp3", kind="audio", title=title, group=f"album:{artist}:{album}".lower(),
                   sort=title, meta={"artist": artist, "album": album, "date": str(year)}, tags=[genre] if genre else [])


def film(lib, title, year, folder=None, tags=(), meta=None, duration=6000.0):
    name = f"{title} ({year})"
    rel = f"{folder}/{name}/{name}.mkv" if folder else f"{name}/{name}.mkv"
    return lib.add(rel, title=title, group=f"movie:{title}:{year}".lower(), sort=title.lower(), meta={"year": year, **(meta or {})},
                   tags=tags, duration=duration)


def episode(lib, show, s, e):
    return lib.add(f"{show}/Season {s}/{show}.S{s:02d}E{e:02d}.mkv", title=f"Episode {e}", group=f"show:{show}".lower(),
                   sort=f"S{s:02d}E{e:03d}", duration=1500.0)


def picks(item, me, limit=8, up_next=False, runs=RUNS):
    fn = recommend.up_next if up_next else lambda i, u, a, n, rng: recommend.similar(i, u, a, {i["id"]}, n, rng)
    return [[c["title"] for c in fn(item, me, None, limit, random.Random(seed))] for seed in range(runs)]


def firsts(lists) -> Counter:
    return Counter(x[0] for x in lists if x)


def test_a_sequel_comes_before_unrelated_films_and_shows(library, me):
    movies, shows = library("movies"), library("shows")
    second = film(movies, "Iron Forge 2", 2010, "Iron Forge Collection")
    film(movies, "Iron Forge 3", 2013, "Iron Forge Collection")
    for t, y in [("Midnight Drive", 2011), ("Deep Blue Harbor", 1999), ("Summer Road", 2015), ("Quiet Machines", 2021)]:
        film(movies, t, y)
    for s in (1, 2):
        for e in range(1, 5):
            episode(shows, "Night Mechanics", s, e)
    assert firsts(picks(second, me))["Iron Forge 3"] >= RUNS * 0.9


def test_another_show_is_only_entered_at_the_next_episode_to_watch(library, me):
    movies, shows = library("movies"), library("shows")
    a_film = film(movies, "Solo Picture", 2005)
    eps = {(s, e): episode(shows, "Long Runner", s, e) for s in (1, 2) for e in range(1, 7)}
    for i, l in enumerate(picks(a_film, me, limit=30)):
        assert l.count("Episode 1") + sum(l.count(f"Episode {n}") for n in range(2, 7)) == 1      # one choice, not twelve
        assert "Episode 1" in l                                                                   # and it's the first
    # once someone's started, it's where they are up to
    db.x("INSERT INTO progress(user_id, item_id, position, duration, completed, updated_at) VALUES(?,?,?,?,1,?)",
         (me, eps[(1, 1)]["id"], 1500, 1500, time.time() - 30 * 86400))
    db.x("INSERT INTO progress(user_id, item_id, position, duration, completed, updated_at) VALUES(?,?,?,?,1,?)",
         (me, eps[(1, 2)]["id"], 1500, 1500, time.time() - 30 * 86400))
    for l in picks(a_film, me, limit=30, runs=10):
        assert "Episode 3" in l and "Episode 1" not in l and "Episode 2" not in l


def test_finishing_a_show_moves_on_instead_of_going_round_again(library, me):
    shows = library("shows")
    eps = [episode(shows, "Short Run", 1, e) for e in range(1, 5)]
    other = episode(shows, "Other Show", 1, 1)
    for seed in range(10):
        ids = [c["id"] for c in recommend.up_next(eps[-1], me, None, 30, random.Random(seed))]
        assert other["id"] in ids and not {e["id"] for e in eps} & set(ids)     # the other show, and none of this one


def test_an_album_plays_on_in_order_then_moves_on_without_repeating_it(library, me):
    music = library("music")
    tracks = [song(music, "Solo Act", "Only Album", f"T{n}", "Pop") for n in range(1, 5)]
    song(music, "Other Act", "Theirs", "Elsewhere", "Pop")
    for l in picks(tracks[1], me, limit=10, up_next=True, runs=5):
        assert l[:2] == ["T3", "T4"] and "T1" not in l and "Elsewhere" in l


def test_genres_match_whatever_way_they_were_written(library, me):
    music = library("music")
    now = song(music, "Data Drift", "Static", "Packet Loss", "electronic")
    song(music, "Neon Circuit", "Afterglow", "Standby", "Electronic; Ambient")
    song(music, "Pulse", "Grid", "Square Wave", "ELECTRONIC")
    for n in range(6):
        song(music, f"Rock Band {n}", "Loud", f"Riff {n}", "Rock")
    top_two = Counter(t for l in picks(now, me, limit=2) for t in l)
    assert top_two["Standby"] + top_two["Square Wave"] >= RUNS * 2 * 0.8


def test_songs_by_the_same_artist_come_first_but_others_follow(library, me):
    music = library("music")
    now = song(music, "The Pistons", "Redline", "Launch", "Rock")
    for n in range(8):
        song(music, "The Pistons", "Pit Stop", f"Pistons {n}", "Rock")
    for n in range(4):
        song(music, f"Rock Band {n}", "Loud", f"Riff {n}", "Rock")
    for n in range(6):
        song(music, f"Jazz Trio {n}", "Quiet", f"Blues {n}", "Jazz", year=1960)
    lists = picks(now, me, limit=8)
    assert sum(1 for l in lists if l[0].startswith("Pistons")) >= RUNS * 0.8
    for l in lists:
        assert sum(t.startswith("Pistons") for t in l) < 8              # never one artist all the way down
    position = lambda l, prefix: min((i for i, t in enumerate(l) if t.startswith(prefix)), default=99)
    assert sum(position(l, "Riff") < position(l, "Blues") for l in lists) >= RUNS * 0.85


def test_a_featured_artist_counts_as_the_same_artist(library, me):
    music = library("music")
    now = music.add("A/1/x.mp3", kind="audio", title="Now", group="album:a:1", sort="1", meta={"artist": "Lena Vance feat. Otto Bell", "album": "1"})
    music.add("B/2/y.mp3", kind="audio", title="Hers", group="album:b:2", sort="1", meta={"artist": "Lena Vance", "album": "2"})
    for n in range(10):
        music.add(f"C{n}/z.mp3", kind="audio", title=f"Filler {n}", group=f"album:c{n}:z", sort="1", meta={"artist": f"Filler {n}", "album": "z"})
    assert firsts(picks(now, me))["Hers"] >= RUNS * 0.65


def test_technical_tags_dont_make_things_similar(library, me):
    movies = library("movies")
    now = film(movies, "Tag Source", 2000, tags=["1080p", "HDR"])
    film(movies, "Same Quality", 2000, tags=["1080p", "HDR"])
    film(movies, "Same Genre", 2000, tags=["SD", "Thriller"])
    # a genre shared with the current film matters; identical picture quality alone doesn't
    now2 = film(movies, "Genre Source", 2000, tags=["1080p", "Thriller"])
    assert firsts(picks(now2, me))["Same Genre"] >= RUNS * 0.7
    facts = recommend._facts(now, ["1080p", "HDR", "4K", "720p", "Comedy"])
    assert facts["labels"] == {"comedy"}


def test_what_was_just_played_sinks_and_comes_back_later(library, me):
    movies = library("movies")
    now = film(movies, "Anchor", 2001, "Set")
    just = film(movies, "Just Seen", 2001, "Set")
    film(movies, "Not Yet", 2001, "Set")
    db.x("INSERT INTO progress(user_id, item_id, position, duration, completed, updated_at) VALUES(?,?,?,?,1,?)",
         (me, just["id"], 6000, 6000, time.time() - 600))
    assert firsts(picks(now, me))["Not Yet"] >= RUNS * 0.9
    db.x("UPDATE progress SET updated_at=? WHERE user_id=? AND item_id=?", (time.time() - 40 * 86400, me, just["id"]))
    assert firsts(picks(now, me))["Just Seen"] >= 1      # weeks later it's a possibility again


def test_things_people_watch_together_rank_higher(library, me):
    movies = library("movies")
    now = film(movies, "Double Feature One", 1980)
    partner = film(movies, "Unrelated Partner", 2020)
    for n in range(8):
        film(movies, f"Stranger {n}", 2020)
    before = firsts(picks(now, me))["Unrelated Partner"]
    t = time.time() - 30 * 86400
    db.x("INSERT INTO progress(user_id, item_id, position, duration, completed, updated_at) VALUES(?,?,?,?,1,?)", (me, now["id"], 1, 1, t))
    db.x("INSERT INTO progress(user_id, item_id, position, duration, completed, updated_at) VALUES(?,?,?,?,0,?)", (me, partner["id"], 1, 6000, t + 3600))
    assert firsts(picks(now, me))["Unrelated Partner"] > before


def test_memories_stay_with_the_same_trip(library, me):
    home = library("home_videos")
    t = 1.72e9
    now = home.add("2024/beach-1.mp4", group="day:2024-07-04", taken=t, lat=34.01, lon=-118.49)
    home.add("2024/beach-2.mp4", group="day:2024-07-05", taken=t + 86400, lat=34.011, lon=-118.491)
    home.add("2021/snow.mp4", group="day:2021-12-28", taken=t - 8e7, lat=39.19, lon=-106.8)
    home.add("2022/cake.mp4", group="day:2022-09-20", taken=t - 5e7, lat=40.7, lon=-74.0)
    assert firsts(picks(now, me))["beach-2"] >= RUNS * 0.9


def test_the_list_is_different_each_time(library, me):
    movies = library("movies")
    now = film(movies, "Variety Source", 2010)
    for n in range(12):
        film(movies, f"Option {n}", 2010)
    assert len({tuple(l) for l in picks(now, me, limit=6, runs=10)}) >= 8


def test_a_big_library_still_finds_the_same_artist(library, me):
    music = library("music")
    now = song(music, "Needle", "Hay", "One", "Niche")
    song(music, "Needle", "Hay", "Two", "Niche")
    with db.tx():
        for n in range(2500):
            song(music, f"Band {n}", "Pile", f"Song {n}")
    hits = sum("Two" in l[:3] for l in picks(now, me, runs=8))
    assert hits >= 7


def test_up_next_never_crosses_what_someone_may_see(library, me):
    public, private = library("music"), library("music")
    now = song(public, "Open", "A", "Visible", "Pop")
    song(public, "Open", "A", "Also Visible", "Pop")
    song(private, "Open", "B", "Secret", "Pop")
    lists = [[c["title"] for c in recommend.similar(now, me, {public.id}, {now["id"]}, 10, random.Random(s))] for s in range(10)]
    assert all("Secret" not in l and "Also Visible" in l for l in lists)


def test_special_characters_in_names_cant_break_the_search(library, me):
    music = library("music")
    now = song(music, "100%_Pure\\Noise", "O'Brien's", "Weird", "R&B/Soul")
    song(music, "100%_Pure\\Noise", "O'Brien's", "Same", "R&B/Soul")
    assert picks(now, me, runs=3)[0][0] == "Same"


def test_nfo_files_give_genres_people_and_series(tmp_path):
    video = tmp_path / "Iron Forge (2008)" / "Iron Forge (2008).mkv"
    video.parent.mkdir()
    video.write_bytes(b"x")
    (video.parent / "movie.nfo").write_text("""<?xml version="1.0"?><movie>
      <title>Iron Forge</title><genre>Action</genre><genre>Sci-Fi / Fantasy</genre><director>Ana Reyes</director>
      <actor><name>Sam Holt</name><role>Smith</role></actor><actor><name>Jo &amp; Bo</name></actor>
      <set><name>Iron Forge Collection</name></set></movie>""", encoding="utf-8")
    about = probe.nfo(video)
    assert about["genres"] == ["Action", "Sci-Fi", "Fantasy"]
    assert about["directors"] == ["Ana Reyes"]
    assert about["people"] == ["Sam Holt", "Jo & Bo"]
    assert about["set"] == "Iron Forge Collection"
    # sloppy files are the norm: never a reason to fail a scan
    (video.parent / "movie.nfo").write_text("https://www.imdb.com/title/tt0000001/ <genre>Drama", encoding="utf-8")
    assert probe.nfo(video) == {}
    assert probe.nfo(tmp_path / "nothing-here.mkv") == {}
    # an old style set with the name directly inside
    (video.parent / "movie.nfo").write_text("<movie><set>Old Style Set</set></movie>", encoding="utf-8")
    assert probe.nfo(video)["set"] == "Old Style Set"


def test_a_series_from_an_nfo_outranks_everything_else(library, me):
    movies = library("movies")
    now = film(movies, "Dawn", 2001, meta={"set": "The Saga"})
    film(movies, "Dusk", 2020, meta={"set": "The Saga"})
    for n in range(8):
        film(movies, f"Lookalike {n}", 2001)
    assert firsts(picks(now, me))["Dusk"] >= RUNS * 0.8


def test_scanning_a_film_with_an_nfo_tags_it(app, admin, tmp_path):
    root = tmp_path / "movies"
    video = root / "Tagged (2015)" / "Tagged (2015).mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"\x00" * 16)
    (video.parent / "movie.nfo").write_text("<movie><genre>Western</genre><director>Del Rio</director></movie>", encoding="utf-8")
    lib = {"id": db.x("INSERT INTO libraries(name, kind, path, options, created_at) VALUES('T', 'movies', ?, '{}', ?)", (str(root), time.time())).lastrowid,
           "kind": "movies"}
    try:
        item = scanner.upsert_item(lib, root, video, "video", video.stat(), None)
        assert "Western" in [r["tag"] for r in db.q("SELECT tag FROM tags WHERE item_id=?", (item,))]
        assert json.loads(db.q1("SELECT meta FROM items WHERE id=?", (item,))["meta"])["directors"] == ["Del Rio"]
    finally:
        db.x("DELETE FROM libraries WHERE id=?", (lib["id"],))


def test_tags_with_several_genres_split_when_scanned(app, tmp_path):
    from media_fixtures import _ff

    root = tmp_path / "music"
    track = root / "01.mp3"
    root.mkdir()
    _ff("-f", "lavfi", "-i", "sine=frequency=330:duration=1", "-metadata", "genre=Electronic; Ambient", "-metadata", "artist=A", str(track))
    lib = {"id": db.x("INSERT INTO libraries(name, kind, path, options, created_at) VALUES('T2', 'music', ?, '{}', ?)", (str(root), time.time())).lastrowid,
           "kind": "music"}
    try:
        item = scanner.upsert_item(lib, root, track, "audio", track.stat(), None)
        assert {r["tag"] for r in db.q("SELECT tag FROM tags WHERE item_id=?", (item,))} >= {"Electronic", "Ambient"}
    finally:
        db.x("DELETE FROM libraries WHERE id=?", (lib["id"],))
