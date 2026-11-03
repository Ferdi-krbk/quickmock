# QuickMock

[![CI](https://github.com/Ferdi-krbk/quickmock/actions/workflows/ci.yml/badge.svg)](https://github.com/Ferdi-krbk/quickmock/actions/workflows/ci.yml)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776ab.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/release-v1.0.0-2563eb.svg)](https://github.com/Ferdi-krbk/quickmock/releases)

**Zero-Config Instant Mock REST API Server, Network Chaos Simulator, TypeScript SDK Generator & Local API Studio.**

When building a frontend application (React, Next.js, Vue, Svelte, Flutter) or running E2E resilience tests, waiting on backend endpoints—or manually wiring loading skeletons, retry boundaries, and `503` / `429` error states—slows development down.

**QuickMock** turns a single CLI command (or any JSON file) into a production-like REST API with **relational joins (`_expand` / `_embed`)**, **realistic multi-locale data seeding (`tr` / `en`)**, **real-time network fault injection**, **auto-inferred TypeScript Fetch SDKs**, and a **desktop-grade Local API Studio**.

---

## Why QuickMock?

| Capability | `json-server` | Postman Mock | **QuickMock (`v1.0.0`)** |
| :--- | :---: | :---: | :---: |
| Zero-config CRUD from `.json` file | Yes | No | **Yes (atomic disk persistence)** |
| Built-in relational data presets (`ecommerce`, `saas`, `blog`) | No | No | **Yes (`TR` & `EN` locales)** |
| Inline CLI schema generator (`--schema "cars: brand, price:float"`) | No | No | **Yes** |
| Relational joins (`?_expand=user` & `?_embed=orders`) | Legacy only | No | **Yes + filtering on joined fields** |
| Network Chaos Engine (Latency, Jitter, `%` HTTP `503`/`429` faults) | No | Static status | **Yes (live sliders + 1-click presets)** |
| Real HMAC-SHA256 JWT Auth simulation (`/api/auth/login`, `/me`) | No | No | **Yes** |
| Auto-inferred **TypeScript Interface + Fetch SDK** codegen | No | No | **Yes (`/_admin/codegen/{col}`)** |
| Auto-inferred **OpenAPI 3.0.3** specification & Swagger UI | No | Paid / Cloud | **Yes (`/_admin/openapi.json` & `/swagger`)** |
| Built-in Local API Studio (`/`) + Demo Storefront (`/demo`) | No | Cloud UI | **Yes (100% local, zero cloud account)** |

---

## Installation

QuickMock is a **local development tool**. Mock login accepts arbitrary credentials
and uses a public development signing key; it is not production authentication.
Keep the default loopback binding and use generated test data. Studio admin
endpoints require same-origin browser access; cross-origin frontends can still
use `/api/*`. Read-only mode guards mock API writes; Studio controls can reset
or seed data deliberately.

Recent fixes are listed in the [changelog](CHANGELOG.md). The generated TypeScript
SDK infers types from up to 50 records; cURL snippets target POSIX shells.

```bash
git clone https://github.com/Ferdi-krbk/quickmock.git
cd quickmock
pip install -e ".[dev]"
```

---

## Quick Start

### 1. Launch with a Built-in Relational Dataset (`ecommerce`, `saas`, `blog`)
```bash
# Creates db.json (if missing) with relational users, products, and orders
quickmock db.json --preset ecommerce --lang tr --port 5000
```
- **Local API Studio:** `http://127.0.0.1:5000/`
- **Interactive Demo Storefront:** `http://127.0.0.1:5000/demo`
- **Swagger UI:** `http://127.0.0.1:5000/swagger`
- **OpenAPI 3.0 JSON:** `http://127.0.0.1:5000/_admin/openapi.json`

### 2. Generate Custom Collections from an Inline Schema
No JSON file needed—define collections and field types directly on the command line:
```bash
quickmock --schema "cars: brand, model, price:float, year:int; dealers: name, city, active:bool" --count 20
```

### 3. Start with Network Chaos & Auth Guard Enabled
Test how your frontend handles slow 3G connections, 25% random `503 Service Unavailable` drops, or `401` token expiration:
```bash
quickmock db.json --delay 400 --jitter 150 --error-rate 25 --error-status 503 --auth
```

### 4. Export OpenAPI 3.0.3 Specification to Disk (CI/CD Mode)
```bash
quickmock db.json --export-openapi openapi.json
```

---

## REST API & Query Engine Cheat Sheet

Every collection automatically exposes full CRUD, bulk insertion, and numeric aggregation endpoints:

### Standard CRUD, Bulk & Aggregations
| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/api/{collection}` | List records (supports filtering, search, sorting, pagination, joins, `X-Total-Count` header) |
| `GET` | `/api/{collection}/{id}` | Get single record by ID (supports `?_expand=` and `?_embed=`) |
| `POST` | `/api/{collection}` | Create record (auto-assigns sequential `id` & `createdAt` if omitted) |
| `POST` | `/api/{collection}/_bulk` | **Atomic bulk insert** of 1 to 500 records in a single transaction |
| `PUT` | `/api/{collection}/{id}` | Replace record by ID (preserves original `id`) |
| `PATCH` | `/api/{collection}/{id}` | Partially update record by ID (auto-updates `updatedAt`) |
| `DELETE` | `/api/{collection}/{id}` | Delete record by ID |
| `GET` | `/api/{collection}/_stats` | Compute `min`, `max`, `avg`, `sum`, and `count` across all numeric fields |

### Filtering, Searching, Sorting & Relational Joins
```bash
# Exact match & boolean coercion
curl "http://127.0.0.1:5000/api/users?role=customer&active=true"

# Multi-value OR filter (fetch multiple IDs or roles in one call)
curl "http://127.0.0.1:5000/api/products?id=1&id=4&id=9"

# Numeric range operators (_gte, _lte, _gt, _lt)
curl "http://127.0.0.1:5000/api/products?price_gte=1000&price_lte=5000"

# Inequality (_ne) & case-insensitive substring match (_like)
curl "http://127.0.0.1:5000/api/products?category_ne=Ofis&title_like=monitör"

# Full-text search across all fields (?q=)
curl "http://127.0.0.1:5000/api/products?q=kablosuz"

# Type-safe sorting with NULLS LAST + Pagination
curl "http://127.0.0.1:5000/api/products?_sort=price&_order=desc&_page=1&_limit=10"

# Foreign-key parent join (_expand) & filtering on joined fields
curl "http://127.0.0.1:5000/api/orders?_expand=user&_expand=product&user.role=admin"

# Child collection embedding (_embed)
curl "http://127.0.0.1:5000/api/users/1?_embed=orders"
```

---

## Auto-Inferred TypeScript SDK Generator

Query `GET /_admin/codegen/{collection}` (or open the **TypeScript & cURL** tab in the studio) to generate ready-to-paste TypeScript interfaces and typed `fetch` helpers inferred directly from your live collection data:

```typescript
export interface Product {
  id: number;
  sku: string;
  title: string;
  slug: string;
  category: string;
  price: number;
  currency: string;
  stock: number;
  inStock: boolean;
  rating: number;
  createdAt: string;
}

const API_BASE = "http://127.0.0.1:5000/api/products";

export async function listProducts(params?: Record<string, string | number>): Promise<Product[]> {
  const qs = params ? "?" + new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)])) : "";
  const res = await fetch(`${API_BASE}${qs}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  return res.json();
}
```

---

## Network Chaos Profiles & HMAC-JWT Auth Simulation

Switch network conditions at runtime from the Studio UI or via `POST /_admin/chaos-preset`:

| Preset Name | Latency (`delay_ms`) | Jitter (`jitter_ms`) | Failure Rate (`error_rate`) | Status Code |
| :--- | :---: | :---: | :---: | :---: |
| `clean` | `0 ms` | `0 ms` | `0%` | — (resets guards) |
| `slow_3g` | `850 ms` | `+0–250 ms` | `0%` | — |
| `flaky` | `120 ms` | `+0–60 ms` | `25%` | `503 Service Unavailable` |
| `rate_limit` | `20 ms` | `0 ms` | `100%` | `429 Too Many Requests` |
| `outage` | `40 ms` | `0 ms` | `100%` | `503 Service Unavailable` |

- **CORS-Safe Fault Injection:** Mock API `OPTIONS` preflight requests bypass Chaos faults and Auth guards. CORS headers are also attached to error responses (`401`, `403`, `429`, `503`), so browser `fetch()` / `axios` clients can inspect the HTTP status. The allowed origin is either `*` or the requesting origin, depending on credential handling.

---

## CLI Reference

```text
usage: quickmock [-h] [--version] [--host HOST] [-p PORT]
                 [--preset {ecommerce,saas,blog,crm}] [--schema SCHEMA]
                 [-c COUNT] [--lang {tr,en}] [--delay MS] [--jitter MS]
                 [--error-rate PCT] [--error-status ERROR_STATUS] [--auth]
                 [--read-only] [--export-openapi OUT.json]
                 [file]
```

---

## Running Tests

```bash
pytest -v
```

---

## License

MIT © [Ferdi Karabacak](https://github.com/Ferdi-krbk)
