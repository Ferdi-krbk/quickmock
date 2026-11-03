import json
import shlex
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from quickmock.cli import main
from quickmock.server import create_app
from quickmock.store import DataStore


def test_invalid_config_does_not_partially_apply():
    client = TestClient(create_app(DataStore({"items": []})))
    before = client.get("/_admin/state").json()["chaos"]
    response = client.patch("/_admin/config", json={"delay_ms": 123, "error_rate": 101})
    assert response.status_code == 400
    assert client.get("/_admin/state").json()["chaos"] == before


@pytest.mark.parametrize("payload", [{"delay_ms": 1.5}, {"error_status": 500.5}, {"error_status": 1e100}])
def test_integer_settings_reject_fractions_and_overflow(payload):
    client = TestClient(create_app(DataStore({"items": []})))
    assert client.patch("/_admin/config", json=payload).status_code == 400


@pytest.mark.parametrize("operation", ["create", "bulk", "replace", "update", "delete", "reset", "seed", "merge", "drop"])
def test_persistence_failure_rolls_back_memory_and_reset_snapshot(tmp_path, operation):
    path = tmp_path / "db.json"
    store = DataStore({"items": [{"id": 1, "name": "original"}]}, file_path=path)
    store.update("items", 1, {"name": "current"})
    before = store.query("items", {})[0]
    disk = path.read_bytes()
    actions = {
        "create": lambda: store.create("items", {"name": "new"}),
        "bulk": lambda: store.create_bulk("items", [{"name": "new"}]),
        "replace": lambda: store.replace("items", 1, {"name": "changed"}),
        "update": lambda: store.update("items", 1, {"name": "changed"}),
        "delete": lambda: store.delete("items", 1),
        "reset": store.reset,
        "seed": lambda: store.replace_all({"other": [{"id": 3}]}),
        "merge": lambda: store.merge_collections({"other": []}),
        "drop": lambda: store.delete_collection("items"),
    }
    with patch.object(store, "_persist", side_effect=OSError("disk unavailable")):
        with pytest.raises(OSError):
            actions[operation]()
    assert store.query("items", {})[0] == before
    assert not store.has_collection("other")
    assert path.read_bytes() == disk
    assert list(tmp_path.iterdir()) == [path]
    store.reset()
    assert store.get_by_id("items", 1)["name"] == "original"


def test_persistence_never_overwrites_neighboring_tmp_file(tmp_path):
    neighbor = tmp_path / "db.tmp"
    neighbor.write_text("unrelated work", encoding="utf-8")
    store = DataStore({"items": []}, file_path=tmp_path / "db.json")
    store.create("items", {})
    assert neighbor.read_text(encoding="utf-8") == "unrelated work"


def test_invalid_create_does_not_create_collection():
    store = DataStore({})
    with pytest.raises(ValueError):
        store.create("items", {"id": -1})
    assert not store.has_collection("items")


def test_generated_ids_respect_later_explicit_ids():
    store = DataStore({"items": [{"name": "automatic"}, {"id": 1}]})
    rows, _ = store.query("items", {})
    assert len({str(row["id"]) for row in rows}) == 2
    assert rows[1]["id"] == 1


def test_integer_filter_and_sort_keep_exact_precision():
    large = 2**53
    store = DataStore({"items": [{"id": 1, "n": large + 1}, {"id": 2, "n": large}]})
    assert [r["id"] for r in store.query("items", {"n": str(large)})[0]] == [2]
    assert [r["id"] for r in store.query("items", {"_sort": "n"})[0]] == [2, 1]
    assert [r["id"] for r in store.query("items", {"n_gt": str(large)})[0]] == [1]


def test_typescript_sample_and_heterogeneous_arrays():
    store = DataStore({"items": [{"id": i, "values": [True, 3, "x", None]} for i in range(1, 61)]})
    interface = store.generate_typescript("items")["interface"]
    assert "id: number;" in interface
    assert "id?:" not in interface
    assert "values: (boolean | null | number | string)[];" in interface


