import json
from datetime import datetime
from pathlib import Path

from fakes import FakePage, bodies_of
from fbtool import db, find as F
from fbtool.config import Config, Group

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 8, 12, 0, 0)


def fixture(kind):
    return json.loads((FIXTURES / f"find_{kind}.json").read_text())["bodies"]


def make_cfg(tmp_path, groups=(), **kw):
    return Config(groups=list(groups), db_path=tmp_path / "t.db", max_scrolls=40,
                  scroll_pause_ms=0, **kw)


def run_find(cfg, kind, max_results=20):
    responses = []
    page = FakePage(responses, script={F.FIND_URL.format(kind=kind): (bodies_of(fixture(kind)), [])})
    code, found = F.find(cfg, kind, "sample", max_results=max_results,
                         session=(page, responses), now=NOW)
    return code, found, page


def test_groups_parsed(tmp_path):
    code, found, page = run_find(make_cfg(tmp_path), "groups")
    assert code == F.EXIT_OK and len(found) == 8
    assert page.gotos == [F.FIND_URL.format(kind="groups") + "sample"]
    assert all(f["name"] and f["url"] and f["summary"] for f in found)
    assert {f["privacy"] for f in found} == {"Public", "Private"}
    assert {f["join_state"] for f in found} <= {"CAN_JOIN", "CAN_REQUEST", "MEMBER"}
    vanity = [f for f in found if not f["slug"].isdigit()]
    assert vanity and all(f"/groups/{f['slug']}/" in f["url"] for f in vanity)
    assert all(f["slug"] == f["id"] for f in found if f["slug"].isdigit())


def test_pages_parsed(tmp_path):
    _, found, _ = run_find(make_cfg(tmp_path), "pages")
    assert len(found) == 8
    assert all(f["join_state"] is None and f["summary"] for f in found)
    assert any(f["description"] for f in found)
    assert all(f["vanity"] == (not f["slug"].isdigit()) for f in found)


def test_budget_and_sources(tmp_path):
    cfg = make_cfg(tmp_path)
    run_find(cfg, "groups", max_results=3)
    con = db.connect(cfg.db_path)
    assert db.budget_used(con, "2026-10-08")["page_loads"] == 1
    assert con.execute("SELECT count(*) FROM sources WHERE kind = 'group'").fetchone()[0] == 3
    cfg.search_budget = {"page_loads": 1, "scrolls": 300, "queries": 20}
    code, found, page = run_find(cfg, "groups")
    assert code == F.EXIT_BUDGET and found == [] and page.gotos == []


def test_yaml_marks_and_skips_configured(tmp_path, capsys):
    _, found, _ = run_find(make_cfg(tmp_path), "groups")
    first = found[0]
    cfg = make_cfg(tmp_path, [Group(slug=first["slug"], name="Mine")])
    F.report(cfg, "groups", found, as_yaml=True)
    out = capsys.readouterr().out
    assert f'slug: "{first["slug"]}"' not in out
    private = [f for f in found[1:] if f["privacy"] == "Private" and f["join_state"] != "MEMBER"]
    assert out.count("# private group: join it first") + out.count("# join required") >= len(private)
    F.report(cfg, "groups", found)
    table = capsys.readouterr().out
    assert table.splitlines()[0].startswith("* ") and "1 monitored" in table
