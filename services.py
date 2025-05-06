# services.py
import sqlite3
from fuzzywuzzy import process
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
import re
from config import DB_PATH, TOL_MM, FUZZY_THRESHOLD, PLANS
from tenacity import retry, stop_after_attempt, wait_fixed
import logging

logging.basicConfig(level=logging.INFO)

_display_list_cache = None
VALID_NOTCH_TYPES = {"None", "Punch-hole", "Waterdrop", "Notch", "Full"}

def normalize_notch_type(notch_type: str) -> str:
    notch_type = notch_type.strip().title()
    return notch_type if notch_type in VALID_NOTCH_TYPES else "None"

def validate_device_dimensions(height_mm: float, width_mm: float, diagonal_in: float) -> bool:
    return (
        0 < height_mm <= 300 and
        0 < width_mm <= 200 and
        0 < diagonal_in <= 10
    )

def clear_display_list_cache():
    global _display_list_cache
    _display_list_cache = None

def _build_display_list():
    global _display_list_cache
    if _display_list_cache is None:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT brand,model,height_mm,width_mm,diagonal_in,notch_type FROM glasses"
            ).fetchall()
        _display_list_cache = [
            (f"{b} {m}".strip(), (b, m, h, w, d, nt))
            for b, m, h, w, d, nt in rows
        ]
    return _display_list_cache

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(""" 
            CREATE TABLE IF NOT EXISTS glasses (
                brand       TEXT,
                model       TEXT,
                height_mm   REAL,
                width_mm    REAL,
                diagonal_in REAL,
                notch_type  TEXT,
                PRIMARY KEY(brand, model)
            )
        """)
        conn.execute(""" 
            CREATE TABLE IF NOT EXISTS device_suggestions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                brand       TEXT,
                model       TEXT,
                height_mm   REAL,
                width_mm    REAL,
                diagonal_in REAL,
                notch_type  TEXT,
                status      TEXT DEFAULT 'pending',
                created_at  TEXT
            )
        """)
        conn.execute(""" 
            CREATE TABLE IF NOT EXISTS subscription_plans (
                plan_id     TEXT PRIMARY KEY,
                price       REAL,
                description TEXT
            )
        """)
        conn.execute(""" 
            CREATE TABLE IF NOT EXISTS payments (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER,
                plan_id             TEXT,
                screenshot_file_id  TEXT,
                status              TEXT,
                created_at          TEXT,
                FOREIGN KEY(plan_id) REFERENCES subscription_plans(plan_id)
            )
        """)
        conn.execute(""" 
            CREATE TABLE IF NOT EXISTS compatible_devices (
                device_brand      TEXT,
                device_model      TEXT,
                compatible_brand  TEXT,
                compatible_model  TEXT,
                PRIMARY KEY(device_brand, device_model, compatible_brand, compatible_model),
                FOREIGN KEY(device_brand, device_model) REFERENCES glasses(brand, model)
            )
        """)
        conn.execute(""" 
            CREATE TABLE IF NOT EXISTS user_queries (
                user_id     INTEGER,
                query_date  TEXT,
                query_count INTEGER,
                PRIMARY KEY(user_id, query_date)
            )
        """)
        conn.commit()

        existing = conn.execute("SELECT plan_id FROM subscription_plans").fetchall()
        have = {r[0] for r in existing}
        for pid, (price, desc) in PLANS.items():
            if pid not in have:
                conn.execute(
                    "INSERT INTO subscription_plans (plan_id,price,description) VALUES (?,?,?)",
                    (pid, price, desc)
                )
        conn.commit()

def get_plans():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT plan_id,price,description FROM subscription_plans"
        ).fetchall()
    return rows

def add_payment(user_id, plan_id, screenshot_file_id):
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO payments (user_id,plan_id,screenshot_file_id,status,created_at) "
            "VALUES (?,?,?,?,?)",
            (user_id, plan_id, screenshot_file_id, "pending", now)
        )
        pid = cur.lastrowid
        conn.commit()
    return pid

def get_payment(payment_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id,user_id,plan_id,screenshot_file_id,status,created_at "
            "FROM payments WHERE id=?",
            (payment_id,)
        ).fetchone()
    return row

def list_payments(status="pending"):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id,user_id,plan_id,screenshot_file_id,status,created_at "
            "FROM payments WHERE status=?",
            (status,)
        ).fetchall()
    return rows

def update_payment_status(payment_id, new_status):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE payments SET status=? WHERE id=?",
            (new_status, payment_id)
        )
        conn.commit()

