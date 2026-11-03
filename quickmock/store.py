"""In-Memory & JSON-Backed Data Store with Type-Safe Query Engine, Relational Joins, Stats, OpenAPI 3.0, and TypeScript Codegen."""
from __future__ import annotations

import copy
import json
import math
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COLLECTION_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
ITEM_ID_STR_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
JS_IDENT_RE = re.compile(r"^[a-zA-Z_$][a-zA-Z0-9_$]*$")
RESERVED_COLLECTIONS = {"auth", "_admin", "demo", "swagger"}
RESERVED_ITEM_IDS = {"_stats", "_bulk"}


def validate_collection_name(name: str) -> str:
    if not isinstance(name, str) or not COLLECTION_NAME_RE.match(name) or name.lower() in RESERVED_COLLECTIONS:
        raise ValueError(f"Invalid or reserved collection name: '{name}'")
    return name


def validate_item_id(raw_id: Any) -> int | str:
    """Validate that an explicit item ID is a positive int or a URL-safe string."""
    if isinstance(raw_id, bool) or raw_id is None:
        raise ValueError("Item 'id' must be a positive integer or URL-safe string")
    if isinstance(raw_id, int):
        if raw_id < 1:
            raise ValueError(f"Numeric item 'id' must be >= 1, got {raw_id}")
        return raw_id
    if isinstance(raw_id, str):
        s = raw_id.strip()
        if not ITEM_ID_STR_RE.match(s) or s in RESERVED_ITEM_IDS:
            raise ValueError(f"Reserved or invalid item id: '{raw_id}'")
        return s
    raise ValueError(f"Item 'id' must be a positive integer or URL-safe string, got {type(raw_id).__name__}")


