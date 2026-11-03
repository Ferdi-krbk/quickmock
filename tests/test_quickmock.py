from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quickmock import seeder
from quickmock.cli import main as cli_main
from quickmock.server import ChaosConfig, _make_mock_jwt, create_app
from quickmock.store import DataStore, _pascal_case, _pluralize, _singularize, validate_collection_name


def _make_sample_store(tmp_path: Path | None = None) -> DataStore:
    data = {
        "users": [
            {"id": 1, "name": "Ahmet Yılmaz", "role": "admin", "active": True},
            {"id": 2, "name": "Zeynep Kaya", "role": "customer", "active": True},
            {"id": 3, "name": "Mehmet Demir", "role": "customer", "active": False},
        ],
        "products": [
            {"id": 10, "title": "Mekanik Klavye", "price": 2500.0, "stock": 15},
            {"id": 20, "title": "Kablosuz Mouse", "price": 900.0, "stock": 40},
            {"id": 30, "title": "4K Monitör", "price": 8500.0, "stock": 5},
        ],
        "orders": [
            {"id": 101, "user_id": 1, "product_id": 10, "totalAmount": 2500.0},
            {"id": 102, "user_id": 2, "product_id": 20, "totalAmount": 1800.0},
        ],
    }
    db_file = (tmp_path / "db.json") if tmp_path else None
    return DataStore(initial_data=data, file_path=db_file)


def test_crud_lifecycle(tmp_path: Path):
    store = _make_sample_store(tmp_path)
    client = TestClient(create_app(store=store))

    # LIST
    res = client.get("/api/products")
    assert res.status_code == 200
    assert res.headers["X-Total-Count"] == "3"
    assert len(res.json()) == 3

    # GET single
    res = client.get("/api/products/10")
    assert res.status_code == 200
    assert res.json()["title"] == "Mekanik Klavye"
    assert client.get("/api/products/9999").status_code == 404
    assert client.get("/api/nonexistent").status_code == 404

    # CREATE (auto id & createdAt)
    res = client.post("/api/products", json={"title": "USB-C Hub", "price": 650.0, "stock": 25})
    assert res.status_code == 201
    created = res.json()
    assert created["id"] == 31
    assert "createdAt" in created

    # Duplicate ID -> 409 Conflict
    dup = client.post("/api/products", json={"id": 31, "title": "Duplicate"})
    assert dup.status_code == 409

    # PATCH
    res = client.patch("/api/products/31", json={"price": 599.0, "id": 999})
    assert res.status_code == 200
    assert res.json()["id"] == 31  # id cannot be overwritten
    assert res.json()["price"] == 599.0
    assert res.json()["title"] == "USB-C Hub"
    assert "updatedAt" in res.json()
    assert client.patch("/api/products/9999", json={"price": 1.0}).status_code == 404

    # PUT (replace)
    res = client.put("/api/products/31", json={"title": "Thunderbolt Dock", "price": 4200.0, "id": 888})
    assert res.status_code == 200
    assert res.json()["id"] == 31
    assert "stock" not in res.json()
    assert client.put("/api/products/9999", json={"title": "Ghost"}).status_code == 404

    # DELETE
    res = client.delete("/api/products/31")
    assert res.status_code == 200
    assert client.get("/api/products/31").status_code == 404
    assert client.delete("/api/products/31").status_code == 404

    # Verify JSON persistence on disk
    persisted = json.loads((tmp_path / "db.json").read_text(encoding="utf-8"))
    assert len(persisted["products"]) == 3


def test_filtering_operators_and_boolean_int_isolation():
    client = TestClient(create_app(store=_make_sample_store()))

    # Exact filter + boolean coercion
    res = client.get("/api/users?role=customer&active=true")
    assert res.status_code == 200
    assert [u["id"] for u in res.json()] == [2]

    # Invalid boolean string should NOT match active=False
    res_bad_bool = client.get("/api/users?active=banana")
    assert res_bad_bool.json() == []

    # Inequality (_ne) case-insensitive
    res_ne = client.get("/api/users?role_ne=CUSTOMER")
    assert [u["id"] for u in res_ne.json()] == [1]

    # Substring match (_like)
    res_like = client.get("/api/products?title_like=kablosuz")
    assert [p["id"] for p in res_like.json()] == [20]

    # Numeric range (_gte, _lte, _gt, _lt)
    assert [p["id"] for p in client.get("/api/products?price_gte=1000&price_lte=5000").json()] == [10]
    assert [p["id"] for p in client.get("/api/products?price_gt=2500").json()] == [30]
    assert [p["id"] for p in client.get("/api/products?price_lt=2500").json()] == [20]

    # Full-text search (?q=)
    assert [p["id"] for p in client.get("/api/products?q=monitör").json()] == [30]


