"""Tests for the "Rensa dubbletter" page backend (src/website/dedupe_api.py)."""
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from website import dedupe_api, server  # noqa: E402

Image = pytest.importorskip("PIL.Image")


def make_image(path: Path, color) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), color).save(path)
    return path


@pytest.fixture
def client():
    server.app.config["TESTING"] = True
    return server.app.test_client()


@pytest.fixture
def folder(tmp_path):
    """raw/ with two unique images and two copies of one of them."""
    raw = tmp_path / "raw"
    red = make_image(raw / "red.png", "red")
    make_image(raw / "blue.png", "blue")
    shutil.copy(red, raw / "red copy.png")
    (raw / "sub").mkdir()
    shutil.copy(red, raw / "sub" / "red again.png")
    return raw


def names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.rglob("*") if p.is_file())


def test_page_and_script_are_served(client):
    assert b"Rensa dubbletter" in client.get("/dedupe").data
    assert client.get("/dedupe.js").status_code == 200
    assert b"dedupe.html" in client.get("/").data          # tab on the start page


def test_dry_run_reports_savings_and_moves_nothing(client, folder):
    before = names(folder)
    r = client.post("/api/dedupe/run", json={"target": str(folder)}).get_json()
    assert names(folder) == before
    assert r["space"]["removed_this_run_files"] == 2
    copy_bytes = (folder / "red.png").stat().st_size
    assert r["space"]["removed_this_run_bytes"] == 2 * copy_bytes
    assert r["web"]["total_seconds"] > 0 and r["web"]["moved"] is False
    assert len(r["removed"]) == 2 and all(x["reason"] for x in r["removed"])


def test_move_puts_rejected_files_in_trash(client, folder, tmp_path):
    trash = tmp_path / "trash"
    r = client.post("/api/dedupe/run", json={"target": str(folder), "move": True,
                                             "trash": str(trash)}).get_json()
    assert r["moved_files"] == 2 and len(names(trash)) == 2
    assert len(names(folder)) == 2                           # red + blue stay
    assert r["web"]["trash_after"]["files"] == 2


def test_trash_inside_target_is_not_scanned(client, folder):
    trash = folder / "_trash"
    body = {"target": str(folder), "move": True, "trash": str(trash)}
    client.post("/api/dedupe/run", json=body)
    r = client.post("/api/dedupe/run", json=body).get_json()  # again: trash not re-read
    assert r["files"]["found"] == 2 and r["space"]["removed_this_run_files"] == 0


def test_auto_delete_needs_the_confirmed_trash_contents(client, folder, tmp_path):
    trash = make_image(tmp_path / "trash" / "old.png", "green").parent
    body = {"target": str(folder), "move": True, "trash": str(trash), "auto_delete": True}

    resp = client.post("/api/dedupe/run", json=body)               # never confirmed
    assert resp.status_code == 400 and len(names(folder)) == 4

    resp = client.post("/api/dedupe/run", json={**body, "confirm": {"files": 0, "bytes": 0}})
    assert resp.status_code == 409                                 # confirmed something else
    assert resp.get_json()["trash"]["files"] == 1 and len(names(folder)) == 4

    info = client.get(f"/api/dedupe/trash?path={trash}").get_json()
    r = client.post("/api/dedupe/run", json={**body, "confirm": info}).get_json()
    assert r["web"]["deleted"]["files"] == 3                       # old.png + 2 copies
    assert names(trash) == [] and len(names(folder)) == 2


def test_empty_trash(client, tmp_path):
    trash = tmp_path / "trash"
    make_image(trash / "a.png", "red")
    make_image(trash / "deep" / "b.png", "blue")
    info = client.get(f"/api/dedupe/trash?path={trash}").get_json()
    assert info["files"] == 2 and info["name"] == "trash" and info["bytes"] > 0

    stale = client.post("/api/dedupe/empty", json={"path": str(trash), "files": 1, "bytes": 1})
    assert stale.status_code == 409 and len(names(trash)) == 2

    r = client.post("/api/dedupe/empty", json=info).get_json()
    assert r["deleted_files"] == 2 and r["freed_bytes"] == info["bytes"]
    assert trash.is_dir() and not any(trash.iterdir())


@pytest.mark.parametrize("protected", [lambda f: Path.home(), lambda f: dedupe_api.PROJECT_DIR,
                                       lambda f: dedupe_api.DATA_DIR, lambda f: f.parent,
                                       lambda f: f])
def test_protected_folders_are_never_used_as_trash(client, folder, protected):
    trash = protected(folder)
    r = client.post("/api/dedupe/run", json={"target": str(folder), "move": True,
                                             "trash": str(trash)})
    assert r.status_code == 400 and len(names(folder)) == 4


def test_cannot_empty_home(client):
    assert client.get(f"/api/dedupe/trash?path={Path.home()}").status_code == 400
    assert client.post("/api/dedupe/empty", json={"path": str(Path.home()), "files": 0,
                                                  "bytes": 0}).status_code == 400


def test_browse_lists_subfolders(client, folder):
    d = client.get(f"/api/dedupe/browse?path={folder}").get_json()
    assert [x["name"] for x in d["dirs"]] == ["sub"]
    assert d["supported_files_here"] == 3 and d["parent"] == str(folder.parent)
    assert client.get(f"/api/dedupe/browse?path={folder / 'nope'}").status_code == 404
