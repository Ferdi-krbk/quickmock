"""Command-Line Interface for QuickMock."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import uvicorn

from . import __version__, seeder
from .server import ChaosConfig, create_app
from .store import DataStore


def load_or_init_store(
    source_path: str | None,
    preset: str,
    schema: str | None,
    count: int,
    lang: str,
    read_only: bool,
) -> DataStore:
    if schema:
        initial = seeder.generate_from_schema(schema, count=count, lang=lang)
    else:
        initial = seeder.generate_preset(preset, count=count, lang=lang)

    if not source_path:
        return DataStore(initial_data=initial, file_path=None, read_only=read_only)

    path = Path(source_path).resolve()
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not all(isinstance(v, list) for v in raw.values()):
            raise ValueError(f"{path} must be a JSON object mapping collection names to arrays of objects.")
        return DataStore(initial_data=raw, file_path=path, read_only=read_only)

    # File does not exist yet -> initialize it with generated preset/schema
    return DataStore(initial_data=initial, file_path=path, read_only=read_only)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="quickmock",
        description="Zero-config instant mock REST API server with realistic data seeding and chaos simulation.",
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="Optional path to a JSON database file (e.g. db.json). Created automatically if missing.",
    )
    parser.add_argument("--version", action="version", version=f"quickmock {__version__}")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("-p", "--port", type=int, default=5000, help="Bind port (default: 5000)")
    parser.add_argument(
        "--preset",
        choices=["ecommerce", "saas", "blog", "crm"],
        default="ecommerce",
        help="Built-in relational dataset preset (default: ecommerce)",
    )
    parser.add_argument(
        "--schema",
        help="Custom inline schema, e.g. 'cars: brand, price:float, year:int; dealers: name, city'",
    )
    parser.add_argument("-c", "--count", type=int, default=12, help="Number of records per collection (default: 12)")
    parser.add_argument("--lang", choices=["tr", "en"], default="tr", help="Seed data locale (default: tr)")
    parser.add_argument("--delay", type=int, default=0, metavar="MS", help="Simulated network latency in ms")
    parser.add_argument("--jitter", type=int, default=0, metavar="MS", help="Random latency jitter in ms")
    parser.add_argument(
        "--error-rate",
        type=float,
        default=0.0,
        metavar="PCT",
        help="Simulated HTTP failure rate percentage (0-100)",
    )
    parser.add_argument("--error-status", type=int, default=500, help="HTTP status code for chaos faults (default: 500)")
    parser.add_argument("--auth", action="store_true", help="Require Bearer token on all /api/* routes")
    parser.add_argument("--read-only", action="store_true", help="Block POST/PUT/PATCH/DELETE mutations")
    parser.add_argument("--export-openapi", metavar="OUT.json", help="Export generated OpenAPI 3.0 JSON and exit")

    args = parser.parse_args(argv)

    if not (1 <= args.port <= 65535):
        print(f"error: --port must be between 1 and 65535, got {args.port}", file=sys.stderr)
        return 2
    if not (1 <= args.count <= 1000):
        print(f"error: --count must be between 1 and 1000, got {args.count}", file=sys.stderr)
        return 2
    if not (400 <= args.error_status <= 599):
        print(f"error: --error-status must be between 400 and 599, got {args.error_status}", file=sys.stderr)
        return 2

    try:
        store = load_or_init_store(
            source_path=args.file,
            preset=args.preset,
            schema=args.schema,
            count=args.count,
            lang=args.lang,
            read_only=args.read_only,
        )
    except Exception as ex:
        print(f"error loading store: {ex}", file=sys.stderr)
        return 2

    base_url = f"http://{args.host}:{args.port}"
    if args.export_openapi:
        out_path = Path(args.export_openapi)
        spec = store.generate_openapi(base_url=base_url)
        out_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"OpenAPI 3.0 specification written to {out_path}")
        return 0

    chaos = ChaosConfig(
        delay_ms=max(0, min(10000, args.delay)),
        jitter_ms=max(0, min(5000, args.jitter)),
        error_rate=max(0.0, min(100.0, args.error_rate)),
        error_status=args.error_status,
        require_auth=args.auth,
        read_only=args.read_only,
    )

    app = create_app(store=store, chaos=chaos)

    cols = store.collections_summary()
    print("=" * 64)
    print(f"  QuickMock v{__version__} // Instant REST API & Chaos Server")
    print("=" * 64)
    print(f"  Dashboard & Inspector : {base_url}/")
    print(f"  Swagger UI            : {base_url}/swagger")
    print(f"  OpenAPI 3.0 Spec      : {base_url}/_admin/openapi.json")
    print(f"  Storage Mode          : {store.file_path if store.file_path else 'In-Memory'}")
    print("-" * 64)
    for c in cols:
        print(f"  [CRUD] {base_url}/api/{c['name']:<16} ({c['count']} items)")
    print("=" * 64)

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