def get_user_subscription_status(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT plan_id FROM payments WHERE user_id=? AND status='approved' ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        ).fetchone()
    return row[0] if row else "free"

def get_subscription_details(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT plan_id, created_at FROM payments WHERE user_id=? AND status='approved' ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        ).fetchone()
    if not row:
        current_dt = datetime.utcnow()
        return {
            "plan_id": "free",
            "created_at": current_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "valid_till": "Indefinite"
        }
    plan_id, created_at = row
    created_dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
    expiry_dt = created_dt + timedelta(days=30)
    return {
        "plan_id": plan_id,
        "created_at": created_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "valid_till": expiry_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    }

def increment_query_count(user_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT query_count FROM user_queries WHERE user_id=? AND query_date=?",
            (user_id, today)
        ).fetchone()
        if row:
            new_count = row[0] + 1
            conn.execute(
                "UPDATE user_queries SET query_count=? WHERE user_id=? AND query_date=?",
                (new_count, user_id, today)
            )
        else:
            conn.execute(
                "INSERT INTO user_queries (user_id, query_date, query_count) VALUES (?,?,?)",
                (user_id, today, 1)
            )
        conn.commit()
    return row[0] + 1 if row else 1

def check_query_limit(user_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT query_count FROM user_queries WHERE user_id=? AND query_date=?",
            (user_id, today)
        ).fetchone()
    return row[0] if row else 0

def device_exists(brand: str, model: str):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM glasses WHERE brand=? AND model=?",
            (brand, model)
        ).fetchone()
    return bool(row)

def add_glass(brand: str, model: str, h: float, w: float, diagonal_in: float, notch_type: str):
    if not validate_device_dimensions(h, w, diagonal_in):
        raise ValueError("Invalid device dimensions")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO glasses (brand,model,height_mm,width_mm,diagonal_in,notch_type) VALUES (?,?,?,?,?,?)",
            (brand, model, h, w, diagonal_in, normalize_notch_type(notch_type))
        )
        conn.commit()
    clear_display_list_cache()

def add_phone(name: str, h: float, w: float, diagonal_in: float, notch_type: str):
    parts = name.split(" ", 1)
    b, m = (parts if len(parts) == 2 else (parts[0], ""))
    add_glass(b, m, h, w, diagonal_in, notch_type)

def add_device_suggestion(user_id, brand, model, h, w, diagonal_in, notch_type):
    if not validate_device_dimensions(h, w, diagonal_in):
        raise ValueError("Invalid device dimensions")
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO device_suggestions (user_id,brand,model,height_mm,width_mm,diagonal_in,notch_type,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, brand, model, h, w, diagonal_in, normalize_notch_type(notch_type), "pending", now)
        )
        conn.commit()

def add_compatible_devices(device_brand: str, device_model: str, compatible_devices: list):
    with sqlite3.connect(DB_PATH) as conn:
        for compat_brand, compat_model in compatible_devices:
            conn.execute(
                "INSERT OR IGNORE INTO compatible_devices (device_brand,device_model,compatible_brand,compatible_model) "
                "VALUES (?,?,?,?)",
                (device_brand, device_model, compat_brand, compat_model)
            )
        conn.commit()

def get_compatible_devices(brand: str, model: str):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT compatible_brand, compatible_model FROM compatible_devices WHERE device_brand=? AND device_model=?",
            (brand, model)
        ).fetchall()
    return [(cb, cm) for cb, cm in rows]

def normalize_glass(name: str):
    cands = _build_display_list()
    names = [d[0] for d in cands]
    if not names:
        return None
    res = process.extractOne(name, names)
    if not res:
        return None
    match, score = res
    return match if score >= FUZZY_THRESHOLD else None

def normalize_brand(name: str):
    brands = {spec[0] for _, spec in _build_display_list()}
    if not brands:
        return None
    res = process.extractOne(name, list(brands))
    if not res:
        return None
    match, score = res
    return match if score >= FUZZY_THRESHOLD else None