def test_sorting_nulls_last_in_both_directions():
    client = TestClient(create_app(store=_make_sample_store()))
    client.post("/api/products", json={"id": 40, "title": "Fiyatsız Ürün", "price": None, "stock": None})

    # ASC sort: None must be at the very end
    asc_ids = [p["id"] for p in client.get("/api/products?_sort=price&_order=asc").json()]
    assert asc_ids == [20, 10, 30, 40]

    # DESC sort: None must STILL be at the very end (NULLS LAST)
    desc_ids = [p["id"] for p in client.get("/api/products?_sort=price&_order=desc").json()]
    assert desc_ids == [30, 10, 20, 40]

    # Invalid sort order -> 400
    assert client.get("/api/products?_sort=price&_order=sideways").status_code == 400


def test_pagination_page_limit_and_offset_combinations():
    client = TestClient(create_app(store=_make_sample_store()))

    # _page + _limit
    res = client.get("/api/products?_sort=price&_order=asc&_page=2&_limit=1")
    assert res.headers["X-Total-Count"] == "3"
    assert [p["id"] for p in res.json()] == [10]

    # _offset without _limit
    res_off = client.get("/api/products?_sort=price&_order=asc&_offset=2")
    assert [p["id"] for p in res_off.json()] == [30]

    # _page without _limit (defaults to 10 per page)
    res_page2 = client.get("/api/products?_page=2")
    assert res_page2.json() == []

    # Invalid pagination parameters -> 400
    assert client.get("/api/products?_limit=0").status_code == 400
    assert client.get("/api/products?_limit=-5").status_code == 400
    assert client.get("/api/products?_limit=abc").status_code == 400
    assert client.get("/api/products?_page=0").status_code == 400
    assert client.get("/api/products?_page=-1").status_code == 400
    assert client.get("/api/products?_offset=-1").status_code == 400


def test_relational_expand_and_embed_including_multi_expand():
    client = TestClient(create_app(store=_make_sample_store()))

    # Single _expand=user on orders
    res = client.get("/api/orders/101?_expand=user")
    assert res.status_code == 200
    assert res.json()["user"]["name"] == "Ahmet Yılmaz"

    # Repeated query params: ?_expand=user&_expand=product
    res_multi = client.get("/api/orders/101?_expand=user&_expand=product")
    assert res_multi.status_code == 200
    body = res_multi.json()
    assert body["user"]["name"] == "Ahmet Yılmaz"
    assert body["product"]["title"] == "Mekanik Klavye"

    # Plural _expand=users also resolves user_id -> user
    res_plural = client.get("/api/orders?_expand=users&_expand=products")
    assert res_plural.status_code == 200
    assert res_plural.json()[0]["user"]["name"] == "Ahmet Yılmaz"
    assert res_plural.json()[0]["product"]["title"] == "Mekanik Klavye"

    # _embed=orders on users attaches child orders
    res_emb = client.get("/api/users/1?_embed=orders")
    assert res_emb.status_code == 200
    assert len(res_emb.json()["orders"]) == 1
    assert res_emb.json()["orders"][0]["id"] == 101