def validate_json_values(obj: Any, path: str = "root") -> None:
    """Recursively ensure no NaN or Infinity floats exist inside a JSON payload."""
    if isinstance(obj, float) and not math.isfinite(obj):
        raise ValueError(f"Non-finite number (NaN/Infinity) is not allowed at '{path}'")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ValueError(f"Object keys must be strings at '{path}'")
            validate_json_values(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for idx, elem in enumerate(obj):
            validate_json_values(elem, f"{path}[{idx}]")


def _get_field(row: dict[str, Any], field_path: str) -> tuple[bool, Any]:
    """Resolve either a direct key or a dotted path (e.g. 'user.role') inside a row."""
    if field_path in row:
        return True, row[field_path]
    if "." not in field_path:
        return False, None
    cur: Any = row
    for part in field_path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def _coerce_val(raw: str, target_val: Any) -> Any:
    """Coerce query string value to match target field type."""
    if isinstance(target_val, bool):
        low = raw.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
        return raw
    if isinstance(target_val, int) and not isinstance(target_val, bool):
        try:
            return int(raw)
        except ValueError:
            return raw
    if isinstance(target_val, float):
        try:
            f = float(raw)
            return f if math.isfinite(f) else raw
        except ValueError:
            return raw
    return raw


def _values_equal(row_val: Any, query_raw: str) -> bool:
    if row_val is None:
        return query_raw.strip().lower() == "null"
    coerced = _coerce_val(query_raw, row_val)
    if isinstance(row_val, bool):
        return isinstance(coerced, bool) and row_val is coerced
    if isinstance(row_val, (int, float)) and not isinstance(row_val, bool):
        if isinstance(coerced, (int, float)) and not isinstance(coerced, bool):
            return float(row_val) == float(coerced)
        return False
    return str(row_val).lower() == query_raw.lower()


def _parse_query_float(param_name: str, raw: str) -> float:
    try:
        val = float(raw)
    except ValueError:
        raise ValueError(f"Query parameter '{param_name}' expects a finite numeric value, got '{raw}'")
    if not math.isfinite(val):
        raise ValueError(f"Query parameter '{param_name}' must be finite, got '{raw}'")
    return val


def _row_float(val: Any) -> float | None:
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return f if math.isfinite(f) else None
    try:
        f = float(str(val))
        return f if math.isfinite(f) else None
    except ValueError:
        return None


def _safe_sort_key(val: Any) -> tuple[int, float, str]:
    """Type-safe sort key for non-None values (bool < number < string)."""
    if isinstance(val, bool):
        return (0, 1.0 if val else 0.0, "")
    if isinstance(val, (int, float)):
        f = float(val)
        return (1, f if math.isfinite(f) else 0.0, "")
    return (2, 0.0, str(val).lower())


def _singularize(plural: str) -> str:
    low = plural.lower()
    if low.endswith(("ss", "us", "is", "news")):
        return plural
    if low.endswith("ies") and len(plural) > 3:
        return plural[:-3] + "y"
    if low.endswith(("ses", "xes", "zes", "ches", "shes")) and len(plural) > 3:
        return plural[:-2]
    if low.endswith("s") and len(plural) > 1:
        return plural[:-1]
    return plural


def _pluralize(singular: str) -> str:
    low = singular.lower()
    if low.endswith("y") and len(singular) > 1 and low[-2] not in "aeiou":
        return singular[:-1] + "ies"
    if low.endswith(("s", "x", "z", "ch", "sh")):
        return singular if low.endswith("es") or low.endswith("s") else singular + "es"
    return singular + "s"


def _pascal_case(name: str) -> str:
    parts = re.split(r"[-_]+", _singularize(name))
    ident = "".join(p.capitalize() for p in parts if p) or "Item"
    if ident[0].isdigit():
        ident = "Item" + ident
    return ident


def _normalize_and_validate_dataset(data: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(data, dict):
        raise ValueError("Dataset must be a JSON object mapping collection names to arrays of objects")
    normalized: dict[str, list[dict[str, Any]]] = {}
    for col, rows in data.items():
        validate_collection_name(col)
        if not isinstance(rows, list):
            raise ValueError(f"Collection '{col}' must be a JSON array")
        norm_rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for idx, r in enumerate(rows):
            if not isinstance(r, dict):
                raise ValueError(f"Collection '{col}' item at index {idx} must be a JSON object")
            validate_json_values(r, f"{col}[{idx}]")
            item = copy.deepcopy(r)
            if "id" not in item or item["id"] is None or (isinstance(item["id"], str) and not item["id"].strip()):
                next_id = idx + 1
                while str(next_id) in seen_ids:
                    next_id += 1
                item = {"id": next_id, **{k: v for k, v in item.items() if k != "id"}}
            else:
                item["id"] = validate_item_id(item["id"])
                if str(item["id"]) in seen_ids:
                    raise ValueError(f"Duplicate id '{item['id']}' in collection '{col}'")
            seen_ids.add(str(item["id"]))
            norm_rows.append(item)
        normalized[col] = norm_rows
    return normalized


class DataStore:
    def __init__(
        self,
        initial_data: dict[str, list[dict[str, Any]]],
        file_path: Path | None = None,
        read_only: bool = False,
    ) -> None:
        clean_data = _normalize_and_validate_dataset(initial_data)
        self._lock = threading.RLock()
        self._initial_snapshot = copy.deepcopy(clean_data)
        self._data: dict[str, list[dict[str, Any]]] = copy.deepcopy(clean_data)
        self.file_path = file_path
        self.read_only = read_only
        if self.file_path and not self.file_path.exists():
            self._persist(force=True)

    def _persist(self, force: bool = False) -> None:
        if not self.file_path or (self.read_only and not force):
            return
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        tmp.replace(self.file_path)

    def reset(self) -> dict[str, int]:
        """Restore store to initial snapshot."""
        with self._lock:
            self._data = copy.deepcopy(self._initial_snapshot)
            self._persist(force=True)
            return {k: len(v) for k, v in self._data.items()}

    def replace_all(self, new_data: dict[str, list[dict[str, Any]]], update_snapshot: bool = True) -> dict[str, int]:
        clean_data = _normalize_and_validate_dataset(new_data)
        with self._lock:
            self._data = copy.deepcopy(clean_data)
            if update_snapshot:
                self._initial_snapshot = copy.deepcopy(clean_data)
            self._persist(force=True)
            return {k: len(v) for k, v in self._data.items()}

    def merge_collections(self, extra_data: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
        clean_extra = _normalize_and_validate_dataset(extra_data)
        with self._lock:
            for col, rows in clean_extra.items():
                self._data[col] = copy.deepcopy(rows)
            self._persist(force=True)
            return {k: len(v) for k, v in self._data.items()}

    def collections_summary(self) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for name, rows in self._data.items():
                sample_keys = list(rows[0].keys()) if rows and isinstance(rows[0], dict) else ["id"]
                out.append({
                    "name": name,
                    "count": len(rows),
                    "fields": sample_keys,
                    "sample": rows[0] if rows else None,
                })
            return out

    def has_collection(self, collection: str) -> bool:
        with self._lock:
            return collection in self._data

    def delete_collection(self, collection: str) -> bool:
        with self._lock:
            if collection not in self._data:
                return False
            del self._data[collection]
            self._persist(force=True)
            return True

    def collection_stats(self, collection: str) -> dict[str, Any]:
        """Compute count and numeric aggregations (min, max, avg, sum) for a collection."""
        with self._lock:
            rows = self._data.get(collection)
            if rows is None:
                raise KeyError(collection)
            numeric_buckets: dict[str, list[float]] = {}
            for r in rows:
                for k, v in r.items():
                    if k == "id" or isinstance(v, bool):
                        continue
                    if isinstance(v, (int, float)) and math.isfinite(float(v)):
                        numeric_buckets.setdefault(k, []).append(float(v))

            metrics: dict[str, Any] = {}
            for field, vals in numeric_buckets.items():
                if vals:
                    metrics[field] = {
                        "min": round(min(vals), 2),
                        "max": round(max(vals), 2),
                        "avg": round(sum(vals) / len(vals), 2),
                        "sum": round(sum(vals), 2),
                        "count": len(vals),
                    }
            return {
                "collection": collection,
                "total_records": len(rows),
                "numeric_fields": metrics,
            }

    def _expand_item(
        self,
        collection: str,
        item: dict[str, Any],
        expands: list[str],
        embeds: list[str],
    ) -> dict[str, Any]:
        if not expands and not embeds:
            return item
        enriched = copy.deepcopy(item)

        for rel in expands:
            rel = rel.strip()
            if not rel:
                continue
            singular_rel = _singularize(rel)
            target_col = _pluralize(singular_rel) if rel not in self._data else rel
            fk_candidates = [f"{singular_rel}_id", f"{singular_rel}Id", f"{rel}_id", f"{rel}Id"]
            fk_val = None
            for fk in fk_candidates:
                if fk in enriched:
                    fk_val = enriched[fk]
                    break
            if fk_val is not None and target_col in self._data:
                match = next((r for r in self._data[target_col] if str(r.get("id")) == str(fk_val)), None)
                enriched[singular_rel] = copy.deepcopy(match)

        singular_self = _singularize(collection)
        fk_names = [f"{singular_self}_id", f"{singular_self}Id", f"{collection}_id"]
        for emb in embeds:
            emb = emb.strip()
            if not emb or emb not in self._data:
                continue
            children = [
                copy.deepcopy(r)
                for r in self._data[emb]
                if any(str(r.get(fk)) == str(item.get("id")) for fk in fk_names)
            ]
            enriched[emb] = children

        return enriched

    def query(self, collection: str, params: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        """Filter, search, sort, paginate, and relationally expand a collection."""
        with self._lock:
            raw_rows = self._data.get(collection)
            if raw_rows is None:
                raise KeyError(collection)
            rows = [copy.deepcopy(r) for r in raw_rows]

            def _single_str(k: str, default: str = "") -> str:
                v = params.get(k)
                if v is None:
                    return default
                if isinstance(v, list):
                    return str(v[-1]) if v else default
                return str(v)

            q_text = (_single_str("q") or _single_str("search")).strip().lower()
            sort_spec = _single_str("_sort") or _single_str("sort")
            order_spec = _single_str("_order") or _single_str("order") or "asc"
            page_raw = params.get("_page") if "_page" in params else params.get("page")
            limit_raw = params.get("_limit") if "_limit" in params else params.get("limit")
            offset_raw = params.get("_offset") if "_offset" in params else params.get("offset")
            expand_raw = _single_str("_expand") or _single_str("expand")
            embed_raw = _single_str("_embed") or _single_str("embed")

            # 1. Relational expansion (_expand / _embed) so filtering/searching/sorting works on joined fields too
            expands = [x for x in expand_raw.split(",") if x.strip()]
            embeds = [x for x in embed_raw.split(",") if x.strip()]
            if expands or embeds:
                rows = [self._expand_item(collection, r, expands, embeds) for r in rows]

            reserved = {
                "q", "search", "_sort", "sort", "_order", "order",
                "_page", "page", "_limit", "limit", "_offset", "offset",
                "_expand", "expand", "_embed", "embed",
            }

            # 2. Field filters (including multi-value OR and dotted paths) & range operators
            for key, raw_val in params.items():
                if key in reserved or raw_val == "" or raw_val == []:
                    continue
                val_list = [str(x) for x in raw_val] if isinstance(raw_val, list) else [str(raw_val)]
                val = val_list[-1]

                if key.endswith("_gte"):
                    field = key[:-4]
                    target = _parse_query_float(key, val)
                    rows = [r for r in rows if (rf := _row_float(_get_field(r, field)[1])) is not None and rf >= target]
                elif key.endswith("_lte"):
                    field = key[:-4]
                    target = _parse_query_float(key, val)
                    rows = [r for r in rows if (rf := _row_float(_get_field(r, field)[1])) is not None and rf <= target]
                elif key.endswith("_gt"):
                    field = key[:-3]
                    target = _parse_query_float(key, val)
                    rows = [r for r in rows if (rf := _row_float(_get_field(r, field)[1])) is not None and rf > target]
                elif key.endswith("_lt"):
                    field = key[:-3]
                    target = _parse_query_float(key, val)
                    rows = [r for r in rows if (rf := _row_float(_get_field(r, field)[1])) is not None and rf < target]
                elif key.endswith("_ne"):
                    field = key[:-3]
                    rows = [
                        r for r in rows
                        if not (found := _get_field(r, field))[0] or all(not _values_equal(found[1], v) for v in val_list)
                    ]
                elif key.endswith("_like"):
                    field = key[:-5]
                    needles = [v.lower() for v in val_list]
                    rows = [
                        r for r in rows
                        if (found := _get_field(r, field))[0] and any(n in str(found[1]).lower() for n in needles)
                    ]
                else:
                    rows = [
                        r for r in rows
                        if (found := _get_field(r, key))[0] and any(_values_equal(found[1], v) for v in val_list)
                    ]

            # 3. Full-text search across all values
            if q_text:
                rows = [
                    r for r in rows
                    if any(q_text in str(v).lower() for v in r.values())
                ]

            total_count = len(rows)

            # 4. Type-safe sorting with NULLS LAST in both ASC and DESC
            if sort_spec:
                if ":" in sort_spec:
                    s_field, s_dir = sort_spec.split(":", 1)
                else:
                    s_field, s_dir = sort_spec, order_spec
                s_field = s_field.strip()
                dir_clean = s_dir.strip().lower()
                if dir_clean not in ("asc", "desc"):
                    raise ValueError(f"Invalid sort order '{s_dir}': expected 'asc' or 'desc'")
                reverse = dir_clean == "desc"
                present = [r for r in rows if _get_field(r, s_field)[1] is not None]
                missing = [r for r in rows if _get_field(r, s_field)[1] is None]
                present.sort(key=lambda r: _safe_sort_key(_get_field(r, s_field)[1]), reverse=reverse)
                rows = present + missing

            # 5. Pagination (_limit, _page, _offset)
            if limit_raw is not None or page_raw is not None or offset_raw is not None:
                if isinstance(limit_raw, list):
                    limit_raw = limit_raw[-1]
                if isinstance(page_raw, list):
                    page_raw = page_raw[-1]
                if isinstance(offset_raw, list):
                    offset_raw = offset_raw[-1]

                if limit_raw is not None:
                    try:
                        limit = int(limit_raw)
                    except ValueError:
                        raise ValueError(f"Invalid _limit value: '{limit_raw}'")
                    if limit < 1:
                        raise ValueError(f"_limit must be >= 1, got '{limit_raw}'")
                elif page_raw is not None:
                    limit = 10
                else:
                    limit = None

                if page_raw is not None:
                    try:
                        page = int(page_raw)
                    except ValueError:
                        raise ValueError(f"Invalid _page value: '{page_raw}'")
                    if page < 1:
                        raise ValueError(f"_page must be >= 1, got '{page_raw}'")
                    start = (page - 1) * (limit or 10)
                elif offset_raw is not None:
                    try:
                        start = int(offset_raw)
                    except ValueError:
                        raise ValueError(f"Invalid _offset value: '{offset_raw}'")
                    if start < 0:
                        raise ValueError(f"_offset must be >= 0, got '{offset_raw}'")
                else:
                    start = 0

                rows = rows[start : (start + limit) if limit is not None else None]

            return rows, total_count

    def get_by_id(
        self,
        collection: str,
        item_id: str | int,
        expand_raw: str = "",
        embed_raw: str = "",
    ) -> dict[str, Any] | None:
        with self._lock:
            rows = self._data.get(collection)
            if rows is None:
                raise KeyError(collection)
            found = next((r for r in rows if str(r.get("id")) == str(item_id)), None)
            if not found:
                return None
            expands = [x for x in expand_raw.split(",") if x.strip()]
            embeds = [x for x in embed_raw.split(",") if x.strip()]
            return self._expand_item(collection, copy.deepcopy(found), expands, embeds)

    def _prepare_new_item(self, rows: list[dict[str, Any]], payload: dict[str, Any], seen_ids: set[str]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Record payload must be a JSON object")
        validate_json_values(payload)
        item = copy.deepcopy(payload)
        raw_id = item.get("id")
        if raw_id is None or (isinstance(raw_id, str) and not raw_id.strip()):
            int_ids = [
                int(r["id"])
                for r in rows
                if "id" in r and not isinstance(r["id"], bool) and str(r["id"]).isdigit()
            ]
            next_id = (max(int_ids) + 1) if int_ids else (len(rows) + 1)
            while str(next_id) in seen_ids:
                next_id += 1
            item = {"id": next_id, **{k: v for k, v in item.items() if k != "id"}}
        else:
            valid_id = validate_item_id(raw_id)
            if str(valid_id) in seen_ids:
                raise ValueError(f"Duplicate id: {valid_id}")
            item["id"] = valid_id
        seen_ids.add(str(item["id"]))
        if "createdAt" not in item:
            item["createdAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return item

    def create(self, collection: str, payload: dict[str, Any]) -> dict[str, Any]:
        validate_collection_name(collection)
        with self._lock:
            if collection not in self._data:
                self._data[collection] = []
            rows = self._data[collection]
            seen_ids = {str(r.get("id")) for r in rows if "id" in r}
            item = self._prepare_new_item(rows, payload, seen_ids)
            rows.append(item)
            self._persist()
            return copy.deepcopy(item)

    def create_bulk(self, collection: str, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        validate_collection_name(collection)
        if not isinstance(payloads, list) or not (1 <= len(payloads) <= 500):
            raise ValueError("Bulk payload must be a JSON array of 1 to 500 objects")
        with self._lock:
            existing_rows = self._data.get(collection, [])
            working_rows = list(existing_rows)
            seen_ids = {str(r.get("id")) for r in working_rows if "id" in r}
            created_items: list[dict[str, Any]] = []
            for p in payloads:
                if not isinstance(p, dict):
                    raise ValueError("Each item in bulk array must be a JSON object")
                item = self._prepare_new_item(working_rows, p, seen_ids)
                working_rows.append(item)
                created_items.append(copy.deepcopy(item))
            self._data[collection] = working_rows
            self._persist()
            return created_items

    def replace(self, collection: str, item_id: str | int, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            raise ValueError("Record payload must be a JSON object")
        validate_json_values(payload)
        with self._lock:
            rows = self._data.get(collection)
            if rows is None:
                raise KeyError(collection)
            for idx, r in enumerate(rows):
                if str(r.get("id")) == str(item_id):
                    orig_id = r.get("id")
                    new_item = {"id": orig_id, **copy.deepcopy(payload)}
                    new_item["id"] = orig_id
                    rows[idx] = new_item
                    self._persist()
                    return copy.deepcopy(new_item)
            return None

    def update(self, collection: str, item_id: str | int, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            raise ValueError("Record payload must be a JSON object")
        validate_json_values(payload)
        with self._lock:
            rows = self._data.get(collection)
            if rows is None:
                raise KeyError(collection)
            for r in rows:
                if str(r.get("id")) == str(item_id):
                    orig_id = r.get("id")
                    r.update(copy.deepcopy(payload))
                    r["id"] = orig_id
                    r["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    self._persist()
                    return copy.deepcopy(r)
            return None

    def delete(self, collection: str, item_id: str | int) -> bool:
        with self._lock:
            rows = self._data.get(collection)
            if rows is None:
                raise KeyError(collection)
            for idx, r in enumerate(rows):
                if str(r.get("id")) == str(item_id):
                    rows.pop(idx)
                    self._persist()
                    return True
            return False

    def generate_typescript(self, collection: str, base_url: str = "http://127.0.0.1:5000") -> dict[str, str]:
        """Generate TypeScript interface, typed Fetch SDK, and cURL snippets for a collection."""
        with self._lock:
            rows = self._data.get(collection)
            if rows is None:
                raise KeyError(collection)
            type_name = _pascal_case(collection)
            plural_parts = re.split(r"[-_]+", _pluralize(_singularize(collection)))
            plural_fn_name = "".join(p.capitalize() for p in plural_parts if p) or f"{type_name}s"
            if plural_fn_name == type_name:
                plural_fn_name = f"{type_name}List"

            field_types: dict[str, set[str]] = {}
            field_counts: dict[str, int] = {}
            total_rows = len(rows)

            for r in rows[:50]:
                if not isinstance(r, dict):
                    continue
                for k, v in r.items():
                    field_counts[k] = field_counts.get(k, 0) + 1
                    if v is None:
                        t = "null"
                    elif isinstance(v, bool):
                        t = "boolean"
                    elif isinstance(v, (int, float)):
                        t = "number"
                    elif isinstance(v, list):
                        elem_t = "string"
                        if v and isinstance(v[0], (int, float)) and not isinstance(v[0], bool):
                            elem_t = "number"
                        t = f"{elem_t}[]"
                    elif isinstance(v, dict):
                        t = "Record<string, unknown>"
                    else:
                        t = "string"
                    field_types.setdefault(k, set()).add(t)

            if not field_types:
                field_types = {"id": {"number"}, "createdAt": {"string"}}
                field_counts = {"id": 1, "createdAt": 1}
                total_rows = 1

            ts_lines = [f"export interface {type_name} {{"]
            for k, tset in field_types.items():
                opt = "?" if field_counts.get(k, 0) < total_rows else ""
                union_t = " | ".join(sorted(tset))
                prop_key = k if JS_IDENT_RE.match(k) else json.dumps(k)
                ts_lines.append(f"  {prop_key}{opt}: {union_t};")
            ts_lines.append("}")
            interface_code = "\n".join(ts_lines)

            sdk_code = f"""{interface_code}

const API_BASE = "{base_url}/api/{collection}";

export async function list{plural_fn_name}(params?: Record<string, string | number>): Promise<{type_name}[]> {{
  const qs = params ? "?" + new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)])) : "";
  const res = await fetch(`${{API_BASE}}${{qs}}`);
  if (!res.ok) throw new Error(`HTTP ${{res.status}}: ${{await res.text()}}`);
  return res.json();
}}

export async function get{type_name}ById(id: number | string): Promise<{type_name}> {{
  const res = await fetch(`${{API_BASE}}/${{id}}`);
  if (!res.ok) throw new Error(`HTTP ${{res.status}}: ${{await res.text()}}`);
  return res.json();
}}

export async function create{type_name}(payload: Omit<{type_name}, "id" | "createdAt">): Promise<{type_name}> {{
  const res = await fetch(API_BASE, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify(payload),
  }});
  if (!res.ok) throw new Error(`HTTP ${{res.status}}: ${{await res.text()}}`);
  return res.json();
}}

export async function delete{type_name}(id: number | string): Promise<void> {{
  const res = await fetch(`${{API_BASE}}/${{id}}`, {{ method: "DELETE" }});
  if (!res.ok) throw new Error(`HTTP ${{res.status}}: ${{await res.text()}}`);
}}"""

            sample_obj = copy.deepcopy(rows[0]) if rows else {"name": "Sample"}
            sample_obj.pop("id", None)
            sample_obj.pop("createdAt", None)
            sample_obj.pop("updatedAt", None)
            sample_json = json.dumps(sample_obj, ensure_ascii=False)

            curl_code = f"""# List with filtering, sorting & pagination
curl -s "{base_url}/api/{collection}?_limit=5&_sort=id&_order=desc"

# Get single record by ID
curl -s "{base_url}/api/{collection}/1"

# Create a new record
curl -s -X POST "{base_url}/api/{collection}" \\
  -H "Content-Type: application/json" \\
  -d '{sample_json}'

# Collection numeric stats
curl -s "{base_url}/api/{collection}/_stats" """

            return {
                "collection": collection,
                "type_name": type_name,
                "interface": interface_code,
                "typescript_sdk": sdk_code,
                "curl": curl_code,
            }

    def generate_openapi(self, base_url: str = "http://127.0.0.1:5000") -> dict[str, Any]:
        """Auto-generate an OpenAPI 3.0.3 specification from current collections."""
        with self._lock:
            paths: dict[str, Any] = {}
            schemas: dict[str, Any] = {}

            for col, rows in self._data.items():
                singular = _pascal_case(col)
                props: dict[str, Any] = {}
                sample = rows[0] if rows and isinstance(rows[0], dict) else {"id": 1, "name": "example"}
                for k, v in sample.items():
                    if isinstance(v, bool):
                        props[k] = {"type": "boolean", "example": v}
                    elif isinstance(v, int):
                        props[k] = {"type": "integer", "example": v}
                    elif isinstance(v, float):
                        props[k] = {"type": "number", "example": v}
                    elif isinstance(v, list):
                        props[k] = {"type": "array", "items": {"type": "string"}, "example": v}
                    elif isinstance(v, dict):
                        props[k] = {"type": "object", "example": v}
                    else:
                        props[k] = {"type": "string", "example": str(v)}

                schemas[singular] = {"type": "object", "properties": props}
                col_ref = f"#/components/schemas/{singular}"

                paths[f"/api/{col}"] = {
                    "get": {
                        "summary": f"List {col}",
                        "tags": [col],
                        "parameters": [
                            {"name": "q", "in": "query", "schema": {"type": "string"}, "description": "Full-text search"},
                            {"name": "_sort", "in": "query", "schema": {"type": "string"}, "description": "Field to sort by"},
                            {"name": "_order", "in": "query", "schema": {"type": "string", "enum": ["asc", "desc"]}},
                            {"name": "_page", "in": "query", "schema": {"type": "integer"}},
                            {"name": "_limit", "in": "query", "schema": {"type": "integer"}},
                        ],
                        "responses": {
                            "200": {
                                "description": f"Array of {col}",
                                "content": {"application/json": {"schema": {"type": "array", "items": {"$ref": col_ref}}}},
                            }
                        },
                    },
                    "post": {
                        "summary": f"Create {singular}",
                        "tags": [col],
                        "requestBody": {
                            "required": True,
                            "content": {"application/json": {"schema": {"$ref": col_ref}}},
                        },
                        "responses": {"201": {"description": "Created"}},
                    },
                }

                paths[f"/api/{col}/{{id}}"] = {
                    "get": {
                        "summary": f"Get {singular} by ID",
                        "tags": [col],
                        "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                        "responses": {
                            "200": {"description": "Found", "content": {"application/json": {"schema": {"$ref": col_ref}}}},
                            "404": {"description": "Not Found"},
                        },
                    },
                    "put": {
                        "summary": f"Replace {singular}",
                        "tags": [col],
                        "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "Updated"}},
                    },
                    "patch": {
                        "summary": f"Update {singular}",
                        "tags": [col],
                        "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "Patched"}},
                    },
                    "delete": {
                        "summary": f"Delete {singular}",
                        "tags": [col],
                        "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                        "responses": {"200": {"description": "Deleted"}},
                    },
                }

            return {
                "openapi": "3.0.3",
                "info": {
                    "title": "QuickMock Dynamic REST API",
                    "version": "1.0.0",
                    "description": "Auto-generated OpenAPI 3.0 specification from QuickMock active collections.",
                },
                "servers": [{"url": base_url}],
                "paths": paths,
                "components": {"schemas": schemas},
            }