def get_verified_dimension_bounds(brand: str, model: str):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT g.brand, g.model, g.height_mm, g.width_mm, g.diagonal_in, g.notch_type
            FROM compatible_devices cd
            JOIN glasses g ON cd.compatible_brand = g.brand AND cd.compatible_model = g.model
            WHERE cd.device_brand = ? AND cd.device_model = ?
            UNION
            SELECT brand, model, height_mm, width_mm, diagonal_in, notch_type
            FROM glasses
            WHERE brand = ? AND model = ?
            """,
            (brand, model, brand, model)
        ).fetchall()

    if not rows:
        return None, None, None

    min_height = float('inf')
    max_height = float('-inf')
    min_width = float('inf')
    max_width = float('-inf')
    min_diagonal = float('inf')
    max_diagonal = float('-inf')
    notch_types = set()
    largest_device = None
    smallest_device = None
    largest_area = float('-inf')
    smallest_area = float('inf')

    for row in rows:
        b, m, h, w, d, nt = row
        area = h * w
        notch_types.add(nt)
        min_height = min(min_height, h)
        max_height = max(max_height, h)
        min_width = min(min_width, w)
        max_width = max(max_width, w)
        min_diagonal = min(min_diagonal, d)
        max_diagonal = max(max_diagonal, d)
        if area > largest_area:
            largest_area = area
            largest_device = (b, m, h, w, d, nt)
        if area < smallest_area:
            smallest_area = area
            smallest_device = (b, m, h, w, d, nt)

    bounds = {
        'min_height': min_height,
        'max_height': max_height,
        'min_width': min_width,
        'max_width': max_width,
        'min_diagonal': min_diagonal,
        'max_diagonal': max_diagonal,
        'notch_types': notch_types
    }
    return largest_device, smallest_device, bounds

def check_compat(guard_spec, device_spec, height_tol=TOL_MM, width_tol=TOL_MM, diagonal_tol=0.1):
    _, _, gh, gw, gd, gnt = guard_spec
    _, _, dh, dw, dd, dnt = device_spec
    notch_compatible = gnt == dnt or gnt == "None" or dnt == "None"
    height_compatible = abs(dh - gh) <= height_tol and gh <= dh
    width_compatible = abs(dw - gw) <= width_tol and gw <= dw
    diagonal_compatible = abs(dd - gd) <= diagonal_tol
    return height_compatible and width_compatible and diagonal_compatible and notch_compatible

def find_compatible_glasses(name: str, height_tol=TOL_MM, width_tol=TOL_MM, diagonal_tol=0.1):
    display = normalize_glass(name)
    if not display:
        return None
    _, base = next(filter(lambda x: x[0] == display, _build_display_list()))
    base_brand, base_model, base_h, base_w, base_d, base_nt = base

    largest_device, smallest_device, bounds = get_verified_dimension_bounds(base_brand, base_model)
    verified_devices = get_compatible_devices(base_brand, base_model)

    with sqlite3.connect(DB_PATH) as conn:
        verified_rows = []
        for cb, cm in verified_devices:
            row = conn.execute(
                """
                SELECT brand, model, height_mm, width_mm, diagonal_in, notch_type
                FROM glasses WHERE brand = ? AND model = ?
                """,
                (cb, cm)
            ).fetchone()
            if row:
                b, m, h, w, d, nt = row
                guard_spec = (base_brand, base_model, base_h, base_w, base_d, base_nt)
                device_spec = (b, m, h, w, d, nt)
                if check_compat(guard_spec, device_spec, height_tol=0, width_tol=0, diagonal_tol=0):
                    verified_rows.append(row + ('Verified',))

        if not bounds:
            low_h, high_h = base_h - height_tol, base_h + height_tol
            low_w, high_w = base_w - width_tol, base_w + height_tol
            low_d, high_d = base_d - diagonal_tol, base_d + diagonal_tol
            rows = conn.execute(
                """
                SELECT brand, model, height_mm, width_mm, diagonal_in, notch_type
                FROM glasses
                WHERE height_mm BETWEEN ? AND ?
                  AND width_mm BETWEEN ? AND ?
                  AND diagonal_in BETWEEN ? AND ?
                  AND (notch_type = ? OR notch_type = 'None' OR ? = 'None')
                """,
                (low_h, high_h, low_w, high_w, low_d, high_d, base_nt, base_nt)
            ).fetchall()
            return [(r + ('Dimension-based',)) for r in rows]

        min_h = bounds['min_height'] - height_tol
        max_h = bounds['max_height'] + height_tol
        min_w = bounds['min_width'] - width_tol
        max_w = bounds['max_width'] + height_tol
        min_d = bounds['min_diagonal'] - diagonal_tol
        max_d = bounds['max_diagonal'] + diagonal_tol
        valid_notch_types = bounds['notch_types'] | {'None'}

        query = """
            SELECT brand, model, height_mm, width_mm, diagonal_in, notch_type
            FROM glasses
            WHERE height_mm BETWEEN ? AND ?
              AND width_mm BETWEEN ? AND ?
              AND diagonal_in BETWEEN ? AND ?
              AND notch_type IN ({})
        """.format(','.join('?' * len(valid_notch_types)))
        params = [min_h, max_h, min_w, max_w, min_d, max_d] + list(valid_notch_types)
        rows = conn.execute(query, params).fetchall()

        filtered_rows = []
        base_area = base_h * base_w
        for row in rows:
            b, m, h, w, d, nt = row
            area = h * w
            if area <= base_area:
                filtered_rows.append(row + ('Dimension-based',))

        combined = verified_rows + filtered_rows
        unique_results = []
        seen = set()
        for row in combined:
            key = (row[0], row[1])
            if key not in seen:
                seen.add(key)
                unique_results.append(row)

        return unique_results

def get_phone(name: str):
    display = normalize_glass(name)
    if not display:
        return None
    for disp, spec in _build_display_list():
        if disp == display:
            return spec
    return None

def list_devices_by_brand(brand_name: str):
    norm = normalize_brand(brand_name)
    if not norm:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT model,height_mm,width_mm,diagonal_in,notch_type FROM glasses WHERE brand=?",
            (norm,)
        ).fetchall()
    return [(norm, m, h, w, d, nt) for (m, h, w, d, nt) in rows]

def update_phone_dimensions(brand: str, model: str, h: float, w: float, diagonal_in: float, notch_type: str):
    if not validate_device_dimensions(h, w, diagonal_in):
        raise ValueError("Invalid device dimensions")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE glasses SET height_mm=?, width_mm=?, diagonal_in=?, notch_type=? WHERE brand=? AND model=?",
            (h, w, diagonal_in, normalize_notch_type(notch_type), brand, model)
        )
        conn.commit()
    clear_display_list_cache()

def check_batch_compatibility(device_names, height_tol=TOL_MM, width_tol=TOL_MM, diagonal_tol=0.1):
    device_cache = {}
    for name in device_names:
        if name not in device_cache:
            spec = get_phone(name) or get_phone(normalize_glass(name) or "")
            device_cache[name] = spec
    specs = [spec for spec in device_cache.values() if spec]
    if len(specs) < 2:
        return []
    results = []
    for i, s1 in enumerate(specs):
        for s2 in specs[i+1:]:
            fit = check_compat(s1, s2, height_tol, width_tol, diagonal_tol)
            results.append(((s1[0], s1[1]), (s2[0], s2[1]), fit))
    return results

def find_devices_by_dimensions(height_min: float, height_max: float, width_min: float, width_max: float, diagonal_min: float, diagonal_max: float):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT brand,model,height_mm,width_mm,diagonal_in,notch_type FROM glasses "
            "WHERE height_mm BETWEEN ? AND ? AND width_mm BETWEEN ? AND ? AND diagonal_in BETWEEN ? AND ?",
            (height_min, height_max, width_min, width_max, diagonal_min, diagonal_max)
        ).fetchall()
    return rows

def escape_markdown_v2(text):
    logging.info(f"Escaping Markdown V2 text: {text}")
    reserved_chars = r'_[]()~`>#*+-|=}{.!'
    for char in reserved_chars:
        text = text.replace(char, f'\\{char}')
    logging.info(f"Escaped Markdown V2 text: {text}")
    return text

def format_compatible_devices(devices):
    if not devices:
        return escape_markdown_v2("No compatible devices found.")
    header = escape_markdown_v2("| Brand | Model | Dimensions | Diagonal | Notch Type | Source |")
    separator = escape_markdown_v2("|-------|-------|------------|---------|------------|--------|")
    rows = [
        escape_markdown_v2(f"| {b} | {m} | {h}×{w} mm | {d} in | {nt} | {source} |")
        for b, m, h, w, d, nt, source in devices
    ]
    return f"{header}\n{separator}\n" + "\n".join(rows)

@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
def fetch_device_data_from_gsmarena(brand, model):
    try:
        query = f"{brand} {model}".replace(" ", "+")
        search_url = f"https://www.gsmarena.com/results.php3?sQuickSearch={query}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(search_url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        device_list = soup.find("div", class_="makers")
        if not device_list:
            logging.error(f"No device list found on GSMArena for {brand} {model}")
            return None

        target_name = f"{brand} {model}".lower()
        device_url = None
        for link in device_list.find_all("a"):
            device_name = link.find("span").text.lower()
            if target_name in device_name or device_name in target_name:
                device_url = "https://www.gsmarena.com" + link["href"]
                break

        if not device_url:
            logging.error(f"No matching device found on GSMArena for {brand} {model}")
            return None

        return parse_device_page(device_url, brand, model)
    except requests.RequestException as e:
        logging.error(f"Network error searching GSMArena for {brand} {model}: {e}")
        return None
    except Exception as e:
        logging.error(f"Error searching GSMArena for {brand} {model}: {e}")
        return None

def parse_device_page(url, brand, model):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        page_model = soup.find("h1", class_="specs-phone-name-title")
        if not page_model:
            logging.error(f"No model name found on page {url}")
            return None
        page_model = page_model.text.strip().replace(f"{brand} ", "", 1)

        dimensions = None
        body_section = soup.find("td", string=re.compile("Dimensions", re.I))
        if body_section:
            dimensions = body_section.find_next("td").text.strip()
            match = re.match(r"(\d+\.?\d*)\s*x\s*(\d+\.?\d*)\s*x\s*\d+\.?\d*", dimensions)
            if match:
                height_mm, width_mm = float(match.group(1)), float(match.group(2))
            else:
                logging.error(f"Invalid dimensions format on page {url}: {dimensions}")
                return None
        else:
            logging.error(f"No dimensions found on page {url}")
            return None

        diagonal_in = None
        display_section = soup.find("td", string=re.compile("Size", re.I))
        if display_section:
            display_text = display_section.find_next("td").text.strip()
            match = re.match(r"(\d+\.?\d*)\s*inches", display_text)
            if match:
                diagonal_in = float(match.group(1))
            else:
                logging.error(f"Invalid display size format on page {url}: {display_text}")
                return None
        else:
            logging.error(f"No display size found on page {url}")
            return None

        notch_type = "None"
        display_type = soup.find("td", string=re.compile("Type", re.I))
        if display_type:
            display_type = display_type.find_next("td").text.lower()
            if "punch-hole" in display_type or "dynamic amoled" in display_type:
                notch_type = "Punch-hole"
            elif "notch" in display_type or "waterdrop" in display_type:
                notch_type = "Notch"
            elif "full" in display_type or "edge-to-edge" in display_type:
                notch_type = "Full"

        return {
            "brand": brand,
            "model": model,
            "height_mm": height_mm,
            "width_mm": width_mm,
            "diagonal_in": diagonal_in,
            "notch_type": notch_type
        }
    except requests.RequestException as e:
        logging.error(f"Network error parsing device page {url}: {e}")
        return None
    except Exception as e:
        logging.error(f"Error parsing device page {url}: {e}")
        return None

def update_device_from_source(brand, model):
    device_data = fetch_device_data_from_gsmarena(brand, model)
    if not device_data:
        return None, f"Device {brand} {model} not found or failed to fetch."

    brand = device_data["brand"]
    model = device_data["model"]
    height_mm = device_data["height_mm"]
    width_mm = device_data["width_mm"]
    diagonal_in = device_data["diagonal_in"]
    notch_type = device_data["notch_type"]

    try:
        with sqlite3.connect(DB_PATH) as conn:
            if device_exists(brand, model):
                cursor = conn.execute(
                    "SELECT height_mm, width_mm, diagonal_in, notch_type FROM glasses WHERE brand=? AND model=?",
                    (brand, model)
                )
                current = cursor.fetchone()
                if current and (
                    abs(current[0] - height_mm) > 0.01 or
                    abs(current[1] - width_mm) > 0.01 or
                    abs(current[2] - diagonal_in) > 0.01 or
                    current[3] != notch_type
                ):
                    conn.execute(
                        """
                        UPDATE glasses
                        SET height_mm=?, width_mm=?, diagonal_in=?, notch_type=?
                        WHERE brand=? AND model=?
                        """,
                        (height_mm, width_mm, diagonal_in, notch_type, brand, model)
                    )
                    conn.commit()
                    return "updated", f"Updated {brand} {model} ({height_mm}×{width_mm} mm, {diagonal_in} in, {notch_type})"
                else:
                    return "skipped", f"Skipped {brand} {model} (no changes needed)"
            else:
                add_glass(brand, model, height_mm, width_mm, diagonal_in, notch_type)
                conn.commit()
                return "inserted", f"Added {brand} {model} ({height_mm}×{width_mm} mm, {diagonal_in} in, {notch_type})"
    except sqlite3.Error as e:
        logging.error(f"Database error for {brand} {model}: {str(e)}")
        return None, f"Database error for {brand} {model}: {str(e)}"