def test_chaos_fault_injection_jwt_security_and_clean_reset():
    chaos = ChaosConfig(error_rate=100.0, error_status=503)
    client = TestClient(create_app(store=_make_sample_store(), chaos=chaos))

    # 100% fault rate -> 503
    res = client.get("/api/products")
    assert res.status_code == 503
    assert res.json()["chaos"] is True

    # Enable Auth Guard + Read-Only
    client.patch("/_admin/config", json={"error_rate": 0.0, "require_auth": True, "read_only": True})
    assert client.get("/api/products").status_code == 401
    assert client.get("/api/products", headers={"Authorization": "Bearer fake.jwt.token"}).status_code == 401

    # Expired JWT -> 401
    expired_token = _make_mock_jwt({"sub": "1", "email": "a@b.c", "exp": int(time.time()) - 10})
    assert client.get("/api/products", headers={"Authorization": f"Bearer {expired_token}"}).status_code == 401

    # Non-dict JSON body on /api/auth/login should not crash with 500
    login_list = client.post("/api/auth/login", json=["not", "a", "dict"])
    assert login_list.status_code == 200
    token = login_list.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Authenticated GET succeeds, POST blocked by Read-Only -> 403
    assert client.get("/api/products", headers=headers).status_code == 200
    assert client.post("/api/products", json={"title": "Test"}, headers=headers).status_code == 403

    # String boolean "false" in /_admin/config properly disables read_only & require_auth
    cfg_res = client.patch("/_admin/config", json={"require_auth": "false", "read_only": "false"})
    assert cfg_res.status_code == 200
    assert cfg_res.json()["require_auth"] is False
    assert cfg_res.json()["read_only"] is False

    # Re-enable guards and verify 'clean' chaos preset resets both require_auth and read_only
    client.patch("/_admin/config", json={"require_auth": True, "read_only": True})
    clean_res = client.post("/_admin/chaos-preset", json={"preset": "clean"})
    assert clean_res.status_code == 200
    assert clean_res.json()["require_auth"] is False
    assert clean_res.json()["read_only"] is False
    assert client.post("/api/products", json={"title": "Unlocked"}).status_code == 201


def test_atomic_bulk_insert_and_stats():
    client = TestClient(create_app(store=_make_sample_store()))

    # Valid bulk insert
    bulk_res = client.post(
        "/api/products/_bulk",
        json=[
            {"title": "Webcam HD", "price": 1200.0, "stock": 12},
            {"title": "Mikrofon", "price": 3400.0, "stock": 8},
        ],
    )
    assert bulk_res.status_code == 201
    assert bulk_res.json()["inserted"] == 2
    assert len(client.get("/api/products").json()) == 5

    # Atomic rollback when 2nd item has duplicate ID or invalid type
    assert client.post("/api/products/_bulk", json=[{"title": "Item1"}, {"id": 10, "title": "Dup"}]).status_code == 409
    assert len(client.get("/api/products").json()) == 5
    assert client.post("/api/products/_bulk", json=[{"title": "Item1"}, "bad_item"]).status_code == 400
    assert len(client.get("/api/products").json()) == 5
    assert client.post("/api/products/_bulk", json=[]).status_code == 400

    # Numeric stats aggregation
    stats = client.get("/api/products/_stats").json()
    assert stats["total_records"] == 5
    assert stats["numeric_fields"]["price"]["min"] == 900.0
    assert stats["numeric_fields"]["price"]["max"] == 8500.0


def test_nan_infinity_and_malformed_payload_rejection():
    client = TestClient(create_app(store=_make_sample_store()))

    # Query filter with NaN / Infinity
    assert client.get("/api/products?price_gte=nan").status_code == 400
    assert client.get("/api/products?price_lte=inf").status_code == 400
    assert client.get("/api/products?price_gt=-inf").status_code == 400

    # Raw JSON body containing NaN or Infinity must be rejected with 400 (never stored)
    raw_nan = b'{"title": "Broken", "price": NaN}'
    assert client.post("/api/products", content=raw_nan, headers={"Content-Type": "application/json"}).status_code == 400
    assert client.patch("/api/products/10", content=raw_nan, headers={"Content-Type": "application/json"}).status_code == 400
    assert client.put("/api/products/10", content=raw_nan, headers={"Content-Type": "application/json"}).status_code == 400

    # Non-dict JSON payloads on object endpoints -> 400
    assert client.post("/api/products", json=["list"]).status_code == 400
    assert client.patch("/_admin/config", json=["list"]).status_code == 400
    assert client.post("/_admin/seed", json="string").status_code == 400

    # Boolean passed as integer in /_admin/config -> 400
    assert client.patch("/_admin/config", json={"delay_ms": True}).status_code == 400
    assert client.patch("/_admin/config", json={"error_status": 200}).status_code == 400
    assert client.patch("/_admin/config", json={"error_rate": 150}).status_code == 400


def test_singularize_pluralize_and_typescript_codegen():
    assert _singularize("status") == "status"
    assert _singularize("address") == "address"
    assert _singularize("categories") == "category"
    assert _pluralize("category") == "categories"
    assert _pascal_case("user_statuses") == "UserStatus"

    client = TestClient(create_app(store=_make_sample_store()))
    # Add a product with a hyphenated key to verify TypeScript property quoting
    client.post("/api/products", json={"title": "Test", "price": 100.0, "first-name": "özel"})
    cg = client.get("/_admin/codegen/products").json()
    assert "export interface Product" in cg["typescript_sdk"]
    assert '"first-name"?: string;' in cg["typescript_sdk"]
    assert "export async function listProducts" in cg["typescript_sdk"]


