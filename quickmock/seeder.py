"""Smart Schema-Aware Realistic Fake Data Generator (EN & TR support, zero external bloat)."""
from __future__ import annotations

import hashlib
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

POOLS = {
    "tr": {
        "first_names": [
            "Ahmet", "Mehmet", "Zeynep", "Elif", "Can", "Burak", "Selin", "Deniz",
            "Emre", "Defne", "Mert", "Ece", "Kerem", "Ayşe", "Kaan", "Buse",
        ],
        "last_names": [
            "Yılmaz", "Kaya", "Demir", "Çelik", "Şahin", "Yıldız", "Öztürk", "Aydın",
            "Arslan", "Doğan", "Kılıç", "Aslan", "Çetin", "Koç", "Kurt", "Özkan",
        ],
        "cities": [
            "İstanbul", "Ankara", "İzmir", "Bursa", "Antalya", "Eskişehir", "Muğla", "Kocaeli",
        ],
        "products": [
            ("Mekanik Oyuncu Klavyesi RGB", "Elektronik", 2450.0),
            ("Kablosuz Gürültü Önleyici Kulaklık", "Ses & Müzik", 5890.0),
            ("27 inç 165Hz IPS Monitör", "Bilgisayar", 7400.0),
            ("Ergonomik Çalışma Koltuğu", "Ofis", 6200.0),
            ("USB-C 7-in-1 Çoklayıcı Hub", "Aksesuar", 890.0),
            ("Taşınabilir NVMe SSD 1TB", "Depolama", 3150.0),
            ("Akıllı Masa Lambası Wi-Fi", "Ev & Yaşam", 1120.0),
            ("4K Web Kamerası Oto-Odak", "Elektronik", 2790.0),
            ("Dikey Ergonomik Mouse", "Aksesuar", 1350.0),
            ("Alüminyum Laptop Standı", "Ofis", 680.0),
            ("34 inç Ultrawide Curved Monitör", "Bilgisayar", 14900.0),
            ("Stüdyo Kondenser Mikrofon", "Ses & Müzik", 3490.0),
        ],
        "companies": [
            "Nova Teknoloji A.Ş.", "Kuzey Yazılım", "Atlas Lojistik", "Piksel Medya",
            "Vektör Finans", "Bulut Bilişim Ltd.", "Meridyen Enerji", "Kare Tasarım",
        ],
        "blog_titles": [
            "Modern Web Mimarisinde Mikro-Önyüz Yaklaşımları",
            "PostgreSQL'de Yüksek Trafik Altında İndeks Optimizasyonu",
            "Sıfır Güven (Zero-Trust) Ağ Güvenliği Nasıl Kurulur?",
            "Dağıtık Sistemlerde Idempotency ve Retry Stratejileri",
            "Python 3.13 ile Gelen Performans İyileştirmeleri",
            "Frontend Test Süreçlerinde Kaos Mühendisliği",
            "Docker İmaj Boyutlarını %80 Küçültme Rehberi",
            "REST API Tasarımında Sık Yapılan 10 Hata",
        ],
        "task_titles": [
            "Ödeme sayfası hata yönetimi testleri",
            "JWT yenileme (refresh token) akışının düzeltilmesi",
            "Koyu tema renk kontrastlarının güncellenmesi",
            "Mobil menü animasyon gecikmesinin giderilmesi",
            "Veritabanı bağlantı havuzu sızıntı analizi",
            "CI/CD pipeline önbellek hızlandırması",
            "Kullanıcı profil fotoğrafı yükleme sınırı",
            "Arama çubuğu debounce optimizasyonu",
        ],
    },
    "en": {
        "first_names": [
            "Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Quinn",
            "Liam", "Emma", "Noah", "Olivia", "Ethan", "Sophia", "Mason", "Ava",
        ],
        "last_names": [
            "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
            "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas",
        ],
        "cities": [
            "San Francisco", "New York", "Austin", "Seattle", "London", "Berlin", "Toronto", "Amsterdam",
        ],
        "products": [
            ("Mechanical Gaming Keyboard RGB", "Electronics", 129.99),
            ("Wireless ANC Headphones", "Audio", 249.50),
            ("27-inch 165Hz IPS Monitor", "Displays", 319.00),
            ("Ergonomic Mesh Office Chair", "Furniture", 389.00),
            ("USB-C 7-in-1 Multiport Hub", "Accessories", 49.90),
            ("Portable NVMe SSD 1TB", "Storage", 109.99),
            ("Smart Wi-Fi Desk Lamp", "Home Office", 59.00),
            ("4K Auto-Focus Webcam", "Electronics", 139.00),
            ("Vertical Ergonomic Wireless Mouse", "Accessories", 69.99),
            ("Aluminum Laptop Riser Stand", "Furniture", 39.50),
            ("34-inch Ultrawide Curved Monitor", "Displays", 649.00),
            ("Studio Condenser USB Microphone", "Audio", 149.00),
        ],
        "companies": [
            "Apex Systems Inc.", "Vertex Cloud", "Northstar Labs", "Pulse Analytics",
            "Cobalt Security", "Hyperion Data", "Linear Wave", "Vanguard Tech",
        ],
        "blog_titles": [
            "Scaling PostgreSQL Under High Write Contention",
            "Zero-Downtime Database Migrations in Production",
            "Building Resilient Frontend Apps with Network Chaos",
            "Why Supply-Chain Auditing Matters for Bundled Dependencies",
            "Understanding Idempotency Keys in Payment APIs",
            "Reducing Docker Image Bloat with Multi-Stage Builds",
            "Designing Predictable Rate Limiters with Token Buckets",
            "Modern State Management Patterns in 2026",
        ],
        "task_titles": [
            "Fix race condition in token refresh interceptor",
            "Add skeleton loading states to analytics dashboard",
            "Implement exponential backoff for failed webhook retries",
            "Audit third-party dependencies in release artifact",
            "Optimize SQL query for invoice export endpoint",
            "Add dark mode high-contrast border tokens",
            "Configure rate limiting on public auth endpoints",
            "Write E2E tests for checkout error boundaries",
        ],
    },
}


