import threading

from selfheal.healer import ConflictError, ToolDef
from selfheal.store import SqliteRegistry


def test_tenants_are_isolated(tmp_path):
    a = SqliteRegistry(tmp_path / "r.db", tenant="acme")
    b = SqliteRegistry(tmp_path / "r.db", tenant="globex")
    a.register("tool", "/x", {"user_id": "str"})
    a.add(ToolDef("tool", "/x", 0, {"userId": ("user_id", None)}, status="active", origin="auto"))
    assert a.latest_version("tool") == 2
    assert b.versions == {}
    b.register("tool", "/x", {"user_id": "str"})
    assert b.latest_version("tool") == 1 and a.latest_version("tool") == 2


def test_concurrent_writers_never_lose_a_version(tmp_path):
    reg = SqliteRegistry(tmp_path / "r.db", tenant="t")
    reg.register("tool", "/x", {"a": "str"})

    def worker(i):
        SqliteRegistry(tmp_path / "r.db", tenant="t").add(
            ToolDef("tool", "/x", 0, {f"a{i}": ("a", None)}, status="canary", origin="auto", owner=f"agent-{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    versions = [v.version for v in reg.versions["tool"]]
    assert versions == list(range(1, 22))


def test_double_approval_is_refused(tmp_path):
    reg = SqliteRegistry(tmp_path / "r.db", tenant="t")
    reg.register("tool", "/x", {"a": "str"})
    p = reg.propose("tool", 1, ToolDef("tool", "/x", 2, {"b": ("a", None)}), ["test"])
    reg.approve(p.id)
    try:
        reg.approve(p.id)
        assert False, "second approval should fail"
    except ConflictError:
        pass


def test_history_survives_restart_and_is_audited(tmp_path):
    reg = SqliteRegistry(tmp_path / "r.db", tenant="t")
    reg.register("tool", "/x", {"a": "str"})
    reg.add(ToolDef("tool", "/x", 0, {"b": ("a", None)}, status="active", origin="approved"))
    reg.rollback("tool")
    again = SqliteRegistry(tmp_path / "r.db", tenant="t")
    assert [v.status for v in again.versions["tool"]] == ["active", "rolled_back"]
    kinds = [e["kind"] for e in again.events()]
    assert {"registered", "version_added", "rolled_back"} <= set(kinds)