def test_seed_validation_and_cli_validation(tmp_path: Path):
    client = TestClient(create_app(store=_make_sample_store()))

    # Unknown preset -> 400
    assert client.post("/_admin/seed", json={"preset": "nonexistent_preset"}).status_code == 400
    # Reserved collection name in custom schema -> 400
    assert client.post("/_admin/seed", json={"schema": "auth: token"}).status_code == 400
    assert client.post("/_admin/seed", json={"schema": "demo: title"}).status_code == 400
    # Invalid count -> 400
    assert client.post("/_admin/seed", json={"preset": "blog", "count": 0}).status_code == 400
    assert client.post("/_admin/seed", json={"preset": "blog", "count": True}).status_code == 400

    # Valid custom schema seed
    res = client.post(
        "/_admin/seed",
        json={"schema": "cars: brand, price:float, year:int", "count": 5, "lang": "tr", "mode": "merge"},
    )
    assert res.status_code == 200
    assert len(client.get("/api/cars").json()) == 5

    # CLI validation tests
    assert cli_main(["--port", "0"]) == 2
    assert cli_main(["--count", "0"]) == 2
    assert cli_main(["--error-status", "200"]) == 2

    out_spec = tmp_path / "openapi.json"
    assert cli_main(["--preset", "blog", "--export-openapi", str(out_spec)]) == 0
    assert out_spec.is_file()

    # UI routes
    assert client.get("/").status_code == 200
    assert client.get("/demo").status_code == 200


def test_cors_preflight_and_headers_on_chaos_and_auth_errors():
    chaos = ChaosConfig(error_rate=100.0, error_status=503, require_auth=True, read_only=True)
    client = TestClient(create_app(store=_make_sample_store(), chaos=chaos))
    cors_headers = {
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "Authorization, Content-Type",
    }

    # OPTIONS preflight must succeed (200) and never be blocked by Chaos 503 or Auth 401
    opt_res = client.options("/api/products", headers=cors_headers)
    assert opt_res.status_code == 200
    assert "access-control-allow-origin" in opt_res.headers

    # Even when 503 Chaos fault is returned, Access-Control-Allow-Origin must be present
    err_res = client.get("/api/products", headers={"Origin": "http://localhost:3000"})
    assert err_res.status_code == 503
    assert "access-control-allow-origin" in err_res.headers


def test_multi_value_filter_and_joined_field_query():
    client = TestClient(create_app(store=_make_sample_store()))

    # Multi-value OR filter: ?id=10&id=30
    res_multi = client.get("/api/products?id=10&id=30")
    assert res_multi.status_code == 200
    assert [p["id"] for p in res_multi.json()] == [10, 30]

    # Filter and full-text search on expanded relational fields
    res_join_filter = client.get("/api/orders?_expand=user&user.role=admin")
    assert [o["id"] for o in res_join_filter.json()] == [101]

    res_join_search = client.get("/api/orders?_expand=user&q=zeynep")
    assert [o["id"] for o in res_join_search.json()] == [102]


def test_item_id_validation_and_initial_data_normalization():
    # Dataset with missing IDs gets sequential IDs automatically
    store = DataStore({"todos": [{"title": "Task A"}, {"title": "Task B"}]})
    client = TestClient(create_app(store=store))
    todos = client.get("/api/todos").json()
    assert [t["id"] for t in todos] == [1, 2]
    assert client.get("/api/todos/1").status_code == 200

    # Invalid explicit item IDs (negative, list, dict, URL-unsafe chars) -> 400
    assert client.post("/api/todos", json={"id": -5, "title": "Bad"}).status_code == 400
    assert client.post("/api/todos", json={"id": [1, 2], "title": "Bad"}).status_code == 400
    assert client.post("/api/todos", json={"id": {"a": 1}, "title": "Bad"}).status_code == 400
    assert client.post("/api/todos", json={"id": "bad?id#1", "title": "Bad"}).status_code == 400
    assert client.post("/api/todos", json={"id": "_bulk", "title": "Bad"}).status_code == 400