def test_swagger_uses_live_spec_and_schema_names_do_not_collide():
    client = TestClient(create_app(DataStore({"users": [{"id": 1, "name": "A"}], "user": [{"id": 1, "active": True}]})))
    assert '/_admin/openapi.json' in client.get("/swagger").text
    spec = client.get("/_admin/openapi.json").json()
    refs = [spec["paths"][f"/api/{col}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["items"]["$ref"] for col in ("users", "user")]
    assert refs[0] != refs[1]
    client.post("/api/tasks", json={"title": "task"})
    assert "/api/tasks" in client.get("/_admin/openapi.json").json()["paths"]


def test_openapi_export_cannot_overwrite_source_database(tmp_path):
    path = tmp_path / "db.json"
    data = {"items": [{"id": 1}]}
    path.write_text(json.dumps(data), encoding="utf-8")
    assert main([str(path), "--export-openapi", str(path)]) == 2
    assert json.loads(path.read_text(encoding="utf-8")) == data


def test_cross_origin_admin_access_is_rejected_but_mock_api_remains_cors_enabled():
    client = TestClient(create_app(DataStore({"items": [{"id": 1}]})))
    headers = {"Origin": "https://unrelated.example"}
    assert client.delete("/_admin/collections/items", headers=headers).status_code == 403
    assert client.get("/_admin/state", headers=headers).status_code == 403
    assert client.get("/api/items", headers=headers).headers["access-control-allow-origin"] in ("*", headers["Origin"])
    assert client.get("/_admin/state", headers={"Origin": "http://testserver"}).status_code == 200


def test_failed_atomic_file_replace_returns_storage_error_and_cleans_up(tmp_path):
    path = tmp_path / "db.json"
    store = DataStore({"items": [{"id": 1}]}, file_path=path)
    client = TestClient(create_app(store))
    before = path.read_bytes()
    with patch("quickmock.store.os.replace", side_effect=PermissionError("file locked")):
        response = client.post("/api/items", json={"name": "unsaved"})
    assert response.status_code == 507
    assert client.get("/api/items").json() == [{"id": 1}]
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_read_only_store_blocks_api_by_default():
    client = TestClient(create_app(DataStore({"items": []}, read_only=True)))
    assert client.post("/api/items", json={"name": "blocked"}).status_code == 403
    assert client.get("/api/items").json() == []


@pytest.mark.parametrize("payload", [{"count": 2.5}, {"count": 1e100}, {"mode": "typo"}, {"lang": "unknown"}])
def test_invalid_seed_options_cannot_replace_data(payload):
    client = TestClient(create_app(DataStore({"items": [{"id": 1}]})))
    assert client.post("/_admin/seed", json=payload).status_code == 400
    assert client.get("/api/items").json() == [{"id": 1}]


@pytest.mark.parametrize("option", ["nan", "inf", "-1", "101"])
def test_cli_rejects_invalid_fault_percentage(option):
    assert main(["--error-rate", option]) == 2


def test_codegen_quotes_payload_apostrophes_and_url_literals():
    data = {"items": [{"id": 1, "name": "O'Brien", "line\n": "value"}]}
    base_url = 'http://example.test/"quoted'
    result = DataStore(data).generate_typescript("items", base_url)
    assert f"const API_BASE = {json.dumps(base_url + '/api/items')};" in result["typescript_sdk"]
    assert '"line\\n": string;' in result["interface"]
    # Parse only the generated create command; shell syntax must preserve the JSON.
    curl = result["curl"].split("# Create a new record\n")[1].split("# Collection")[0]
    tokens = shlex.split(curl.replace("\\\n", ""))
    assert base_url + "/api/items" in tokens
    assert json.loads(tokens[tokens.index("-d") + 1])["name"] == "O'Brien"


def test_openapi_update_endpoints_have_request_bodies():
    client = TestClient(create_app(DataStore({"items": [{"id": 1}]})))
    operations = client.get("/_admin/openapi.json").json()["paths"]["/api/items/{id}"]
    for method in ("put", "patch"):
        assert operations[method]["requestBody"]["required"] is True


def test_stats_overflow_is_client_error_and_large_integer_sum_is_exact():
    client = TestClient(create_app(DataStore({"items": [{"id": 1, "n": 1e308}, {"id": 2, "n": 1e308}]})))
    assert client.get("/api/items/_stats").status_code == 400
    large = 2**53 + 1
    metrics = DataStore({"items": [{"id": 1, "n": large}]}).collection_stats("items")
    assert metrics["numeric_fields"]["n"]["sum"] == large


def test_swagger_preserves_auth_bulk_stats_and_json_sample_types():
    store = DataStore({"items": [{"id": 1, "values": [1, True], "empty": None}, {"id": 2, "extra": "present"}]})
    spec = store.generate_openapi()
    for path in ("/api/items/_bulk", "/api/items/_stats", "/api/auth/login", "/api/auth/me"):
        assert path in spec["paths"]
    props = spec["components"]["schemas"]["items"]["properties"]
    assert props["empty"]["nullable"] is True
    assert props["empty"]["example"] is None
    assert props["extra"]["type"] == "string"
    assert {s["type"] for s in props["values"]["items"]["anyOf"]} == {"integer", "boolean"}


def test_concurrent_inserts_leave_disk_and_memory_consistent(tmp_path):
    path = tmp_path / "db.json"
    store = DataStore({"items": []}, file_path=path)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda n: store.create("items", {"n": n}), range(30)))
    rows, _ = store.query("items", {})
    assert len(rows) == len({r["id"] for r in rows}) == 30
    assert {r["n"] for r in rows} == set(range(30))
    assert json.loads(path.read_text(encoding="utf-8"))["items"] == rows


def test_missing_delete_does_not_try_to_write_to_disk():
    store = DataStore({"items": []})
    with patch.object(store, "_persist", side_effect=OSError("disk locked")):
        assert store.delete("items", 404) is False
        assert store.delete_collection("missing") is False