def _slugify(text: str) -> str:
    tr_map = str.maketrans("çğıöşüÇĞİÖŞÜ ", "cgiosuCGIOSU-")
    cleaned = text.translate(tr_map).lower()
    return "".join(c for c in cleaned if c.isalnum() or c == "-").strip("-")


def _iso_past(rng: random.Random, max_days: int = 90) -> str:
    dt = datetime.now(timezone.utc) - timedelta(
        days=rng.randint(0, max_days),
        hours=rng.randint(0, 23),
        minutes=rng.randint(0, 59),
    )
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_preset(preset: str = "ecommerce", count: int = 12, lang: str = "tr", seed: int | None = 42) -> dict[str, list[dict[str, Any]]]:
    """Generate a rich relational multi-collection dataset for a given preset."""
    rng = random.Random(seed)
    pool = POOLS.get(lang, POOLS["tr"])
    preset = preset.lower().strip()

    users: list[dict[str, Any]] = []
    user_count = max(6, count // 2)
    roles = ["admin", "editor", "customer", "customer", "customer"]
    for i in range(1, user_count + 1):
        fn = rng.choice(pool["first_names"])
        ln = rng.choice(pool["last_names"])
        full = f"{fn} {ln}"
        username = _slugify(f"{fn}.{ln}{i}")
        avatar_hash = hashlib.md5(username.encode()).hexdigest()[:10]
        users.append({
            "id": i,
            "name": full,
            "username": username,
            "email": f"{username}@example.com",
            "role": roles[(i - 1) % len(roles)],
            "city": rng.choice(pool["cities"]),
            "active": rng.random() > 0.15,
            "avatar": f"https://api.dicebear.com/7.x/identicon/svg?seed={avatar_hash}",
            "createdAt": _iso_past(rng, 180),
        })

    if preset == "ecommerce":
        products: list[dict[str, Any]] = []
        prod_templates = pool["products"]
        for i in range(1, count + 1):
            base_title, cat, base_price = prod_templates[(i - 1) % len(prod_templates)]
            title = base_title if i <= len(prod_templates) else f"{base_title} Pro v{i}"
            price = round(base_price * rng.uniform(0.9, 1.15), 2)
            stock = rng.randint(0, 120)
            products.append({
                "id": i,
                "sku": f"PRD-{1000 + i}",
                "title": title,
                "slug": _slugify(title),
                "category": cat,
                "price": price,
                "currency": "TRY" if lang == "tr" else "USD",
                "stock": stock,
                "inStock": stock > 0,
                "rating": round(rng.uniform(3.8, 5.0), 1),
                "createdAt": _iso_past(rng, 120),
            })

        orders: list[dict[str, Any]] = []
        statuses = ["pending", "processing", "shipped", "delivered", "cancelled"]
        for i in range(1, count + 1):
            u = rng.choice(users)
            p = rng.choice(products)
            qty = rng.randint(1, 3)
            orders.append({
                "id": 100 + i,
                "orderNumber": f"ORD-{202600 + i}",
                "user_id": u["id"],
                "product_id": p["id"],
                "customerName": u["name"],
                "quantity": qty,
                "totalAmount": round(p["price"] * qty, 2),
                "status": rng.choice(statuses),
                "createdAt": _iso_past(rng, 30),
            })
        return {"users": users, "products": products, "orders": orders}

    if preset == "blog":
        posts: list[dict[str, Any]] = []
        titles = pool["blog_titles"]
        tags_pool = ["architecture", "backend", "security", "devops", "frontend", "database", "performance"]
        for i in range(1, count + 1):
            u = rng.choice(users)
            title = titles[(i - 1) % len(titles)]
            if i > len(titles):
                title = f"{title} ({i})"
            posts.append({
                "id": i,
                "user_id": u["id"],
                "author": u["name"],
                "title": title,
                "slug": _slugify(title),
                "excerpt": f"{title} — detailed guide and production benchmarks.",
                "tags": rng.sample(tags_pool, k=2),
                "views": rng.randint(120, 18500),
                "published": rng.random() > 0.1,
                "createdAt": _iso_past(rng, 90),
            })

        comments: list[dict[str, Any]] = []
        for i in range(1, count * 2 + 1):
            u = rng.choice(users)
            post = rng.choice(posts)
            comments.append({
                "id": i,
                "post_id": post["id"],
                "user_id": u["id"],
                "author": u["name"],
                "body": "Harika analiz, üretim ortamında çok işimize yaradı!" if lang == "tr"
                        else "Solid breakdown, tested this in staging and it worked great.",
                "createdAt": _iso_past(rng, 30),
            })
        return {"users": users, "posts": posts, "comments": comments}

    if preset in ("saas", "crm", "tasks"):
        tasks: list[dict[str, Any]] = []
        t_titles = pool["task_titles"]
        priorities = ["low", "medium", "high", "urgent"]
        statuses = ["backlog", "in_progress", "review", "done"]
        for i in range(1, count + 1):
            u = rng.choice(users)
            title = t_titles[(i - 1) % len(t_titles)]
            if i > len(t_titles):
                title = f"{title} #{i}"
            tasks.append({
                "id": i,
                "key": f"ENG-{100 + i}",
                "title": title,
                "user_id": u["id"],
                "assignee": u["name"],
                "priority": rng.choice(priorities),
                "status": rng.choice(statuses),
                "estimateHours": rng.choice([1, 2, 4, 8, 13]),
                "createdAt": _iso_past(rng, 45),
            })
        orgs = []
        for i, comp in enumerate(pool["companies"][:max(4, count // 2)], start=1):
            orgs.append({
                "id": i,
                "name": comp,
                "plan": rng.choice(["starter", "pro", "enterprise"]),
                "seats": rng.randint(5, 150),
                "mrr": rng.choice([49, 199, 499, 1299]),
                "status": "active" if rng.random() > 0.1 else "past_due",
                "createdAt": _iso_past(rng, 200),
            })
        return {"users": users, "organizations": orgs, "tasks": tasks}

    # Fallback: treat preset string as custom schema spec only if it contains ":"
    if ":" in preset:
        return generate_from_schema(preset, count=count, lang=lang, seed=seed)
    raise ValueError(f"Unknown preset '{preset}'. Valid presets: ecommerce, saas, crm, tasks, blog")


def infer_field_value(field_name: str, field_type: str | None, idx: int, rng: random.Random, lang: str = "tr") -> Any:
    """Infer a realistic mock value based on field name or explicit type hint."""
    pool = POOLS.get(lang, POOLS["tr"])
    fname = field_name.lower().strip()
    ftype = (field_type or "").lower().strip()

    if fname == "id":
        return idx
    if ftype == "uuid" or fname.endswith("uuid"):
        return str(uuid.UUID(int=rng.getrandbits(128), version=4))
    if ftype in ("bool", "boolean") or fname.startswith(("is_", "has_", "active", "enabled", "instock", "published")):
        return rng.random() > 0.2
    if ftype == "email" or "email" in fname or "mail" in fname:
        fn = _slugify(rng.choice(pool["first_names"]))
        return f"{fn}{idx}@example.com"
    if "name" in fname or "isim" in fname or "ad" == fname or "author" in fname:
        return f"{rng.choice(pool['first_names'])} {rng.choice(pool['last_names'])}"
    if "title" in fname or "baslik" in fname or "urun" in fname:
        return pool["products"][(idx - 1) % len(pool["products"])][0]
    if "category" in fname or "kategori" in fname:
        return pool["products"][(idx - 1) % len(pool["products"])][1]
    if "city" in fname or "sehir" in fname:
        return rng.choice(pool["cities"])
    if "company" in fname or "sirket" in fname:
        return rng.choice(pool["companies"])
    if "price" in fname or "fiyat" in fname or "amount" in fname or "tutar" in fname or ftype == "float":
        return round(rng.uniform(25.0, 4500.0), 2)
    if "stock" in fname or "stok" in fname or "count" in fname or "adet" in fname or "age" in fname or ftype == "int":
        return rng.randint(1, 200)
    if "status" in fname or "durum" in fname:
        return rng.choice(["active", "pending", "completed", "archived"])
    if "role" in fname or "rol" in fname:
        return rng.choice(["admin", "editor", "viewer", "member"])
    if "avatar" in fname or "image" in fname or "resim" in fname or "url" in fname:
        return f"https://api.dicebear.com/7.x/shapes/svg?seed={fname}-{idx}"
    if "date" in fname or "tarih" in fname or fname.endswith(("at", "_at")):
        return _iso_past(rng, 60)
    return f"{field_name}_{idx}"


def generate_from_schema(schema_spec: str, count: int = 10, lang: str = "tr", seed: int | None = None) -> dict[str, list[dict[str, Any]]]:
    """Parse compact schema string like:
    'products: title, price:float, stock:int; users: name, email, role'
    and generate collections.
    """
    from .store import validate_collection_name

    rng = random.Random(seed)
    result: dict[str, list[dict[str, Any]]] = {}
    sections = [s.strip() for s in schema_spec.split(";") if s.strip()]
    if not sections:
        raise ValueError("Schema specification cannot be empty")
    for sec in sections:
        if ":" in sec:
            col_name, fields_raw = sec.split(":", 1)
            col_name = validate_collection_name(col_name.strip())
            fields = [f.strip() for f in fields_raw.split(",") if f.strip()]
        else:
            col_name = validate_collection_name(sec.strip())
            fields = ["name", "status", "createdAt"]

        rows: list[dict[str, Any]] = []
        for idx in range(1, count + 1):
            row: dict[str, Any] = {"id": idx}
            for f in fields:
                if ":" in f:
                    fn, ft = f.split(":", 1)
                else:
                    fn, ft = f, None
                fn = fn.strip()
                if fn == "id":
                    continue
                row[fn] = infer_field_value(fn, ft, idx, rng, lang=lang)
            if "createdAt" not in row:
                row["createdAt"] = _iso_past(rng, 60)
            rows.append(row)
        result[col_name] = rows
    return result
