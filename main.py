import os
import re
import io
import base64
import math

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.transforms import ScaledTranslation
import json
from datetime import date, timedelta

from flask import Flask, request, redirect, url_for, session, render_template_string, jsonify
import mysql.connector as sql
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from google import genai
except ImportError:
    genai = None

# ============================================================
# CONFIGURATION
# ============================================================


# Render / production configuration.
# Keep credentials and API keys OUT of GitHub. Set these as
# environment variables in Render (or in your local environment).
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME", "PHC_FAKE")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

con = sql.connect(
    host=DB_HOST,
    port=DB_PORT,
    user=DB_USER,
    passwd=DB_PASSWORD,
    database=DB_NAME,
)

cur = con.cursor()

print(
    f"CONFIG: DB_HOST={DB_HOST!r}, DB_PORT={DB_PORT}, DB_NAME={DB_NAME!r}, "
    f"GEMINI_CONFIGURED={bool(GEMINI_API_KEY and genai is not None)}",
    flush=True,
)

client = None
if GEMINI_API_KEY and genai is not None:
    client = genai.Client(api_key=GEMINI_API_KEY)
else:
    print("WARNING: Gemini client not configured (missing GEMINI_API_KEY "
          "or google-genai not installed). Coven AI features will return "
          "a friendly error instead of crashing.", flush=True)


try:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS medicine_usage_history (
            usage_id BIGINT AUTO_INCREMENT PRIMARY KEY,
            phc_id INT NOT NULL,
            medicine_id INT NOT NULL,
            units_used INT NOT NULL,
            used_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_usage_phc_medicine_date
                (phc_id, medicine_id, used_at)
        )
    """)
    con.commit()
except Exception as e:
    print("Usage history table setup warning:", e, flush=True)


# ============================================================
# HELPERS
# ============================================================

def clean_ai_response(text):
    text = re.sub(r'#{1,6}\s*', '', text)
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'\*(.*?)\*', r'\1', text)
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)
    return text.strip()


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two lat/lon points.
    Returns None if any coordinate is missing."""
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return None
    try:
        lat1, lon1, lat2, lon2 = float(lat1), float(lon1), float(lat2), float(lon2)
    except (TypeError, ValueError):
        return None
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return round(2 * R * math.asin(math.sqrt(a)), 2)


def get_all_phcs():
    cur = con.cursor()
    cur.execute("""
        SELECT phc_id, name, district, facility_type, latitude, longitude
        FROM phcs
        ORDER BY district, name
    """)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    return rows


def get_phcs_with_distance(user_lat=None, user_lon=None):
    """All PHCs, each annotated with distance_km from the given point
    (or None if no point was supplied). Sorted nearest-first when a
    point is given, otherwise alphabetically by district/name."""
    phcs = get_all_phcs()
    for p in phcs:
        p["distance_km"] = haversine_km(user_lat, user_lon, p.get("latitude"), p.get("longitude"))
    if user_lat is not None and user_lon is not None:
        phcs.sort(key=lambda p: (p["distance_km"] is None, p["distance_km"]))
    return phcs


def get_phc(phc_id):
    cur = con.cursor()
    cur.execute("""
        SELECT phc_id, name, district, facility_type, latitude, longitude
        FROM phcs
        WHERE phc_id = %s
    """, (phc_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        return None
    cols = [d[0] for d in cur.description]
    result = dict(zip(cols, row))
    cur.close()
    return result


def get_beds(phc_id=None):
    cur = con.cursor()
    if phc_id is not None:
        cur.execute("""
            SELECT p.phc_id, p.name AS phc_name, p.district,
                   b.ward_type, b.total_beds, b.available_beds, b.last_updated
            FROM beds b
            JOIN phcs p ON b.phc_id = p.phc_id
            WHERE b.phc_id = %s
            ORDER BY p.name, b.ward_type
        """, (phc_id,))
    else:
        cur.execute("""
            SELECT p.phc_id, p.name AS phc_name, p.district,
                   b.ward_type, b.total_beds, b.available_beds, b.last_updated
            FROM beds b
            JOIN phcs p ON b.phc_id = p.phc_id
            ORDER BY p.name, b.ward_type
        """)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    return rows


def get_stock(phc_id=None):
    cur = con.cursor()
    if phc_id is not None:
        cur.execute("""
            SELECT p.phc_id, p.name AS phc_name, p.district,
                   mc.medicine_id, mc.medicine_name, mc.category,
                   s.quantity_available, s.expiry_date, s.last_updated
            FROM phc_medicine_stock s
            JOIN phcs p ON s.phc_id = p.phc_id
            JOIN medicine_catalog mc ON s.medicine_id = mc.medicine_id
            WHERE s.phc_id = %s
            ORDER BY p.name, mc.medicine_name
        """, (phc_id,))
    else:
        cur.execute("""
            SELECT p.phc_id, p.name AS phc_name, p.district,
                   mc.medicine_id, mc.medicine_name, mc.category,
                   s.quantity_available, s.expiry_date, s.last_updated
            FROM phc_medicine_stock s
            JOIN phcs p ON s.phc_id = p.phc_id
            JOIN medicine_catalog mc ON s.medicine_id = mc.medicine_id
            ORDER BY p.name, mc.medicine_name
        """)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    return rows


def get_medicine_catalog():
    cur = con.cursor()
    cur.execute("""
        SELECT medicine_id, medicine_name, category
        FROM medicine_catalog
        ORDER BY medicine_name
    """)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    return rows


def analyze_stock(phc_id=None):
    stock = get_stock(phc_id)
    warnings = []
    for item in stock:
        qty = item["quantity_available"]
        if qty == 0:
            warnings.append({"type": "OUT_OF_STOCK", "phc_name": item["phc_name"],
                              "medicine": item["medicine_name"], "quantity": qty})
        elif qty <= 5:
            warnings.append({"type": "LOW_STOCK", "phc_name": item["phc_name"],
                              "medicine": item["medicine_name"], "quantity": qty})
    return warnings


# ============================================================
# ADMIN AI DATA
# ============================================================

def format_network_data_for_ai():
    return f"""
PHCs:
{get_all_phcs()}

Beds:
{get_beds()}

Medicine Stock:
{get_stock()}

Warnings:
{analyze_stock()}
"""


# ============================================================
# CITIZEN AI DATA
#
# IMPORTANT: This deliberately DOES NOT include:
# - beds / available beds
# - medicine stock / quantities
# - stock warnings
# (Specific bed/medicine availability is only ever revealed through
# the structured allocation flow below, never through free-text
# network dumps.)
# ============================================================

def format_citizen_data_for_ai():
    return f"""
PHCs:
{get_all_phcs()}

Medicine Catalog:
{get_medicine_catalog()}
"""


def get_medicine_restock_recommendations():
    cur = con.cursor()
    cur.execute("""
        SELECT shortage_phc, shortage_district, medicine_name, category,
               shortage_quantity, stock_status, alert_level,
               source_phc, source_district, source_quantity,
               source_expiry_date, source_distance_km
        FROM best_medicine_redistribution
        ORDER BY priority_score ASC, source_distance_km ASC
    """)
    rows = cur.fetchall()
    cur.close()
    recommendations = []
    for row in rows:
        recommendations.append({
            "shortage_phc": row[0], "shortage_district": row[1],
            "medicine_name": row[2], "category": row[3],
            "shortage_quantity": row[4], "stock_status": row[5],
            "alert_level": row[6], "source_phc": row[7],
            "source_district": row[8], "source_quantity": row[9],
            "source_expiry_date": str(row[10]), "source_distance_km": float(row[11])
        })
    return recommendations


def format_phc_data_for_ai(phc_id):
    query = """
        SELECT phc_name, phc_district, medicine_name, category,
               quantity_available, stock_status, alert_level, expiry_date
        FROM admin_phc_shortage_view
        WHERE phc_id = %s
        ORDER BY
            CASE
                WHEN alert_level = 'CRITICAL' THEN 1
                WHEN alert_level = 'WARNING' THEN 2
                WHEN alert_level = 'NORMAL' THEN 3
                ELSE 4
            END,
            quantity_available ASC,
            expiry_date ASC
    """
    cursor = con.cursor(dictionary=True)
    try:
        cursor.execute(query, (phc_id,))
        rows = cursor.fetchall()
        if not rows:
            return "No medicine records found for this PHC."
        result = []
        for row in rows:
            result.append(f"""
PHC: {row['phc_name']}
District: {row['phc_district']}
Medicine: {row['medicine_name']}
Category: {row['category']}
Current Stock: {row['quantity_available']} units
Status: {row['stock_status']}
Alert Level: {row['alert_level']}
Expiry Date: {row['expiry_date']}
""")
        return "\n".join(result)
    finally:
        cursor.close()


# ============================================================
# ADMIN GEMINI ASSISTANT
# ============================================================

def ask_gemini(question, admin_phc_id=None):
    if not admin_phc_id:
        return "Access denied. No PHC is assigned to this account."

    admin_phc = get_phc(admin_phc_id)
    if not admin_phc:
        return "Access denied. PHC could not be identified."

    if client is None:
        return "Coven AI is not configured on this server (missing Gemini API key)."

    phc_data = format_phc_data_for_ai(admin_phc_id)

    prompt = f"""
You are Coven AI, an inventory management assistant for a Primary
Health Centre (PHC). Be direct, warm and efficient -- like a sharp
colleague who has already read the chart, not a chatbot reciting a
disclaimer.

LOGGED-IN PHC
PHC: {admin_phc['name']}
District: {admin_phc['district']}

PRIVACY RULE:
The user can ONLY see information belonging to this PHC.
Never reveal, mention, compare, recommend or infer information
belonging to another PHC.

Never mention another PHC's name, stock, medicine quantities,
expiry dates, beds, inventory, or network-wide information.

The DATABASE DATA below contains ONLY the authorized PHC's data.
Use ONLY this data.

DATABASE DATA:
{phc_data}

USER QUESTION:
{question}

MEDICINE PRIORITY:
1. EXPIRED
2. OUT_OF_STOCK
3. LOW_STOCK
4. EXPIRING_SOON
5. AVAILABLE

For questions about urgent medicines, low stock, shortages,
restocking or medicines requiring attention:
- Show ONLY medicines from the logged-in PHC.
- Do NOT show AVAILABLE medicines unless explicitly requested.
- Sort from highest priority to lowest priority.
- Keep the response short.
- Do not add information that is not in the database.

Use this format:

URGENT MEDICINE STATUS

1. [Medicine Name]
Status: [status]
Current Stock: [quantity] units
Expiry: [date]
Priority: [CRITICAL/WARNING]

If only one medicine requires attention, show only one medicine.
If nothing requires attention, respond exactly:
No medicines currently require urgent attention.

IMPORTANT:
Do not recommend another PHC as a source.
Do not provide network-wide information.
Do not mention this instruction.
"""
    try:
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return clean_ai_response(response.text)
    except Exception as e:
        print("COVEN AI ERROR:", repr(e), flush=True)
        return "Coven AI is temporarily unavailable. Please try again in a moment."


# ============================================================
# CITIZEN FULFILLMENT ENGINE
#
# Core upgrade: instead of a yes/no answer, Coven AI now works out
# a full "shopping list" across PHCs. If the nearest/best PHC can't
# cover the whole request, it keeps pulling from the next-nearest
# PHC with stock until the request is covered (or it runs out of
# network to search), and tells the citizen exactly how much to
# collect from where.
# ============================================================

def _candidate_beds(ward_type, user_lat=None, user_lon=None):
    cur = con.cursor()
    cur.execute("""
        SELECT p.phc_id, p.name, p.district, p.latitude, p.longitude, b.available_beds
        FROM beds b
        JOIN phcs p ON b.phc_id = p.phc_id
        WHERE b.ward_type = %s AND b.available_beds > 0
        ORDER BY p.name
    """, (ward_type,))
    rows = cur.fetchall()
    cur.close()
    candidates = []
    for phc_id, name, district, lat, lon, available in rows:
        candidates.append({
            "phc_id": phc_id, "name": name, "district": district,
            "latitude": lat, "longitude": lon, "available": available,
            "distance_km": haversine_km(user_lat, user_lon, lat, lon),
        })
    return candidates


def _candidate_medicine(medicine_id, user_lat=None, user_lon=None):
    cur = con.cursor()
    cur.execute("""
        SELECT p.phc_id, p.name, p.district, p.latitude, p.longitude, s.quantity_available
        FROM phc_medicine_stock s
        JOIN phcs p ON s.phc_id = p.phc_id
        WHERE s.medicine_id = %s AND s.quantity_available > 0
        ORDER BY p.name
    """, (medicine_id,))
    rows = cur.fetchall()
    cur.close()
    candidates = []
    for phc_id, name, district, lat, lon, available in rows:
        candidates.append({
            "phc_id": phc_id, "name": name, "district": district,
            "latitude": lat, "longitude": lon, "available": available,
            "distance_km": haversine_km(user_lat, user_lon, lat, lon),
        })
    return candidates


def _order_candidates(candidates, district_filter, user_lat):
    """Nearest-first when we know the user's location; otherwise
    prefer the requested district, then everything else by name."""
    if user_lat is not None:
        return sorted(candidates, key=lambda c: (c["distance_km"] is None, c["distance_km"]))
    if district_filter:
        in_district = [c for c in candidates if c["district"].lower() == district_filter]
        rest = [c for c in candidates if c["district"].lower() != district_filter]
        return in_district + rest
    return sorted(candidates, key=lambda c: c["name"])


def _allocate(candidates, needed):
    """Greedily fill `needed` units across candidates in the order
    given. Returns (allocation_list, remaining_unfulfilled)."""
    allocation = []
    remaining = needed
    for c in candidates:
        if remaining <= 0:
            break
        take = min(c["available"], remaining)
        if take <= 0:
            continue
        entry = dict(c)
        entry["allocated"] = take
        allocation.append(entry)
        remaining -= take
    return allocation, remaining


def _describe_allocation(item_label, unit_word, needed, allocation, remaining):
    """Deterministic, always-available fallback text (used if Gemini
    is unavailable, and as the factual basis Gemini is asked to
    restyle)."""
    lines = [f"You asked for {needed} {unit_word} of {item_label}.", ""]
    if not allocation:
        lines.append(f"None currently available anywhere in the network. Sorry about that.")
        return "\n".join(lines)

    for i, a in enumerate(allocation, start=1):
        dist_text = f" ({a['distance_km']} km away)" if a["distance_km"] is not None else ""
        if i == 1:
            lines.append(f"Go to {a['name']}, {a['district']}{dist_text} -- collect {a['allocated']} {unit_word}.")
        else:
            lines.append(f"Then {a['name']}, {a['district']}{dist_text} -- collect the remaining {a['allocated']} {unit_word}.")

    total_found = sum(a["allocated"] for a in allocation)
    lines.append("")
    if remaining <= 0:
        lines.append(f"That covers your full request of {needed} {unit_word}.")
    else:
        lines.append(f"That covers {total_found} of {needed} {unit_word} -- {remaining} {unit_word} short across the network right now.")
    return "\n".join(lines)


def _narrate_allocation(question, item_label, unit_word, needed, allocation, remaining, is_bed):
    """Ask Gemini to phrase the factual allocation naturally. Falls
    back to the deterministic text if Gemini isn't configured or
    errors out -- the citizen always gets a usable answer either
    way."""
    fallback = _describe_allocation(item_label, unit_word, needed, allocation, remaining)

    if client is None:
        return fallback

    facts_lines = []
    for i, a in enumerate(allocation, start=1):
        dist_text = f"{a['distance_km']} km away" if a["distance_km"] is not None else "distance unknown"
        facts_lines.append(
            f"{i}. PHC: {a['name']} | District: {a['district']} | "
            f"{'Beds' if is_bed else 'Units'} to collect here: {a['allocated']} | {dist_text}"
        )
    facts = "\n".join(facts_lines) if facts_lines else "No PHC in the network currently has any available."

    prompt = f"""
You are Coven AI, a friendly public healthcare resource assistant
for patients in India. Speak plainly and warmly, like a helpful
front-desk volunteer -- not like a corporate chatbot.

The citizen asked:
{question}

The system has already worked out exactly where to go and how much
to collect at each stop (nearest usable option first). These facts
are final and already confirmed against the database -- do not
change any number, name or order:

Requested: {needed} {unit_word} of {item_label}
Allocation plan (in order, already nearest-first when a location was known):
{facts}
Still short by: {max(remaining, 0)} {unit_word}

Turn this into a short, clear answer for the citizen:
- If one stop covers it, just tell them where to go and how much to collect.
- If it takes more than one stop, number the stops in order, and say how much to collect at each.
- For every stop where a distance in km is given above, you MUST state that distance in your sentence for that stop -- never drop it, even if it feels repetitive. Only skip mentioning distance for a stop marked "distance unknown".
- If it's short overall, say so plainly at the end -- don't hide it.
- Do NOT invent any PHC, quantity or distance that isn't in the facts above.
- Do not give medical diagnosis or treatment advice.
- Keep it under 120 words.
"""
    try:
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        if response and response.text:
            return clean_ai_response(response.text)
        return fallback
    except Exception as e:
        print("CITIZEN GEMINI NARRATION ERROR:", repr(e), flush=True)
        return fallback


def _locations_to_html(locations):
    """Small, always-accurate stop list rendered straight from the
    allocation data -- shown underneath the AI's own phrasing so the
    distance and pickup amount are never left out, even on a turn
    where the model's wording glosses over them (e.g. because it
    wasn't given a real distance to work with)."""
    if not locations:
        return ""
    items = ""
    for i, loc in enumerate(locations, start=1):
        if loc.get("distance_km") is not None:
            dist = f"{loc['distance_km']} km away"
        else:
            dist = "distance unknown (allow location access to see this)"
        items += f"""
        <div class="card">
            <h3>{i}. {loc['name']}</h3>
            <p>\U0001F4CD {loc['district']} <span class="distance-pill">{dist}</span></p>
            <p>Collect: <strong>{loc.get('allocated', '')}</strong></p>
        </div>
        """
    return f'<div class="card-grid">{items}</div>'


def get_citizen_answer(question, user_lat=None, user_lon=None):
    """Returns a dict: {"text": <str>, "locations": [ {name, district,
    latitude, longitude, distance_km, allocated?}, ... ]} so callers
    can both display the answer and plot it on a map."""
    question_lower = question.lower()

    # DETECT DISTRICT / AREA MENTIONED
    district_filter = None
    all_phcs = get_all_phcs()
    known_districts = {phc["district"].lower() for phc in all_phcs}

    for district in known_districts:
        if district in question_lower:
            district_filter = district
            break

    if not district_filter:
        for district in known_districts:
            district_words = district.split()
            if all(word in question_lower for word in district_words):
                district_filter = district
                break

    # DETECT BED REQUEST
    requested_ward = None
    if "icu" in question_lower:
        requested_ward = "icu"
    elif "oxygen" in question_lower:
        requested_ward = "oxygen"
    elif "general" in question_lower:
        requested_ward = "general"

    # DETECT REQUESTED QUANTITY
    requested_number = 1
    quantity_patterns = [
        r"\b(\d+)\s*(?:units?|tablets?|capsules?|bottles?|doses?|beds?)\b",
        r"\bneed\s+(\d+)\b",
        r"\bwant\s+(\d+)\b",
        r"\bhave\s+(\d+)\b",
        r"\bget\s+(\d+)\b",
        r"\buse\s+(\d+)\b",
        r"\bfor\s+(\d+)\b",
    ]
    found_quantity = False
    for pattern in quantity_patterns:
        m = re.search(pattern, question_lower)
        if m:
            requested_number = int(m.group(1))
            found_quantity = True
            break
    if not found_quantity:
        numbers = re.findall(r"\b\d+\b", question_lower)
        if numbers:
            requested_number = int(numbers[0])

    # ============================================================
    # BED REQUEST
    # ============================================================
    if requested_ward:
        candidates = _candidate_beds(requested_ward, user_lat, user_lon)
        ordered = _order_candidates(candidates, district_filter, user_lat)
        allocation, remaining = _allocate(ordered, requested_number)

        text = _narrate_allocation(
            question, f"{requested_ward.upper()} beds", "bed(s)",
            requested_number, allocation, remaining, is_bed=True
        )
        locations = [
            {"name": a["name"], "district": a["district"],
             "latitude": a["latitude"], "longitude": a["longitude"],
             "distance_km": a["distance_km"], "allocated": a["allocated"]}
            for a in allocation
        ]
        return {"text": text, "locations": locations}

    # ============================================================
    # MEDICINE REQUEST
    # ============================================================
    else:
        catalog = get_medicine_catalog()
        matched_medicine = None
        for medicine in catalog:
            if str(medicine["medicine_name"]).lower() in question_lower:
                matched_medicine = medicine
                break

        if matched_medicine:
            candidates = _candidate_medicine(matched_medicine["medicine_id"], user_lat, user_lon)
            ordered = _order_candidates(candidates, district_filter, user_lat)
            allocation, remaining = _allocate(ordered, requested_number)

            text = _narrate_allocation(
                question, matched_medicine["medicine_name"], "unit(s)",
                requested_number, allocation, remaining, is_bed=False
            )
            locations = [
                {"name": a["name"], "district": a["district"],
                 "latitude": a["latitude"], "longitude": a["longitude"],
                 "distance_km": a["distance_km"], "allocated": a["allocated"]}
                for a in allocation
            ]
            return {"text": text, "locations": locations}

        else:
            # GENERAL FALLBACK -- not a specific bed/medicine check
            safe_data = format_citizen_data_for_ai()
            prompt = f"""
You are Coven AI, a friendly public healthcare assistant
for a network of Primary Health Centres (PHCs)
in India.

A citizen has asked a general question. Use
ONLY the data provided below to answer.

CITIZEN-SAFE DATA:
{safe_data}

IMPORTANT RULES:
1. Never mention bed counts, medicine quantities, or stock numbers --
   this information is not included above and must never be invented.
2. If asked about a SPECIFIC bed/medicine, tell them to ask directly,
   e.g. "I need 2 ICU beds in North Delhi" or "Do you have Paracetamol,
   I need 90".
3. You CAN answer general questions: which PHCs exist, which PHCs are
   in a given district/area, what medicines are in the catalog, what
   facility type a PHC is, general info about the PHC network.
4. If the question cannot be answered from the data, say so honestly.
5. Do not provide medical diagnosis or treatment advice.
6. Keep the answer short, warm and clear.

CITIZEN'S QUESTION:
{question}
"""
            try:
                if client is None:
                    text = ("I couldn't identify a specific bed or medicine "
                            "request. Please try asking directly, e.g. "
                            "'I need 2 ICU beds in North Delhi' or "
                            "'I need 90 units of Paracetamol'.")
                else:
                    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
                    if response and response.text:
                        text = clean_ai_response(response.text)
                    else:
                        text = ("I couldn't identify a specific bed or medicine "
                                "request. Please try asking directly, e.g. "
                                "'I need 2 ICU beds in North Delhi'.")
            except Exception as e:
                print("CITIZEN GEMINI ERROR:", repr(e), flush=True)
                text = ("I couldn't identify a specific bed or medicine "
                        "request. Please try asking directly, e.g. "
                        "'I need 2 ICU beds in North Delhi'.")
            return {"text": text, "locations": []}


# ============================================================
# PREDICTIVE STOCK MODEL
# ============================================================

def get_predictive_stock(phc_id):
    cursor = con.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT
                s.medicine_id, m.medicine_name, m.category,
                s.quantity_available, s.expiry_date,
                COALESCE(
                    SUM(
                        CASE
                            WHEN h.used_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
                            THEN h.units_used
                            ELSE 0
                        END
                    ), 0
                ) AS units_used_30_days
            FROM phc_medicine_stock s
            JOIN medicine_catalog m ON s.medicine_id = m.medicine_id
            LEFT JOIN medicine_usage_history h
                ON h.phc_id = s.phc_id AND h.medicine_id = s.medicine_id
            WHERE s.phc_id = %s
            GROUP BY s.medicine_id, m.medicine_name, m.category,
                     s.quantity_available, s.expiry_date
            ORDER BY s.quantity_available ASC
        """, (phc_id,))

        rows = cursor.fetchall()
        predictions = []

        for row in rows:
            current_stock = int(row["quantity_available"] or 0)
            used_30_days = float(row["units_used_30_days"] or 0)
            average_daily_usage = used_30_days / 30

            if average_daily_usage > 0:
                predicted_days = current_stock / average_daily_usage
            else:
                predicted_days = None

            if current_stock <= 0:
                prediction_status = "OUT_OF_STOCK"
            elif predicted_days is not None and predicted_days <= 3:
                prediction_status = "CRITICAL"
            elif predicted_days is not None and predicted_days <= 7:
                prediction_status = "WARNING"
            elif predicted_days is not None and predicted_days <= 14:
                prediction_status = "MONITOR"
            else:
                prediction_status = "STABLE"

            predictions.append({
                "medicine_id": row["medicine_id"],
                "medicine_name": row["medicine_name"],
                "category": row["category"],
                "current_stock": current_stock,
                "used_30_days": used_30_days,
                "average_daily_usage": round(average_daily_usage, 2),
                "predicted_days": round(predicted_days, 1) if predicted_days is not None else None,
                "expiry_date": str(row["expiry_date"]) if row["expiry_date"] else None,
                "prediction_status": prediction_status
            })

        return predictions
    finally:
        cursor.close()


# ============================================================
# SHARED PAGE DESIGN
#
# The HTML/CSS/JS template is a *plain* (non-f) string using
# __PLACEHOLDER__ tokens, filled in with .replace(). This avoids the
# f-string brace-escaping trap entirely -- CSS and JS can use { }
# freely without doubling them.
# ============================================================

PAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Poppins:wght@500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
* { box-sizing: border-box; }
body {
    margin: 0;
    font-family: 'Inter', sans-serif;
    background: linear-gradient(135deg, rgba(232,248,244,0.96), rgba(244,249,252,0.97));
    color: #19352d;
    min-height: 100vh;
}
body::before {
    content: "";
    position: fixed;
    inset: 0;
    z-index: -1;
    background-image: url("https://images.unsplash.com/photo-1576091160399-112ba8d25d1d?auto=format&fit=crop&w=2000&q=80");
    background-size: cover;
    background-position: center;
    opacity: 0.055;
}
.navbar {
    width: 100%;
    background: rgba(255,255,255,0.92);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid rgba(11,110,79,0.12);
    padding: 17px 7%;
    display: flex;
    justify-content: space-between;
    align-items: center;
    position: sticky;
    top: 0;
    z-index: 100;
}
.logo {
    font-family: 'Poppins', sans-serif;
    font-size: 21px;
    font-weight: 800;
    color: #087f5b;
    letter-spacing: -0.5px;
}
.nav-right a {
    text-decoration: none;
    color: #31544a;
    margin-left: 22px;
    font-weight: 600;
    font-size: 14px;
}
.nav-right a:hover { color: #087f5b; }
.container {
    width: 90%;
    max-width: 1100px;
    margin: 45px auto;
}
.hero {
    position: relative;
    overflow: hidden;
    background: linear-gradient(135deg, #087f5b, #0b6e4f);
    color: white;
    padding: 60px 55px;
    border-radius: 28px;
    box-shadow: 0 20px 50px rgba(11,110,79,0.20);
    margin-bottom: 30px;
}
.hero::after {
    content: "\\271A";
    position: absolute;
    right: 55px;
    bottom: -30px;
    font-size: 180px;
    opacity: 0.08;
}
.hero h1 {
    border: none;
    padding: 0;
    margin: 0 0 15px;
    color: white;
    font-family: 'Poppins', sans-serif;
    font-size: clamp(30px, 5vw, 55px);
    line-height: 1.08;
    max-width: 800px;
}
.hero p {
    color: rgba(255,255,255,0.88);
    max-width: 650px;
    font-size: 17px;
    line-height: 1.7;
}

/* ============================================================
   LIQUID GLASS DESIGN SYSTEM
   ============================================================ */

.liquid-glass {
    background: rgba(255,255,255,0.55);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border: 1px solid rgba(255,255,255,0.4);
    box-shadow: inset 0 1px 1px rgba(255,255,255,0.6), 0 8px 25px rgba(22,78,64,0.08);
    position: relative;
}

.liquid-glass-strong {
    background: rgba(255,255,255,0.75);
    backdrop-filter: blur(30px);
    -webkit-backdrop-filter: blur(30px);
    border: 1px solid rgba(255,255,255,0.5);
    box-shadow: inset 0 1px 1px rgba(255,255,255,0.7), 0 15px 40px rgba(22,78,64,0.12);
}

.cinematic-hero {
    position: relative;
    overflow: hidden;
    background:
        linear-gradient(135deg, rgba(8,127,91,0.92), rgba(11,110,79,0.96)),
        url("https://images.unsplash.com/photo-1587351021355-a479a299d2f9?auto=format&fit=crop&w=2000&q=80");
    background-size: cover;
    background-position: center;
    color: white;
    padding: 80px 55px;
    border-radius: 32px;
    box-shadow: 0 25px 60px rgba(11,110,79,0.25);
    margin-bottom: 30px;
}
.cinematic-hero h1 {
    border: none;
    padding: 0;
    margin: 0 0 18px;
    color: white;
    font-family: 'Poppins', sans-serif;
    font-size: clamp(32px, 6vw, 60px);
    line-height: 1.05;
    max-width: 850px;
    display: flex;
    flex-wrap: wrap;
    row-gap: 0.1em;
}
.cinematic-hero h1 .blur-word {
    display: inline-block;
    margin-right: 0.28em;
    opacity: 0;
    filter: blur(10px);
    transform: translateY(20px);
    animation: blurIn 0.7s ease-out forwards;
}
@keyframes blurIn {
    0%   { opacity: 0;   filter: blur(10px); transform: translateY(20px); }
    50%  { opacity: 0.5; filter: blur(5px);  transform: translateY(-3px); }
    100% { opacity: 1;   filter: blur(0px);  transform: translateY(0); }
}
.cinematic-hero p {
    color: rgba(255,255,255,0.9);
    max-width: 650px;
    font-size: 17px;
    line-height: 1.7;
    opacity: 0;
    animation: fadeUp 0.8s ease-out 0.9s forwards;
}
@keyframes fadeUp {
    from { opacity: 0; transform: translateY(15px); }
    to   { opacity: 1; transform: translateY(0); }
}
.cinematic-hero .button {
    opacity: 0;
    animation: fadeUp 0.8s ease-out 1.2s forwards;
}
.glass-card {
    background: rgba(255,255,255,0.6);
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
    border: 1px solid rgba(255,255,255,0.45);
    box-shadow: inset 0 1px 1px rgba(255,255,255,0.6), 0 8px 25px rgba(22,78,64,0.08);
    padding: 25px;
    border-radius: 20px;
    transition: transform 0.25s ease, box-shadow 0.25s ease;
}
.glass-card:hover {
    transform: translateY(-6px);
    box-shadow: inset 0 1px 1px rgba(255,255,255,0.7), 0 18px 40px rgba(22,78,64,0.15);
}
h1 {
    font-family: 'Poppins', sans-serif;
    color: #087f5b;
    font-size: 32px;
    margin-bottom: 25px;
}
h2 { font-family: 'Poppins', sans-serif; color: #164e40; }
.card-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
    gap: 20px;
    margin-top: 25px;
}
.card {
    background: rgba(255,255,255,0.92);
    padding: 25px;
    border-radius: 18px;
    border: 1px solid rgba(11,110,79,0.08);
    box-shadow: 0 8px 25px rgba(22,78,64,0.07);
    transition: transform 0.2s ease, box-shadow 0.2s ease;
}
.card:hover {
    transform: translateY(-5px);
    box-shadow: 0 15px 35px rgba(22,78,64,0.12);
}
.card-icon { font-size: 32px; margin-bottom: 12px; }
.card h3 {
    margin: 5px 0 8px;
    font-family: 'Poppins', sans-serif;
    color: #164e40;
}
.card p { color: #657871; font-size: 14px; line-height: 1.6; }
a { color: #087f5b; text-decoration: none; font-weight: 600; }
a:hover { color: #065f45; }
.button {
    display: inline-block;
    background: white;
    color: #087f5b;
    padding: 13px 22px;
    border-radius: 10px;
    font-weight: 700;
    text-decoration: none;
    margin-top: 12px;
    transition: 0.2s;
}
.button:hover {
    transform: translateY(-2px);
    color: #087f5b;
    box-shadow: 0 8px 20px rgba(0,0,0,0.12);
}
.button-green {
    background: #087f5b;
    color: white;
    border: none;
    cursor: pointer;
    font-family: inherit;
    font-weight: 700;
    padding: 12px 22px;
    border-radius: 10px;
    margin-top: 10px;
}
.button-green:hover { background: #066848; color: white; }
form {
    background: rgba(255,255,255,0.94);
    padding: 28px;
    border-radius: 18px;
    box-shadow: 0 8px 30px rgba(22,78,64,0.07);
    border: 1px solid rgba(11,110,79,0.08);
    margin-top: 20px;
}
input, select {
    width: 100%;
    max-width: 450px;
    padding: 12px 14px;
    margin: 7px 0 15px;
    border: 1px solid #d7e4df;
    border-radius: 9px;
    font-family: inherit;
    font-size: 14px;
    outline: none;
}
input:focus, select:focus {
    border-color: #087f5b;
    box-shadow: 0 0 0 3px rgba(8,127,91,0.10);
}
table {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    overflow: hidden;
    background: white;
    border-radius: 15px;
    box-shadow: 0 8px 25px rgba(22,78,64,0.07);
    margin-top: 20px;
}
th { background: #087f5b; color: white; padding: 14px; text-align: left; font-size: 13px; }
td { padding: 13px; border-bottom: 1px solid #edf2f0; font-size: 14px; }
tr:last-child td { border-bottom: none; }
ul.menu {
    list-style: none;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
    gap: 15px;
}
ul.menu li a {
    display: block;
    background: white;
    padding: 20px;
    border-radius: 15px;
    box-shadow: 0 6px 20px rgba(22,78,64,0.07);
    border: 1px solid rgba(11,110,79,0.07);
    transition: 0.2s;
}
ul.menu li a:hover { transform: translateY(-3px); background: #f7fffc; }
.ai-floating {
    position: fixed;
    right: 25px;
    bottom: 25px;
    z-index: 999;
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 13px 18px;
    background: linear-gradient(135deg, #087f5b, #075d44);
    color: white !important;
    border-radius: 50px;
    box-shadow: 0 10px 30px rgba(0,0,0,0.20);
    font-weight: 700;
    font-size: 14px;
    text-decoration: none;
    transition: 0.25s;
}
.ai-floating:hover {
    transform: translateY(-4px) scale(1.02);
    color: white !important;
    box-shadow: 0 15px 35px rgba(0,0,0,0.25);
}
.ai-icon {
    width: 30px;
    height: 30px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(255,255,255,0.16);
    border-radius: 50%;
    font-size: 17px;
}
.ai-response-card { margin-top: 25px; }
.ai-response { font-size: 17px; line-height: 1.8; color: #31544a; white-space: pre-line; }
.alert {
    padding: 15px 18px;
    background: #fff4f4;
    color: #a92d2d;
    border-left: 4px solid #d64545;
    border-radius: 9px;
    margin: 15px 0;
}
.success {
    padding: 15px 18px;
    background: #effaf5;
    color: #087f5b;
    border-left: 4px solid #087f5b;
    border-radius: 9px;
    margin: 15px 0;
}
.back { display: inline-block; margin-top: 25px; color: #657871; }
.search-box { text-align: center; padding: 35px; }
.search-box input { max-width: 600px; font-size: 16px; padding: 15px; }
.phc-card { display: flex; justify-content: space-between; align-items: center; gap: 20px; }
.phc-card-info h3 { margin-top: 0; }
.admin-alert-section {
    margin: 25px 0;
    padding: 25px;
    border-radius: 18px;
    background: #fff5f5;
    border: 2px solid #ff3b30;
    box-shadow: 0 8px 25px rgba(255, 59, 48, 0.12);
}
.alert-heading {
    color: #d70015;
    font-size: 28px;
    font-weight: 800;
    letter-spacing: 0.5px;
    margin-bottom: 5px;
}
.alert-subtitle { color: #7a1c1c; font-size: 15px; margin-bottom: 18px; }
.warning-container { width: 100%; }
.stock-warning {
    display: flex;
    align-items: center;
    gap: 18px;
    padding: 18px 20px;
    margin: 12px 0;
    border-radius: 14px;
    color: #ffffff;
    box-shadow: 0 5px 15px rgba(0, 0, 0, 0.12);
}
.out-of-stock { background: #d70015; border-left: 8px solid #8b0000; }
.low-stock { background: #e53935; border-left: 8px solid #b71c1c; }
.warning-icon { font-size: 38px; min-width: 48px; text-align: center; }
.warning-title { font-size: 25px; font-weight: 900; letter-spacing: 0.5px; }
.warning-medicine { font-size: 21px; font-weight: 800; margin-top: 3px; }
.warning-details { font-size: 16px; margin-top: 4px; font-weight: 600; }
.view-all-warnings { display: inline-block; margin-top: 15px; color: #d70015; font-weight: 800; }
.no-alerts {
    display: flex;
    align-items: center;
    gap: 15px;
    background: #effaf3;
    border: 2px solid #2e9d5b;
    color: #176b3a;
}
.success-icon {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 42px;
    height: 42px;
    border-radius: 50%;
    background: #2e9d5b;
    color: white;
    font-size: 25px;
    font-weight: 900;
}
.no-alerts h2 { margin: 0; }
.no-alerts p { margin: 4px 0 0; }
.map-box {
    width: 100%;
    height: 340px;
    border-radius: 18px;
    margin-top: 20px;
    box-shadow: 0 8px 25px rgba(22,78,64,0.10);
    border: 1px solid rgba(11,110,79,0.08);
}
.map-box.small { height: 240px; }
.distance-pill {
    display: inline-block;
    background: #e6f7f0;
    color: #087f5b;
    font-size: 12px;
    font-weight: 700;
    padding: 3px 10px;
    border-radius: 20px;
    margin-left: 8px;
}
@media(max-width: 700px) {
    .navbar { padding: 15px 5%; }
    .nav-right a { margin-left: 10px; font-size: 12px; }
    .hero { padding: 40px 25px; }
    .hero h1 { font-size: 32px; }
    .container { width: 94%; margin: 25px auto; }
    .ai-floating { right: 15px; bottom: 15px; }
    .ai-text { display: none; }
    .map-box { height: 260px; }
}


/* Merged analytics styles */
/* ============================================================
   ADMIN ANALYTICS DASHBOARD
   ============================================================ */

.dashboard-header {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 25px;
    margin-bottom: 22px;
}

.dashboard-header h1 {
    margin: 5px 0 8px;
    font-size: clamp(30px, 4vw, 46px);
    line-height: 1.1;
}

.dashboard-subtitle {
    margin: 0;
    color: #657871;
    font-size: 15px;
    line-height: 1.6;
}

.section-kicker {
    color: #087f5b;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 1.5px;
    text-transform: uppercase;
}

.danger-kicker { color: #c53030; }
.success-kicker { color: #27824b; }

.status-pill {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 9px 13px;
    border-radius: 999px;
    background: #effaf5;
    color: #176b3a;
    border: 1px solid #ccebd9;
    font-size: 12px;
    font-weight: 700;
    white-space: nowrap;
}

.status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #2e9d5b;
    box-shadow: 0 0 0 4px rgba(46,157,91,0.12);
}

.phc-info-strip {
    display: grid;
    grid-template-columns: 1.5fr 1fr 1fr;
    gap: 1px;
    overflow: hidden;
    background: #dceae4;
    border: 1px solid #dceae4;
    border-radius: 16px;
    margin-bottom: 20px;
    box-shadow: 0 7px 25px rgba(22,78,64,0.06);
}

.phc-info-strip > div {
    padding: 15px 18px;
    background: rgba(255,255,255,0.86);
    display: flex;
    flex-direction: column;
    gap: 5px;
}

.info-label,
.kpi-label {
    color: #72857d;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1px;
    text-transform: uppercase;
}

.phc-info-strip strong {
    color: #23453a;
    font-size: 13px;
}

.kpi-grid {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 14px;
    margin-bottom: 25px;
}

.kpi-card {
    min-height: 94px;
    display: flex;
    align-items: center;
    gap: 13px;
    padding: 16px;
    border-radius: 16px;
    background: rgba(255,255,255,0.90);
    border: 1px solid rgba(11,110,79,0.08);
    box-shadow: 0 8px 25px rgba(22,78,64,0.07);
}

.kpi-icon {
    width: 42px;
    height: 42px;
    flex: 0 0 42px;
    display: grid;
    place-items: center;
    border-radius: 12px;
    background: #eef8f3;
    font-size: 20px;
}

.kpi-card > div:last-child {
    min-width: 0;
}

.kpi-value {
    display: block;
    margin-top: 5px;
    color: #164e40;
    font-family: 'Poppins', sans-serif;
    font-size: 25px;
    line-height: 1;
}

.kpi-small {
    font-family: 'Inter', sans-serif;
    font-size: 12px;
    color: #82928c;
    font-weight: 600;
}

.kpi-warning {
    background: #fffaf0;
    border-color: #f4dfb0;
}

.kpi-warning .kpi-icon { background: #fff1d2; }

.kpi-warning .kpi-value { color: #b7791f; }

.kpi-danger {
    background: #fff6f5;
    border-color: #f1cfcb;
}

.kpi-danger .kpi-icon { background: #ffe5e2; }

.kpi-danger .kpi-value { color: #c53030; }

.admin-alert-section {
    margin: 0 0 28px;
    padding: 22px;
    border-radius: 18px;
}

.admin-alert-section:not(.no-alerts) {
    background: #fff7f6;
    border: 1px solid #f1d0cc;
    box-shadow: 0 8px 25px rgba(197,48,48,0.08);
}

.alert-heading {
    margin: 4px 0 5px;
    color: #b42318;
    font-family: 'Poppins', sans-serif;
    font-size: 23px;
}

.alert-subtitle {
    color: #80504b;
    font-size: 13px;
    margin: 0 0 15px;
}

.stock-warning {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 13px 15px;
    margin: 9px 0;
    border-radius: 12px;
    color: white;
    box-shadow: none;
}

.out-of-stock {
    background: #c53030;
    border-left: 5px solid #8b1e1e;
}

.low-stock {
    background: #d69e2e;
    border-left: 5px solid #a66f12;
}

.warning-icon {
    width: 34px;
    min-width: 34px;
    font-size: 23px;
    text-align: center;
}

.warning-title {
    font-size: 11px;
    font-weight: 900;
    letter-spacing: 0.9px;
}

.warning-medicine {
    font-size: 15px;
    font-weight: 800;
    margin-top: 2px;
}

.warning-details {
    font-size: 12px;
    margin-top: 2px;
    opacity: 0.95;
}

.view-all-warnings {
    display: inline-block;
    margin-top: 12px;
    color: #b42318;
    font-size: 13px;
}

.no-alerts {
    display: flex;
    align-items: center;
    gap: 14px;
    background: #f0faf4;
    border: 1px solid #c8e9d5;
    color: #176b3a;
}

.no-alerts h2 {
    margin: 4px 0 2px;
    font-size: 20px;
}

.no-alerts p {
    margin: 0;
    color: #4e7561;
    font-size: 13px;
}

.success-icon {
    width: 42px;
    height: 42px;
    flex: 0 0 42px;
    display: grid;
    place-items: center;
    border-radius: 50%;
    background: #2e9d5b;
    color: white;
    font-size: 23px;
    font-weight: 900;
}

.analytics-section {
    margin-top: 5px;
}

.section-heading-row {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 20px;
    margin-bottom: 15px;
}

.section-heading-row h2 {
    margin: 5px 0 3px;
    font-size: 26px;
}

.section-heading-row p {
    margin: 0;
    color: #71847c;
    font-size: 13px;
}

.outline-button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 10px 15px;
    border: 1px solid #b9d8cb;
    border-radius: 10px;
    background: rgba(255,255,255,0.75);
    color: #087f5b;
    font-size: 12px;
    font-weight: 800;
    white-space: nowrap;
}

.outline-button:hover {
    background: #effaf5;
    color: #066848;
}

.analytics-grid {
    display: grid;
    grid-template-columns: minmax(0, 0.92fr) minmax(0, 1.08fr);
    gap: 18px;
}

.chart-card {
    min-width: 0;
    overflow: hidden;
    background: rgba(255,255,255,0.91);
    border: 1px solid rgba(11,110,79,0.09);
    border-radius: 19px;
    box-shadow: 0 10px 30px rgba(22,78,64,0.075);
}

.chart-card-wide {
    grid-column: 1 / -1;
}

.chart-card-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 15px;
    padding: 20px 21px 5px;
}

.chart-card-heading > div:first-child {
    min-width: 0;
}

.chart-number {
    display: block;
    margin-bottom: 4px;
    color: #8aa39a;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1.3px;
}

.chart-card-heading h3 {
    margin: 0;
    color: #24463b;
    font-family: 'Poppins', sans-serif;
    font-size: 17px;
    line-height: 1.3;
}

.chart-card-heading p {
    margin: 5px 0 0;
    color: #7a8c85;
    font-size: 12px;
    line-height: 1.45;
}

.chart-badge {
    padding: 6px 9px;
    border-radius: 999px;
    background: #eff8f4;
    color: #087f5b;
    font-size: 9px;
    font-weight: 800;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    white-space: nowrap;
}

.chart-wrap {
    width: 100%;
    min-height: 315px;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 4px 18px 16px;
}

.pie-wrap {
    min-height: 300px;
    padding-right: 8px;
}

.chart-image {
    display: block;
    width: 100%;
    max-width: 900px;
    height: auto;
    margin: 0 auto;
}

.chart-image-pie {
    max-width: 520px;
}

.chart-empty {
    width: 100%;
    padding: 70px 20px;
    text-align: center;
    color: #7c8e87;
    font-size: 13px;
}

.admin-actions-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 15px;
    margin-top: 25px;
}

.action-card {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 12px;
    align-items: start;
    padding: 19px;
    border-radius: 17px;
    background: rgba(255,255,255,0.88);
    border: 1px solid rgba(11,110,79,0.08);
    box-shadow: 0 7px 22px rgba(22,78,64,0.06);
}

.action-icon {
    width: 42px;
    height: 42px;
    display: grid;
    place-items: center;
    border-radius: 12px;
    background: #eef8f3;
    font-size: 20px;
}

.action-card h3 {
    margin: 2px 0 4px;
    color: #24463b;
    font-family: 'Poppins', sans-serif;
    font-size: 15px;
}

.action-card p {
    margin: 0;
    color: #75877f;
    font-size: 12px;
    line-height: 1.45;
}

.action-links {
    grid-column: 1 / -1;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    padding-top: 3px;
}

.action-links .button-green {
    margin-top: 0;
    padding: 9px 13px;
    font-size: 11px;
}

.text-link {
    color: #087f5b;
    font-size: 11px;
    font-weight: 800;
}

.analytics-kpis {
    margin-bottom: 24px;
}

.analytics-page-section {
    margin-top: 0;
}

.analytics-note {
    display: flex;
    align-items: flex-start;
    gap: 11px;
    margin-top: 18px;
    padding: 15px 17px;
    border-radius: 14px;
    background: #f4f9f7;
    border: 1px solid #dcebe5;
    color: #557066;
}

.analytics-note > span {
    font-size: 18px;
}

.analytics-note strong {
    color: #2b5144;
    font-size: 13px;
}

.analytics-note p {
    margin: 3px 0 0;
    font-size: 12px;
    line-height: 1.5;
}

@media(max-width: 700px) {
    .navbar { padding: 15px 5%; }
    .nav-right a { margin-left: 10px; font-size: 12px; }
    .hero { padding: 40px 25px; }
    .hero h1 { font-size: 32px; }
    .container { width: 94%; margin: 25px auto; }
    .ai-floating { right: 15px; bottom: 15px; }
    .ai-text { display: none; }
    .dashboard-header,
    .section-heading-row {
        align-items: flex-start;
        flex-direction: column;
    }

    .phc-info-strip {
        grid-template-columns: 1fr;
    }

    .kpi-grid {
        grid-template-columns: repeat(2, 1fr);
    }

    .analytics-grid,
    .admin-actions-grid {
        grid-template-columns: 1fr;
    }

    .chart-card-wide {
        grid-column: auto;
    }

    .chart-wrap,
    .pie-wrap {
        min-height: 270px;
        padding-left: 8px;
        padding-right: 8px;
    }

    .chart-card-heading {
        padding-left: 15px;
        padding-right: 15px;
    }

    .chart-badge {
        display: none;
    }

    .admin-alert-section {
        padding: 17px;
    }

}

</style>
</head>
<body>
<nav class="navbar">
    <div class="logo">\U0001F3E5 PHC Tracker</div>
    <div class="nav-right">
        __NAV_LINKS__
    </div>
</nav>
<div class="container">
__BODY__
</div>
__AI_BUTTON__

<script>
// ============================================================
// SHARED MAP HELPER
// Every page that wants a map drops a <div class="map-box" id="X">
// and calls renderPhcMap("X", markersArray, userMarkerOrNull).
// markersArray items: {name, district, latitude, longitude,
//                       distance_km (optional), allocated (optional)}
// ============================================================
function renderPhcMap(containerId, markers, userPoint) {
    var el = document.getElementById(containerId);
    if (!el || typeof L === "undefined") return null;

    var points = (markers || []).filter(function(m) {
        return m.latitude != null && m.longitude != null;
    });

    var center = [28.6139, 77.2090]; // Delhi fallback
    if (userPoint) { center = [userPoint.lat, userPoint.lon]; }
    else if (points.length) { center = [points[0].latitude, points[0].longitude]; }

    var map = L.map(containerId).setView(center, points.length ? 11 : 10);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        attribution: '&copy; OpenStreetMap contributors'
    }).addTo(map);

    var bounds = [];

    if (userPoint) {
        var youIcon = L.divIcon({
            html: '<div style="background:#2563eb;width:16px;height:16px;border-radius:50%;border:3px solid white;box-shadow:0 0 0 2px #2563eb;"></div>',
            className: '', iconSize: [16, 16]
        });
        L.marker([userPoint.lat, userPoint.lon], {icon: youIcon})
            .addTo(map).bindPopup("You are here");
        bounds.push([userPoint.lat, userPoint.lon]);
    }

    points.forEach(function(m, i) {
        var marker = L.marker([m.latitude, m.longitude]).addTo(map);
        var popup = "<strong>" + m.name + "</strong><br>" + (m.district || "");
        if (m.allocated) { popup += "<br>Collect: <strong>" + m.allocated + "</strong>"; }
        if (m.distance_km != null) { popup += "<br>" + m.distance_km + " km away"; }
        marker.bindPopup(popup);
        if (i === 0 && m.allocated) { marker.openPopup(); }
        bounds.push([m.latitude, m.longitude]);
    });

    if (bounds.length > 1) {
        map.fitBounds(bounds, {padding: [30, 30]});
    }
    return map;
}
</script>
</body>
</html>
"""


def page(title, body, floating_ai=False):
    ai_button = ""
    if floating_ai:
        if session.get("role") == "customer":
            ai_button = """
            <a href="/customer/ai" class="ai-floating" title="Open Coven AI">
                <span class="ai-icon">\u2726</span>
                <span class="ai-text">Coven AI</span>
            </a>
            """
        else:
            ai_button = """
            <a href="/admin/ai" class="ai-floating" title="Open Coven AI">
                <span class="ai-icon">\u2726</span>
                <span class="ai-text">Coven AI</span>
            </a>
            """

    nav_links = ""
    if session.get("role") == "customer":
        nav_links += '<a href="/customer">Dashboard</a>'
        nav_links += '<a href="/customer/near_me">Near Me</a>'
    if session.get("role") == "admin":
        nav_links += '<a href="/admin">Dashboard</a>'
        nav_links += '<a href="/admin/analytics">Analytics</a>'
    if session.get("role"):
        nav_links += '<a href="/logout">Logout</a>'

    html = PAGE_TEMPLATE
    html = html.replace("__TITLE__", title)
    html = html.replace("__NAV_LINKS__", nav_links)
    html = html.replace("__AI_BUTTON__", ai_button)
    html = html.replace("__BODY__", body)

    # render_template_string is used only so Jinja can safely process
    # any {{ }} that might appear inside dynamically-inserted data;
    # our own template no longer relies on Python f-string braces.
    return render_template_string(html)


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():
    body = """
    <section class="cinematic-hero">
        <h1 id="heroHeading"></h1>
        <p>
            A smarter way to discover Primary Health Centres
            and access essential public information across
            the PHC network.
        </p>
        <a href="/signup" class="button">Create Citizen Account</a>
        <a href="/login" class="button" style="margin-left:8px;">Log In</a>
    </section>

    <div class="card-grid">
        <div class="glass-card">
            <div class="card-icon">\U0001F3E5</div>
            <h3>Find PHCs</h3>
            <p>Search Primary Health Centres by name, area, district or facility type.</p>
        </div>
        <div class="glass-card">
            <div class="card-icon">\U0001F4CD</div>
            <h3>Explore PHC Information</h3>
            <p>Find public information about Primary Health Centres across the network, plotted on a map.</p>
        </div>
        <div class="glass-card">
            <div class="card-icon">\U0001F48A</div>
            <h3>Medicine Catalog</h3>
            <p>Browse the public medicine catalog available in the PHC network.</p>
        </div>
        <div class="glass-card">
            <div class="card-icon">\U0001F916</div>
            <h3>Coven AI</h3>
            <p>Ask for exactly what you need. If one PHC can't cover it, Coven AI finds the nearest place for the rest.</p>
        </div>
    </div>

    <script>
    (function() {
        const text = "PRIMARY HEALTH CENTRES TRACKER";
        const words = text.split(" ");
        const heading = document.getElementById("heroHeading");
        words.forEach((word, i) => {
            const span = document.createElement("span");
            span.className = "blur-word";
            span.textContent = word;
            span.style.animationDelay = (i * 0.1) + "s";
            heading.appendChild(span);
        });
    })();
    </script>
    """
    return page("Primary Health Centres Tracker", body)


# ============================================================
# SIGN UP
# ============================================================

@app.route("/signup", methods=["GET", "POST"])
def signup():
    error = None

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        contact = request.form.get("contact", "").strip()
        password = request.form.get("password", "")

        if not name:
            error = "Name cannot be empty."
        elif not (contact.isdigit() and len(contact) == 10):
            error = "Phone number must be exactly 10 digits."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif not any(c.isupper() for c in password):
            error = "Password must include an uppercase letter."
        elif not any(c.islower() for c in password):
            error = "Password must include a lowercase letter."
        elif not any(c.isdigit() for c in password):
            error = "Password must include a digit."

        if not error:
            try:
                # Passwords are hashed with werkzeug's
                # generate_password_hash (PBKDF2) so a database leak
                # doesn't leak plaintext passwords.
                password_hash = generate_password_hash(password)
                cur = con.cursor()
                cur.execute("""
                    INSERT INTO customer (name, password, contact)
                    VALUES (%s, %s, %s)
                """, (name, password_hash, contact))
                con.commit()
                cur.execute("SELECT LAST_INSERT_ID()")
                customer_id = cur.fetchone()[0]
                cur.close()

                body = """
                <div class="card">
                    <div class="card-icon">\U0001F389</div>
                    <h2>Welcome, {name}!</h2>
                    <p>Your citizen account has been successfully created.</p>
                    <div class="success">
                        Your Customer ID is: <strong>{cid}</strong>
                    </div>
                    <a href="/login" class="button-green">Log in now</a>
                </div>
                """.format(name=name, cid=customer_id)
                return page("Account Created", body)

            except sql.Error as e:
                con.rollback()
                error = f"Could not create account: {e}"

    error_html = f'<div class="alert">{error}</div>' if error else ""

    body = f"""
    <div class="card">
        <h2>Create your Citizen Account</h2>
        <p>Register to access public PHC information and healthcare resources.</p>
        {error_html}
        <form method="POST">
            <label>Name</label><br>
            <input name="name" placeholder="Enter your name" required>
            <br>
            <label>Phone Number</label><br>
            <input name="contact" placeholder="10 digit phone number" maxlength="10" required>
            <br>
            <label>Password</label><br>
            <input name="password" type="password" placeholder="Create a password" required>
            <br>
            <button class="button-green" type="submit">Create Account</button>
        </form>
    </div>
    """
    return page("Citizen Sign Up", body)


# ============================================================
# LOGIN
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        user_id = request.form.get("user_id", "").strip()
        pwd = request.form.get("password", "")

        if not user_id.isdigit():
            error = "Please enter a valid numeric ID."
        else:
            user_id = int(user_id)
            cur = con.cursor()

            cur.execute("""
                SELECT admin_id, phc_id, name, password
                FROM admin
                WHERE admin_id=%s
            """, (user_id,))
            admin = cur.fetchone()

            if admin and _verify_password(admin[3], pwd):
                session["role"] = "admin"
                session["admin_name"] = admin[2]
                session["phc_id"] = admin[1]
                cur.close()
                return redirect(url_for("admin_panel"))

            cur.execute("""
                SELECT customer_id, name, password
                FROM customer
                WHERE customer_id=%s
            """, (user_id,))
            cust = cur.fetchone()
            cur.close()

            if cust and _verify_password(cust[2], pwd):
                session["role"] = "customer"
                session["customer_name"] = cust[1]
                return redirect(url_for("customer_panel"))

            error = "Invalid ID or Password!"

    error_html = f'<div class="alert">{error}</div>' if error else ""

    body = f"""
    <div class="card">
        <h2>Welcome Back</h2>
        <p>Log in as a citizen or PHC administrator.</p>
        {error_html}
        <form method="POST">
            <label>ID</label><br>
            <input name="user_id" placeholder="Enter your ID" required>
            <br>
            <label>Password</label><br>
            <input name="password" type="password" placeholder="Enter your password" required>
            <br>
            <button class="button-green" type="submit">Log In</button>
        </form>
    </div>
    """
    return page("Log In", body)


def _verify_password(stored, provided):
    """Accepts hashed passwords (new accounts) and, for backward
    compatibility with rows created before this fix, falls back to a
    plain equality check against any legacy plaintext value."""
    if not stored:
        return False
    try:
        if stored.startswith(("pbkdf2:", "scrypt:")):
            return check_password_hash(stored, provided)
    except Exception:
        pass
    return stored == provided


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# ============================================================
# ADMIN
# ============================================================

def require_admin():
    return session.get("role") == "admin"


@app.route("/admin")
def admin_panel():
    if not require_admin():
        return redirect(url_for("login"))

    phc = get_phc(session["phc_id"])
    warnings = analyze_stock(session["phc_id"])

    warning_html = ""
    if warnings:
        warning_items = ""
        for w in warnings:
            if w["type"] == "OUT_OF_STOCK":
                warning_items += f"""
                <div class="stock-warning out-of-stock">
                    <span class="warning-icon">\U0001F6A8</span>
                    <div>
                        <div class="warning-title">OUT OF STOCK</div>
                        <div class="warning-medicine">{w['medicine']}</div>
                        <div class="warning-details">Current stock: <strong>0 units</strong></div>
                    </div>
                </div>
                """
            elif w["type"] == "LOW_STOCK":
                warning_items += f"""
                <div class="stock-warning low-stock">
                    <span class="warning-icon">\u26A0\uFE0F</span>
                    <div>
                        <div class="warning-title">LOW STOCK</div>
                        <div class="warning-medicine">{w['medicine']}</div>
                        <div class="warning-details">Current stock: <strong>{w['quantity']} units</strong></div>
                    </div>
                </div>
                """

        warning_html = f"""
        <div class="admin-alert-section">
            <h2 class="alert-heading">\u26A0\uFE0F STOCK ALERTS</h2>
            <p class="alert-subtitle">Immediate attention required for the following medicines:</p>
            <div class="warning-container">{warning_items}</div>
            <a href="/admin/warnings" class="view-all-warnings">View All Stock Warnings \u2192</a>
        </div>
        """
    else:
        warning_html = """
        <div class="admin-alert-section no-alerts">
            <div class="success-icon">\u2713</div>
            <div>
                <h2>No Stock Alerts</h2>
                <p>All medicines at your PHC currently have sufficient stock.</p>
            </div>
        </div>
        """

    map_locations = [{
        "name": phc["name"], "district": phc["district"],
        "latitude": phc["latitude"], "longitude": phc["longitude"]
    }]

    body = f"""
    <div class="glass-card">
        <h1>Admin Dashboard</h1>
        <div class="card">
            <div class="card-icon">\U0001F3E5</div>
            <h2>{phc['name']}</h2>
            <p><b>District:</b> {phc['district']}</p>
            <p><b>Facility Type:</b> {phc['facility_type']}</p>
        </div>

        <div class="map-box small" id="adminPhcMap"></div>

        {warning_html}

        <br>

        <div class="card-grid">
            <div class="card">
                <div class="card-icon">\U0001F6CF\uFE0F</div>
                <h3>Bed Management</h3>
                <a class="button" href="/admin/beds">View Beds</a>
                <br><br>
                <a class="button" href="/admin/update_beds">Update Beds</a>
            </div>
            <div class="card">
                <div class="card-icon">\U0001F48A</div>
                <h3>Medicine Stock</h3>
                <a class="button" href="/admin/stock">View Stock</a>
                <br><br>
                <a class="button" href="/admin/update_stock">Update Stock</a>
            </div>
            <div class="card">
                <div class="card-icon">\u26A0\uFE0F</div>
                <h3>Stock Warnings</h3>
                <a class="button" href="/admin/warnings">View Warnings</a>
            </div>
            <div class="card">
                <div class="card-icon">\U0001F4CB</div>
                <h3>Resource Usage</h3>
                <a class="button" href="/admin/mark_beds">Beds Used</a>
                <br><br>
                <a class="button" href="/admin/mark_medicine">Medicine Used</a>
            </div>
            <div class="card">
                <div class="card-icon">\U0001F4CA</div>
                <h3>Resource Analytics</h3>
                <p>View medicine stock, quantities, and bed availability graphs.</p>
                <a class="button-green" href="/admin/analytics">View Graphs</a>
            </div>
        </div>
    </div>

    <script>
    document.addEventListener("DOMContentLoaded", function() {{
        renderPhcMap("adminPhcMap", {json.dumps(map_locations)}, null);
    }});
    </script>
    """
    return page("Admin Dashboard", body, floating_ai=True)


@app.route("/admin/beds")
def admin_view_beds():
    if not require_admin():
        return redirect(url_for("login"))

    beds = get_beds(session["phc_id"])
    rows = "".join(
        f"<tr><td>{b['ward_type'].upper()}</td><td>{b['total_beds']}</td><td>{b['available_beds']}</td></tr>"
        for b in beds
    )
    body = f"""
    <table>
        <tr><th>Ward</th><th>Total Beds</th><th>Available</th></tr>
        {rows}
    </table>
    <a class="back" href="/admin">\u2190 Back to Dashboard</a>
    """
    return page("My PHC Beds", body, floating_ai=True)


@app.route("/admin/stock")
def admin_view_stock():
    if not require_admin():
        return redirect(url_for("login"))

    stock = get_stock(session["phc_id"])
    rows = "".join(
        f"<tr><td>{s['medicine_name']}</td><td>{s['category']}</td>"
        f"<td>{s['quantity_available']}</td><td>{s['expiry_date']}</td></tr>"
        for s in stock
    )
    body = f"""
    <table>
        <tr><th>Medicine</th><th>Category</th><th>Quantity</th><th>Expiry</th></tr>
        {rows}
    </table>
    <a class="back" href="/admin">\u2190 Back to Dashboard</a>
    """
    return page("Medicine Stock", body, floating_ai=True)


@app.route("/admin/warnings")
def admin_warnings():
    if not require_admin():
        return redirect(url_for("login"))

    warnings = analyze_stock(session["phc_id"])

    if not warnings:
        body = '<div class="success">\U0001F7E2 No low-stock or out-of-stock medicines detected.</div>'
    else:
        items = ""
        for w in warnings:
            icon = "\U0001F534" if w["type"] == "OUT_OF_STOCK" else "\U0001F7E0"
            items += f"""
            <div class="card">
                <h3>{icon} {w["type"]}</h3>
                <p>Medicine: <strong>{w["medicine"]}</strong></p>
                <p>Quantity: {w["quantity"]} units</p>
            </div>
            """
        body = f'<div class="card-grid">{items}</div>'

    body += '<a class="back" href="/admin">\u2190 Back to Dashboard</a>'
    return page("Stock Warnings", body, floating_ai=True)


@app.route("/admin/update_beds", methods=["GET", "POST"])
def admin_update_beds():
    if not require_admin():
        return redirect(url_for("login"))

    phc_id = session["phc_id"]
    message = None

    if request.method == "POST":
        ward = request.form.get("ward", "").lower()
        available = request.form.get("available", "")

        if ward not in ("general", "icu", "oxygen") or not available.isdigit():
            message = "Invalid input."
        else:
            available = int(available)
            cur = con.cursor()
            cur.execute("SELECT total_beds FROM beds WHERE phc_id=%s AND ward_type=%s", (phc_id, ward))
            row = cur.fetchone()

            if not row:
                message = "Ward not found."
            elif available < 0 or available > row[0]:
                message = f"Available beds must be between 0 and {row[0]}."
            else:
                cur.execute("""
                    UPDATE beds SET available_beds=%s, last_updated=NOW()
                    WHERE phc_id=%s AND ward_type=%s
                """, (available, phc_id, ward))
                con.commit()
                message = "Bed availability updated successfully!"
            cur.close()

    msg_html = f'<div class="success">{message}</div>' if message else ""

    body = f"""
    {msg_html}
    <form method="POST">
        <label>Ward Type</label><br>
        <select name="ward">
            <option value="general">General</option>
            <option value="icu">ICU</option>
            <option value="oxygen">Oxygen</option>
        </select>
        <br>
        <label>New Available Count</label><br>
        <input name="available" type="number" min="0" required>
        <br>
        <button class="button-green" type="submit">Update Beds</button>
    </form>
    <a class="back" href="/admin">\u2190 Back to Dashboard</a>
    """
    return page("Update Bed Availability", body, floating_ai=True)


@app.route("/admin/update_stock", methods=["GET", "POST"])
def admin_update_stock():
    if not require_admin():
        return redirect(url_for("login"))

    phc_id = session["phc_id"]
    stock = get_stock(phc_id)
    message = None

    if request.method == "POST":
        medicine_id = request.form.get("medicine_id", "")
        quantity = request.form.get("quantity", "")

        if not medicine_id.isdigit() or not quantity.isdigit():
            message = "Invalid input."
        else:
            medicine_id = int(medicine_id)
            quantity = int(quantity)
            cur = con.cursor()

            cur.execute("""
                SELECT quantity_available FROM phc_medicine_stock
                WHERE phc_id=%s AND medicine_id=%s
            """, (phc_id, medicine_id))

            if not cur.fetchone():
                message = "No stock record for that medicine at your PHC."
            else:
                cur.execute("""
                    UPDATE phc_medicine_stock SET quantity_available=%s, last_updated=NOW()
                    WHERE phc_id=%s AND medicine_id=%s
                """, (quantity, phc_id, medicine_id))
                con.commit()
                message = "Stock updated successfully!"
            cur.close()

            stock = get_stock(phc_id)

    rows = "".join(
        f"<li><strong>{s['medicine_name']}</strong> \u2014 ID {s['medicine_id']} \u2014 Current: {s['quantity_available']}</li>"
        for s in stock
    )
    msg_html = f'<div class="success">{message}</div>' if message else ""

    body = f"""
    <div class="card">
        <h3>Current Medicines</h3>
        <ul>{rows}</ul>
    </div>
    {msg_html}
    <form method="POST">
        <label>Medicine ID</label><br>
        <input name="medicine_id" type="number" required>
        <br>
        <label>New Quantity</label><br>
        <input name="quantity" type="number" min="0" required>
        <br>
        <button class="button-green" type="submit">Update Stock</button>
    </form>
    <a class="back" href="/admin">\u2190 Back to Dashboard</a>
    """
    return page("Update Medicine Stock", body, floating_ai=True)


@app.route("/admin/mark_beds", methods=["GET", "POST"])
def admin_mark_beds():
    if not require_admin():
        return redirect(url_for("login"))

    phc_id = session["phc_id"]
    message = None

    if request.method == "POST":
        ward = request.form.get("ward", "").lower()
        used = request.form.get("used", "")

        if ward not in ("general", "icu", "oxygen") or not used.isdigit():
            message = "Invalid input."
        else:
            used = int(used)
            cur = con.cursor()
            cur.execute("SELECT available_beds FROM beds WHERE phc_id=%s AND ward_type=%s", (phc_id, ward))
            row = cur.fetchone()

            if not row:
                message = "Ward not found."
            elif used > row[0]:
                message = f"Only {row[0]} beds available."
            else:
                new_available = row[0] - used
                cur.execute("""
                    UPDATE beds SET available_beds=%s, last_updated=NOW()
                    WHERE phc_id=%s AND ward_type=%s
                """, (new_available, phc_id, ward))
                con.commit()
                message = f"{used} bed(s) marked as used. New available: {new_available}"
            cur.close()

    msg_html = f'<div class="success">{message}</div>' if message else ""

    body = f"""
    {msg_html}
    <form method="POST">
        <label>Ward Type</label><br>
        <select name="ward">
            <option value="general">General</option>
            <option value="icu">ICU</option>
            <option value="oxygen">Oxygen</option>
        </select>
        <br>
        <label>Beds Used</label><br>
        <input name="used" type="number" min="0" required>
        <br>
        <button class="button-green" type="submit">Submit</button>
    </form>
    <a class="back" href="/admin">\u2190 Back to Dashboard</a>
    """
    return page("Mark Beds as Used", body, floating_ai=True)


@app.route("/admin/mark_medicine", methods=["GET", "POST"])
def admin_mark_medicine():
    if not require_admin():
        return redirect(url_for("login"))

    phc_id = session["phc_id"]
    stock = get_stock(phc_id)
    message = None

    if request.method == "POST":
        medicine_id = request.form.get("medicine_id", "")
        used = request.form.get("used", "")

        if not medicine_id.isdigit() or not used.isdigit():
            message = "Invalid input."
        else:
            medicine_id = int(medicine_id)
            used = int(used)
            cur = con.cursor()

            cur.execute("""
                SELECT quantity_available FROM phc_medicine_stock
                WHERE phc_id=%s AND medicine_id=%s
            """, (phc_id, medicine_id))
            row = cur.fetchone()

            if not row:
                message = "No stock record for that medicine."
            elif used > row[0]:
                message = f"Only {row[0]} units available."
            else:
                new_qty = row[0] - used
                cur.execute("""
                    UPDATE phc_medicine_stock SET quantity_available=%s, last_updated=NOW()
                    WHERE phc_id=%s AND medicine_id=%s
                """, (new_qty, phc_id, medicine_id))

                # Record the usage event for predictive modelling.
                cur.execute("""
                    INSERT INTO medicine_usage_history (phc_id, medicine_id, units_used)
                    VALUES (%s, %s, %s)
                """, (phc_id, medicine_id, used))

                con.commit()
                message = f"{used} unit(s) marked as used. New stock: {new_qty}"
            cur.close()

            stock = get_stock(phc_id)

    rows = "".join(
        f"<li><strong>{s['medicine_name']}</strong> \u2014 ID {s['medicine_id']} \u2014 Current: {s['quantity_available']}</li>"
        for s in stock
    )
    msg_html = f'<div class="success">{message}</div>' if message else ""

    body = f"""
    <div class="card">
        <h3>Current Medicines</h3>
        <ul>{rows}</ul>
    </div>
    {msg_html}
    <form method="POST">
        <label>Medicine ID</label><br>
        <input name="medicine_id" type="number" required>
        <br>
        <label>Units Used</label><br>
        <input name="used" type="number" min="0" required>
        <br>
        <button class="button-green" type="submit">Submit</button>
    </form>
    <a class="back" href="/admin">\u2190 Back to Dashboard</a>
    """
    return page("Mark Medicine as Used", body, floating_ai=True)


# ============================================================
# ADMIN AI (with voice assistant + predictive analytics)
# ============================================================

@app.route("/admin/ai", methods=["GET", "POST"])
def admin_ai():
    if not require_admin():
        return redirect(url_for("login"))

    answer = None

    if request.method == "POST":
        question = request.form.get("question", "").strip()
        if question:
            answer = ask_gemini(question, admin_phc_id=session["phc_id"])

    answer_html = ""
    if answer:
        answer_html = """
        <div class="card">
            <div class="card-icon">\U0001F916</div>
            <h3>Coven AI</h3>
            <p>ANSWER_PLACEHOLDER</p>
        </div>
        """
        answer_html = answer_html.replace("ANSWER_PLACEHOLDER", answer)

    body = """
    <div class="hero" style="padding:35px;">
        <h1 style="font-size:34px;">Coven AI</h1>
        <p>
            Ask Coven AI about your PHC's medicine stock,
            urgent shortages, beds, resources and predicted
            stockout risk.
        </p>
    </div>

    ANSWER_HTML_PLACEHOLDER

    <div style="margin-top:25px; padding:25px; background:rgba(255,255,255,0.04); border-radius:18px;">

        <label style="display:block; margin-bottom:10px; font-weight:600;">Ask Coven AI</label>

        <textarea id="covenQuestion"
            placeholder="Ask Coven AI about your PHC's medicine stock, urgent shortages, beds, or resources..."
            rows="4"
            style="width:100%; padding:15px; border-radius:12px; border:1px solid rgba(255,255,255,0.15);
                   font-size:15px; resize:none; background:white; color:#111827;">
        </textarea>

        <div style="display:flex; gap:10px; flex-wrap:wrap; margin-top:15px;">
            <button type="button" id="micButton" class="button-green" onclick="startCovenListening()">\U0001F3A4 Speak</button>
            <button type="button" onclick="stopCovenListening()"
                style="padding:10px 18px; border:none; border-radius:8px; cursor:pointer;">\u23F9 Stop</button>
            <button type="button" onclick="askCovenVoice()"
                style="padding:10px 18px; border:none; border-radius:8px; cursor:pointer;
                       background:#2563eb; color:white; font-weight:600;">\U0001F916 Ask Coven</button>
            <button type="button" onclick="stopCovenSpeaking()"
                style="padding:10px 18px; border:none; border-radius:8px; cursor:pointer;
                       background:#dc2626; color:white; font-weight:600;">\U0001F507 Stop Voice</button>
        </div>

        <div id="covenListeningStatus" style="display:none; margin-top:12px; color:#60a5fa; font-size:14px;">
            \U0001F3A4 Listening...
        </div>

        <div id="covenVoiceResponse" style="margin-top:20px; padding:18px; border-radius:12px;
             background:#111827; color:white; line-height:1.8; white-space:pre-line;">
            Coven AI's response will appear here.
        </div>

    </div>

    <div style="margin-top:25px; padding:25px; background:rgba(255,255,255,0.04); border-radius:18px;">

        <h2 style="margin-bottom:8px;">\U0001F4C8 Predictive Stock Analytics</h2>
        <p style="opacity:0.75; margin-bottom:20px;">
            Coven estimates how many days each medicine may
            last using the PHC's recent usage history.
        </p>

        <button type="button" onclick="loadPredictions()"
            style="padding:10px 18px; border:none; border-radius:8px; cursor:pointer;
                   background:#087f5b; color:white; font-weight:600;">\U0001F52E Run Prediction</button>

        <div id="predictionStatus" style="margin-top:15px; opacity:0.8;"></div>
        <div id="predictionResults" style="margin-top:20px;"></div>

    </div>

    <script>

    let covenRecognition = null;
    let covenListening = false;

    const CovenSpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

    if (CovenSpeechRecognition) {

        covenRecognition = new CovenSpeechRecognition();
        covenRecognition.continuous = false;
        covenRecognition.interimResults = false;
        covenRecognition.lang = "en-IN";

        covenRecognition.onstart = function() {
            covenListening = true;
            const status = document.getElementById("covenListeningStatus");
            const button = document.getElementById("micButton");
            if (status) status.style.display = "block";
            if (button) button.innerText = "\U0001F399\uFE0F Listening...";
        };

        covenRecognition.onresult = function(event) {
            const transcript = event.results[0][0].transcript;
            const question = document.getElementById("covenQuestion");
            if (question) question.value = transcript;
        };

        covenRecognition.onerror = function(event) {
            console.error("Speech recognition error:", event.error);
            covenListening = false;
            const status = document.getElementById("covenListeningStatus");
            const button = document.getElementById("micButton");
            if (status) status.style.display = "none";
            if (button) button.innerText = "\U0001F3A4 Speak";
        };

        covenRecognition.onend = function() {
            covenListening = false;
            const status = document.getElementById("covenListeningStatus");
            const button = document.getElementById("micButton");
            if (status) status.style.display = "none";
            if (button) button.innerText = "\U0001F3A4 Speak";
        };
    }

    function startCovenListening() {
        if (!covenRecognition) {
            alert("Speech recognition is not supported. Please use Google Chrome.");
            return;
        }
        if (covenListening) return;
        try { covenRecognition.start(); } catch (error) { console.error(error); }
    }

    function stopCovenListening() {
        if (covenRecognition && covenListening) covenRecognition.stop();
    }

    async function askCovenVoice() {
        const question = document.getElementById("covenQuestion").value.trim();
        if (!question) { alert("Please type a question or use the microphone."); return; }

        const responseBox = document.getElementById("covenVoiceResponse");
        responseBox.innerText = "Coven AI is thinking...";

        try {
            const response = await fetch("/api/coven", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ question: question })
            });
            const data = await response.json();
            if (data.error) { responseBox.innerText = data.error; return; }
            responseBox.innerText = data.response;
            speakCovenResponse(data.response);
        } catch (error) {
            console.error(error);
            responseBox.innerText = "Unable to connect to Coven AI.";
        }
    }

    function speakCovenResponse(text) {
        if (!("speechSynthesis" in window)) {
            alert("Text-to-speech is not supported in this browser.");
            return;
        }
        window.speechSynthesis.cancel();
        const speech = new SpeechSynthesisUtterance(text);
        speech.lang = "en-IN";
        speech.rate = 0.9;
        speech.pitch = 1.0;
        speech.volume = 1.0;
        window.speechSynthesis.speak(speech);
    }

    function stopCovenSpeaking() {
        if ("speechSynthesis" in window) window.speechSynthesis.cancel();
    }

    async function loadPredictions() {
        const status = document.getElementById("predictionStatus");
        const results = document.getElementById("predictionResults");
        status.innerText = "Coven is analysing recent medicine usage...";
        results.innerHTML = "";

        try {
            const response = await fetch("/api/predictive-stock");
            const data = await response.json();

            if (data.error) { status.innerText = data.error; return; }

            status.innerText = "Prediction completed.";

            if (!data.predictions || data.predictions.length === 0) {
                results.innerHTML = "<p>No prediction data available yet. Mark medicine as used to build usage history.</p>";
                return;
            }

            data.predictions.forEach(function(item) {
                let statusColor = "#087f5b";
                if (item.prediction_status === "CRITICAL" || item.prediction_status === "OUT_OF_STOCK") {
                    statusColor = "#dc2626";
                } else if (item.prediction_status === "WARNING") {
                    statusColor = "#d97706";
                } else if (item.prediction_status === "MONITOR") {
                    statusColor = "#2563eb";
                }

                let daysText = "No usage history";
                if (item.predicted_days !== null) daysText = item.predicted_days + " days";

                const card = document.createElement("div");
                card.style.cssText =
                    "background:white;color:#111827;border-radius:12px;padding:18px;" +
                    "margin-bottom:12px;border-left:5px solid " + statusColor + ";";

                card.innerHTML =
                    "<div style='display:flex;justify-content:space-between;gap:15px;flex-wrap:wrap;'>" +
                    "<strong style='font-size:17px;'>" + item.medicine_name + "</strong>" +
                    "<strong style='color:" + statusColor + ";'>" + item.prediction_status + "</strong>" +
                    "</div>" +
                    "<div style='margin-top:10px;line-height:1.8;font-size:14px;'>" +
                    "Current stock: <strong>" + item.current_stock + "</strong> units<br>" +
                    "Used in last 30 days: <strong>" + item.used_30_days + "</strong> units<br>" +
                    "Average daily usage: <strong>" + item.average_daily_usage + "</strong> units/day<br>" +
                    "Predicted stockout: <strong>" + daysText + "</strong>" +
                    "</div>";

                results.appendChild(card);
            });

        } catch (error) {
            console.error(error);
            status.innerText = "Unable to run predictive model.";
        }
    }

    </script>

    <a class="back" href="/admin">\u2190 Back to Dashboard</a>
    """

    body = body.replace("ANSWER_HTML_PLACEHOLDER", answer_html)

    return page("Coven AI Network Assistant", body, floating_ai=True)


# ============================================================
# CITIZEN
# ============================================================

def require_customer():
    return session.get("role") == "customer"


@app.route("/customer")
def customer_panel():
    if not require_customer():
        return redirect(url_for("login"))

    body = """
    <div class="hero" style="padding:35px;">
        <h1 style="font-size:34px;">Welcome, __NAME__</h1>
        <p>Explore Primary Health Centres and public healthcare information.</p>
    </div>

    <ul class="menu">
        <li><a href="/customer/phcs">\U0001F3E5 View All PHCs</a></li>
        <li><a href="/customer/search">\U0001F50E Search PHC</a></li>
        <li><a href="/customer/near_me">\U0001F4CD PHCs Near Me</a></li>
        <li><a href="/customer/catalog">\U0001F48A Medicine Catalog</a></li>
        <li><a href="/customer/ai">\U0001F916 Coven AI Assistant</a></li>
    </ul>
    """.replace("__NAME__", session["customer_name"])
    return page("Citizen Dashboard", body, floating_ai=True)


@app.route("/customer/phcs")
def customer_phcs():
    if not require_customer():
        return redirect(url_for("login"))

    phcs = get_all_phcs()
    cards = ""
    for p in phcs:
        cards += f"""
        <div class="card phc-card">
            <div class="phc-card-info">
                <h3>\U0001F3E5 {p['name']}</h3>
                <p>\U0001F4CD {p['district']}</p>
                <p>\U0001F3E2 {p['facility_type']}</p>
            </div>
            <a href="/customer/phc/{p['phc_id']}" class="button-green">View</a>
        </div>
        """

    map_locations = [
        {"name": p["name"], "district": p["district"],
         "latitude": p["latitude"], "longitude": p["longitude"]}
        for p in phcs
    ]

    body = f"""
    <div class="map-box" id="allPhcsMap"></div>
    <div class="card-grid">{cards}</div>
    <a class="back" href="/customer">\u2190 Back to Dashboard</a>

    <script>
    document.addEventListener("DOMContentLoaded", function() {{
        renderPhcMap("allPhcsMap", {json.dumps(map_locations)}, null);
    }});
    </script>
    """
    return page("All Primary Health Centres", body, floating_ai=True)


@app.route("/customer/phc/<int:phc_id>")
def customer_phc_details(phc_id):
    if not require_customer():
        return redirect(url_for("login"))

    phc = get_phc(phc_id)
    if not phc:
        return page("PHC Not Found", '<div class="alert">PHC not found.</div>')

    map_locations = [{
        "name": phc["name"], "district": phc["district"],
        "latitude": phc["latitude"], "longitude": phc["longitude"]
    }]

    body = f"""
    <div class="hero" style="padding:40px;">
        <h1 style="font-size:36px;">{phc['name']}</h1>
        <p>\U0001F4CD {phc['district']}</p>
        <p>\U0001F3E2 {phc['facility_type']}</p>
    </div>

    <div class="map-box small" id="onePhcMap"></div>

    <div class="card-grid">
        <div class="card">
            <div class="card-icon">\U0001F3E5</div>
            <h3>Primary Health Centre</h3>
            <p>This is a registered Primary Health Centre in the PHC network.</p>
        </div>
        <div class="card">
            <div class="card-icon">\U0001F4CD</div>
            <h3>Location</h3>
            <p>District: <strong>{phc['district']}</strong></p>
        </div>
        <div class="card">
            <div class="card-icon">\U0001F3E2</div>
            <h3>Facility Type</h3>
            <p><strong>{phc['facility_type']}</strong></p>
        </div>
    </div>

    <div class="success">
        \U0001F512 Current bed and medicine stock information is restricted to authorised PHC staff, but Coven AI can still tell you whether your specific need can be met and where.
    </div>

    <a class="back" href="/customer/phcs">\u2190 Back to PHCs</a>

    <script>
    document.addEventListener("DOMContentLoaded", function() {{
        renderPhcMap("onePhcMap", {json.dumps(map_locations)}, null);
    }});
    </script>
    """
    return page(phc["name"], body, floating_ai=True)


@app.route("/customer/search", methods=["GET", "POST"])
def customer_search():
    if not require_customer():
        return redirect(url_for("login"))

    results_html = ""
    keyword = ""

    if request.method == "POST":
        keyword = request.form.get("keyword", "").strip()

        if keyword:
            search = f"%{keyword}%"
            cur = con.cursor()
            cur.execute("""
                SELECT phc_id, name, district, facility_type
                FROM phcs
                WHERE name LIKE %s OR district LIKE %s OR facility_type LIKE %s
                ORDER BY district, name
            """, (search, search, search))
            results = cur.fetchall()
            cur.close()

            if not results:
                results_html = '<div class="alert">\u274C No matching PHCs found.</div>'
            else:
                cards = ""
                for r in results:
                    cards += f"""
                    <div class="card">
                        <h3>\U0001F3E5 {r[1]}</h3>
                        <p>\U0001F4CD District: <strong>{r[2]}</strong></p>
                        <p>\U0001F3E2 Facility Type: <strong>{r[3]}</strong></p>
                        <a href="/customer/phc/{r[0]}" class="button-green">View PHC</a>
                    </div>
                    """
                results_html = f'<div class="card-grid">{cards}</div>'

    body = f"""
    <div class="card search-box">
        <div class="card-icon">\U0001F50E</div>
        <h2>Search Primary Health Centres</h2>
        <p>Search using a PHC name, area, district or facility type.</p>
        <form method="POST">
            <input name="keyword" value="{keyword}" placeholder="e.g. Delhi, General Hospital, PHC..." required>
            <br>
            <button class="button-green" type="submit">Search PHCs</button>
        </form>
    </div>

    {results_html}

    <a class="back" href="/customer">\u2190 Back to Dashboard</a>
    """
    return page("Search PHC", body, floating_ai=True)


@app.route("/customer/catalog")
def customer_catalog():
    if not require_customer():
        return redirect(url_for("login"))

    catalog = get_medicine_catalog()
    rows = "".join(
        f"<tr><td>{m['medicine_id']}</td><td>{m['medicine_name']}</td><td>{m['category']}</td></tr>"
        for m in catalog
    )
    body = f"""
    <table>
        <tr><th>ID</th><th>Medicine</th><th>Category</th></tr>
        {rows}
    </table>
    <a class="back" href="/customer">\u2190 Back to Dashboard</a>
    """
    return page("Medicine Catalog", body, floating_ai=True)


@app.route("/customer/near_me")
def customer_near_me():
    if not require_customer():
        return redirect(url_for("login"))

    body = """
    <div class="hero" style="padding:35px;">
        <h1 style="font-size:34px;">PHCs Near Me</h1>
        <p>Share your location and we'll sort every PHC in the network by distance.</p>
    </div>

    <div id="nearMeStatus" class="success">Requesting your location...</div>
    <div class="map-box" id="nearMeMap"></div>
    <div class="card-grid" id="nearMeResults"></div>

    <a class="back" href="/customer">\u2190 Back to Dashboard</a>

    <script>
    async function loadNearby(lat, lon) {
        const status = document.getElementById("nearMeStatus");
        const results = document.getElementById("nearMeResults");
        try {
            const url = (lat != null)
                ? "/api/nearby-phcs?lat=" + lat + "&lon=" + lon
                : "/api/nearby-phcs";
            const response = await fetch(url);
            const data = await response.json();
            if (data.error) { status.innerText = data.error; return; }

            status.innerText = (lat != null)
                ? "Showing PHCs sorted by distance from you."
                : "Location unavailable -- showing all PHCs (allow location access for distances).";

            renderPhcMap("nearMeMap", data.phcs, (lat != null) ? {lat: lat, lon: lon} : null);

            results.innerHTML = "";
            data.phcs.forEach(function(p) {
                const dist = (p.distance_km != null) ? (p.distance_km + " km away") : "";
                const div = document.createElement("div");
                div.className = "card";
                div.innerHTML =
                    "<h3>\\uD83C\\uDFE5 " + p.name + "</h3>" +
                    "<p>\\uD83D\\uDCCD " + p.district + (dist ? " <span class='distance-pill'>" + dist + "</span>" : "") + "</p>" +
                    "<p>\\uD83C\\uDFE2 " + p.facility_type + "</p>" +
                    "<a href='/customer/phc/" + p.phc_id + "' class='button-green'>View</a>";
                results.appendChild(div);
            });
        } catch (error) {
            console.error(error);
            status.innerText = "Unable to load nearby PHCs.";
        }
    }

    if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(
            function(pos) { loadNearby(pos.coords.latitude, pos.coords.longitude); },
            function(err) { console.warn(err); loadNearby(null, null); },
            { timeout: 8000 }
        );
    } else {
        loadNearby(null, null);
    }
    </script>
    """
    return page("PHCs Near Me", body, floating_ai=True)


# ============================================================
# CITIZEN AI (form page + voice widget + map)
# ============================================================

@app.route("/customer/ai", methods=["GET", "POST"])
def customer_ai():
    if not require_customer():
        return redirect(url_for("login"))

    answer_text = None
    answer_locations = []

    if request.method == "POST":
        question = request.form.get("question", "").strip()
        lat_raw = request.form.get("lat", "").strip()
        lon_raw = request.form.get("lon", "").strip()
        user_lat = float(lat_raw) if lat_raw else None
        user_lon = float(lon_raw) if lon_raw else None

        if question:
            result = get_citizen_answer(question, user_lat, user_lon)
            answer_text = result["text"]
            answer_locations = result["locations"]

    answer_html = ""
    if answer_text:
        answer_html = """
        <div class="card ai-response-card">
            <div class="card-icon">\U0001F916</div>
            <h3>Coven AI</h3>
            <div class="ai-response">ANSWER_PLACEHOLDER</div>
        </div>
        STOPS_PLACEHOLDER
        <div class="map-box small" id="aiResultMap"></div>
        """
        answer_html = answer_html.replace("ANSWER_PLACEHOLDER", answer_text)
        answer_html = answer_html.replace("STOPS_PLACEHOLDER", _locations_to_html(answer_locations))

    body = """
    <div class="hero" style="padding:35px;">
        <h1 style="font-size:34px;">Coven AI</h1>
        <p>
            Tell me what bed or medicine you need -- including how
            many -- and I'll check the whole network. If one PHC
            can't cover it all, I'll point you to the nearest place
            for the rest.
        </p>
    </div>

    ANSWER_HTML_PLACEHOLDER

    <div style="margin-top:25px; padding:25px; background:rgba(255,255,255,0.04); border-radius:18px;">

        <label style="display:block; margin-bottom:10px; font-weight:600;">What do you need?</label>

        <textarea id="citizenQuestion"
            placeholder="e.g. I need 90 units of Paracetamol near Rohini"
            rows="4"
            style="width:100%; padding:15px; border-radius:12px; border:1px solid rgba(255,255,255,0.15);
                   font-size:15px; resize:none; background:white; color:#111827;">
        </textarea>

        <div id="citizenLocationStatus" style="margin-top:8px; font-size:13px; opacity:0.7;">
            \U0001F4CD Trying to use your location for nearest-first results...
        </div>

        <div style="display:flex; gap:10px; flex-wrap:wrap; margin-top:15px;">
            <button type="button" id="citizenMicButton" class="button-green" onclick="startCitizenListening()">\U0001F3A4 Speak</button>
            <button type="button" onclick="stopCitizenListening()"
                style="padding:10px 18px; border:none; border-radius:8px; cursor:pointer;">\u23F9 Stop</button>
            <button type="button" onclick="askCitizenVoice()"
                style="padding:10px 18px; border:none; border-radius:8px; cursor:pointer;
                       background:#2563eb; color:white; font-weight:600;">\U0001F916 Check Availability</button>
            <button type="button" onclick="stopCitizenSpeaking()"
                style="padding:10px 18px; border:none; border-radius:8px; cursor:pointer;
                       background:#dc2626; color:white; font-weight:600;">\U0001F507 Stop Voice</button>
        </div>

        <div id="citizenListeningStatus" style="display:none; margin-top:12px; color:#60a5fa; font-size:14px;">
            \U0001F3A4 Listening...
        </div>

        <div id="citizenVoiceResponse" style="margin-top:20px; padding:18px; border-radius:12px;
             background:#111827; color:white; line-height:1.8; white-space:pre-line;">
            Coven AI's response will appear here.
        </div>

        <div class="map-box small" id="citizenVoiceMap" style="display:none;"></div>

    </div>

    <script>

    let citizenUserLat = null;
    let citizenUserLon = null;
    let citizenLocationPromise = null;

    // Returns a Promise that resolves once we know the user's
    // coordinates (or know we can't get them). Safe to call more
    // than once -- later callers just get the same in-flight/settled
    // promise, so a fast click right after page load still waits for
    // the real answer instead of firing off with lat/lon still null.
    function ensureLocation() {
        if (citizenUserLat != null) {
            return Promise.resolve({ lat: citizenUserLat, lon: citizenUserLon });
        }
        if (citizenLocationPromise) return citizenLocationPromise;

        citizenLocationPromise = new Promise(function(resolve) {
            if (!navigator.geolocation) {
                document.getElementById("citizenLocationStatus").innerText = "";
                resolve({ lat: null, lon: null });
                return;
            }
            document.getElementById("citizenLocationStatus").innerText =
                "\\uD83D\\uDCCD Waiting for location permission...";
            navigator.geolocation.getCurrentPosition(
                function(pos) {
                    citizenUserLat = pos.coords.latitude;
                    citizenUserLon = pos.coords.longitude;
                    document.getElementById("citizenLocationStatus").innerText =
                        "\\uD83D\\uDCCD Using your location for nearest-first results.";
                    document.getElementById("citizenLat").value = citizenUserLat;
                    document.getElementById("citizenLon").value = citizenUserLon;
                    resolve({ lat: citizenUserLat, lon: citizenUserLon });
                },
                function(err) {
                    document.getElementById("citizenLocationStatus").innerText =
                        "\\uD83D\\uDCCD Location unavailable -- results will use district name from your question instead.";
                    resolve({ lat: null, lon: null });
                },
                { timeout: 8000 }
            );
        });
        return citizenLocationPromise;
    }

    // Kick it off as soon as the page loads, so it's usually already
    // resolved by the time you finish typing.
    ensureLocation();

    // If the plain (non-JS) form gets submitted before location has
    // resolved, hold it for a moment and fill in the hidden lat/lon
    // fields first, instead of silently sending the request with no
    // coordinates.
    document.addEventListener("DOMContentLoaded", function() {
        var plainForm = document.getElementById("citizenForm");
        if (!plainForm) return;
        plainForm.addEventListener("submit", function(e) {
            if (citizenUserLat != null) return; // already filled in, submit normally
            e.preventDefault();
            var formEl = this;
            ensureLocation().then(function() { formEl.submit(); });
        });
    });

    let citizenRecognition = null;
    let citizenListening = false;

    const CitizenSpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

    if (CitizenSpeechRecognition) {

        citizenRecognition = new CitizenSpeechRecognition();
        citizenRecognition.continuous = false;
        citizenRecognition.interimResults = false;
        citizenRecognition.lang = "en-IN";

        citizenRecognition.onstart = function() {
            citizenListening = true;
            const status = document.getElementById("citizenListeningStatus");
            const button = document.getElementById("citizenMicButton");
            if (status) status.style.display = "block";
            if (button) button.innerText = "\U0001F399\uFE0F Listening...";
        };

        citizenRecognition.onresult = function(event) {
            const transcript = event.results[0][0].transcript;
            const question = document.getElementById("citizenQuestion");
            if (question) question.value = transcript;
        };

        citizenRecognition.onerror = function(event) {
            console.error("Speech recognition error:", event.error);
            citizenListening = false;
            const status = document.getElementById("citizenListeningStatus");
            const button = document.getElementById("citizenMicButton");
            if (status) status.style.display = "none";
            if (button) button.innerText = "\U0001F3A4 Speak";
        };

        citizenRecognition.onend = function() {
            citizenListening = false;
            const status = document.getElementById("citizenListeningStatus");
            const button = document.getElementById("citizenMicButton");
            if (status) status.style.display = "none";
            if (button) button.innerText = "\U0001F3A4 Speak";
        };
    }

    function startCitizenListening() {
        if (!citizenRecognition) {
            alert("Speech recognition is not supported. Please use Google Chrome.");
            return;
        }
        if (citizenListening) return;
        try { citizenRecognition.start(); } catch (error) { console.error(error); }
    }

    function stopCitizenListening() {
        if (citizenRecognition && citizenListening) citizenRecognition.stop();
    }

    // Builds the always-accurate "1. PHC -- X km away -- collect Y"
    // list straight from the structured data, so distance/quantity
    // are never left out even if the AI's own sentence glosses over
    // them.
    function renderStopsList(locations) {
        if (!locations || !locations.length) return "";
        var lines = locations.map(function(loc, i) {
            var dist = (loc.distance_km != null)
                ? (loc.distance_km + " km away")
                : "distance unknown (allow location access to see this)";
            return (i + 1) + ". " + loc.name + " (" + loc.district + ") -- " + dist +
                   " -- collect " + loc.allocated;
        });
        return lines.join("\\n");
    }

    async function askCitizenVoice() {
        const question = document.getElementById("citizenQuestion").value.trim();
        if (!question) { alert("Please type a question or use the microphone."); return; }

        const responseBox = document.getElementById("citizenVoiceResponse");
        responseBox.innerText = "Coven AI is checking the network...";

        // Wait (briefly) for real coordinates before asking, so a
        // fast click right after page load doesn't go out with
        // lat/lon still null.
        const loc = await ensureLocation();

        try {
            const response = await fetch("/api/coven-citizen", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ question: question, lat: loc.lat, lon: loc.lon })
            });
            const data = await response.json();
            if (data.error) { responseBox.innerText = data.error; return; }

            const stopsText = renderStopsList(data.locations);
            responseBox.innerText = stopsText ? (data.response + "\\n\\n" + stopsText) : data.response;
            speakCitizenResponse(data.response);

            const mapBox = document.getElementById("citizenVoiceMap");
            if (data.locations && data.locations.length) {
                mapBox.style.display = "block";
                mapBox.innerHTML = "";
                mapBox.id = "citizenVoiceMap_" + Date.now();
                renderPhcMap(mapBox.id, data.locations,
                    (loc.lat != null) ? {lat: loc.lat, lon: loc.lon} : null);
            } else {
                mapBox.style.display = "none";
            }
        } catch (error) {
            console.error(error);
            responseBox.innerText = "Unable to connect to Coven AI.";
        }
    }

    function speakCitizenResponse(text) {
        if (!("speechSynthesis" in window)) {
            alert("Text-to-speech is not supported in this browser.");
            return;
        }
        window.speechSynthesis.cancel();
        const speech = new SpeechSynthesisUtterance(text);
        speech.lang = "en-IN";
        speech.rate = 0.9;
        speech.pitch = 1.0;
        speech.volume = 1.0;
        window.speechSynthesis.speak(speech);
    }

    function stopCitizenSpeaking() {
        if ("speechSynthesis" in window) window.speechSynthesis.cancel();
    }

    document.addEventListener("DOMContentLoaded", function() {
        var aiMap = document.getElementById("aiResultMap");
        if (aiMap) {
            renderPhcMap("aiResultMap", ANSWER_LOCATIONS_JSON, null);
        }
    });

    </script>

    <form method="POST" id="citizenForm" style="margin-top:20px;">
        <label>Or type your question directly</label><br>
        <input name="question" placeholder="e.g. I need 90 units of Paracetamol near Rohini" required>
        <input type="hidden" name="lat" id="citizenLat" value="">
        <input type="hidden" name="lon" id="citizenLon" value="">
        <br>
        <button class="button-green" type="submit">Check Availability</button>
    </form>

    <a class="back" href="/customer">\u2190 Back to Dashboard</a>
    """

    body = body.replace("ANSWER_HTML_PLACEHOLDER", answer_html)
    body = body.replace("ANSWER_LOCATIONS_JSON", json.dumps(answer_locations))

    return page("Coven AI Resource Assistant", body, floating_ai=True)


# ============================================================
# MERGED ADMIN ANALYTICS (from the analytics version)
# ============================================================

CHART_GREEN = "#2E8B57"
CHART_GREEN_LIGHT = "#8CC9A5"
CHART_AMBER = "#E6A23C"
CHART_RED = "#D9534F"
CHART_BLUE = "#4C78A8"
CHART_GRID = "#E8EFEC"
CHART_TEXT = "#29443A"
CHART_MUTED = "#6B7F76"


def _prepare_chart():
    """Apply consistent Matplotlib typography and rendering settings."""
    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 12,
        "axes.titlesize": 18,
        "axes.labelsize": 15,
        "xtick.labelsize": 13,
        "ytick.labelsize": 13,
        "legend.fontsize": 13,
        "figure.dpi": 150,
        "savefig.dpi": 170,
    })


def _chart_to_base64(fig):
    """Convert a Matplotlib figure to a base64 PNG string."""
    buffer = io.BytesIO()
    fig.savefig(
        buffer,
        format="png",
        dpi=170,
        bbox_inches="tight",
        facecolor="white",
        edgecolor="none",
        pad_inches=0.25,
    )
    plt.close(fig)
    buffer.seek(0)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def create_stock_status_chart(phc_id):
    """Create a clean donut chart for medicine availability."""
    stock = get_stock(phc_id)
    if not stock:
        return None

    out_of_stock = sum(
        1 for item in stock
        if int(item.get("quantity_available") or 0) == 0
    )
    low_stock = sum(
        1 for item in stock
        if 0 < int(item.get("quantity_available") or 0) <= 5
    )
    sufficient_stock = sum(
        1 for item in stock
        if int(item.get("quantity_available") or 0) > 5
    )

    raw = [
        ("Out of Stock", out_of_stock, CHART_RED),
        ("Low Stock", low_stock, CHART_AMBER),
        ("Sufficient Stock", sufficient_stock, CHART_GREEN),
    ]

    # Remove zero-value slices. This prevents empty 0% labels from
    # cluttering the chart.
    data = [(label, value, color) for label, value, color in raw if value > 0]

    if not data:
        return None

    _prepare_chart()
    fig, ax = plt.subplots(figsize=(5.8, 4.7))

    labels = [item[0] for item in data]
    values = [item[1] for item in data]
    colors = [item[2] for item in data]

    def percentage_label(pct):
        return f"{pct:.0f}%" if pct >= 4 else ""

    wedges, _, autotexts = ax.pie(
        values,
        labels=None,
        colors=colors,
        startangle=90,
        counterclock=False,
        autopct=percentage_label,
        pctdistance=0.72,
        wedgeprops={
            "width": 0.38,
            "edgecolor": "white",
            "linewidth": 3,
        },
        textprops={
            "fontsize": 14,
            "fontweight": "bold",
            "color": "white",
        },
    )

    for text_obj in autotexts:
        text_obj.set_fontsize(14)
        text_obj.set_fontweight("bold")

    # Center label: much cleaner than a second Matplotlib title.
    ax.text(
        0, 0.08, "STOCK",
        ha="center", va="center",
        fontsize=11, fontweight="bold", color=CHART_MUTED
    )
    ax.text(
        0, -0.12, f"{sum(values)}",
        ha="center", va="center",
        fontsize=20, fontweight="bold", color=CHART_TEXT
    )

    legend_labels = [
        f"{label}  ·  {value} medicine{'s' if value != 1 else ''}"
        for label, value, _ in data
    ]
    ax.legend(
        wedges,
        legend_labels,
        loc="center left",
        bbox_to_anchor=(1.02, 0.5),
        frameon=False,
        labelspacing=1.15,
        handlelength=1.1,
        fontsize=14,
    )

    ax.set_aspect("equal")
    fig.subplots_adjust(left=0.02, right=0.70, top=0.96, bottom=0.04)

    return _chart_to_base64(fig)


def create_medicine_bar_chart(phc_id):
    """Create a clean bar chart showing current medicine quantities."""
    stock = get_stock(phc_id)
    if not stock:
        return None

    medicine_names = [str(item["medicine_name"]) for item in stock]
    quantities = [
        int(item.get("quantity_available") or 0)
        for item in stock
    ]

    _prepare_chart()

    # A horizontal chart becomes much easier to read when there are
    # many medicine names. With a small catalog, vertical bars look
    # cleaner and match the requested dashboard style.
    if len(medicine_names) > 7:
        fig, ax = plt.subplots(figsize=(10, 5.2))
        positions = list(range(len(medicine_names)))
        bars = ax.barh(
            positions,
            quantities,
            height=0.58,
            color=CHART_BLUE,
            edgecolor="none",
        )
        ax.set_yticks(positions)
        ax.set_yticklabels(medicine_names)
        ax.invert_yaxis()
        ax.set_xlabel("Units available", color=CHART_MUTED, labelpad=8)
        ax.grid(axis="x", color=CHART_GRID, linewidth=0.8)
        ax.set_axisbelow(True)

        max_value = max(quantities) if quantities else 1
        ax.set_xlim(0, max(1, max_value * 1.18))

        for bar, quantity in zip(bars, quantities):
            ax.text(
                bar.get_width() + max(1, max_value * 0.015),
                bar.get_y() + bar.get_height() / 2,
                str(quantity),
                va="center",
                ha="left",
                fontsize=9,
                fontweight="bold",
                color=CHART_TEXT,
            )
    else:
        fig, ax = plt.subplots(figsize=(8.8, 4.9))
        positions = list(range(len(medicine_names)))
        bars = ax.bar(
            positions,
            quantities,
            width=0.56,
            color=CHART_BLUE,
            edgecolor="none",
        )

        ax.set_xticks(positions)
        ax.set_xticklabels(
            medicine_names,
            rotation=0,
            ha="center",
            color=CHART_TEXT,
            fontsize=13,
            fontweight="bold",
)
        
# Move ONLY the x-axis labels to the right
        for label in ax.get_xticklabels():
            label.set_transform(
                label.get_transform()
                + ScaledTranslation(18 / 72, 0, fig.dpi_scale_trans)
    )
        
        ax.set_ylabel("Units available", color=CHART_MUTED, labelpad=8, fontsize=15,
        fontweight="bold",)
        ax.grid(axis="y", color=CHART_GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_color("#DCE7E2")
        ax.spines["bottom"].set_color("#DCE7E2")

        max_value = max(quantities) if quantities else 1
        ax.set_ylim(0, max(1, max_value * 1.20))

        for bar, quantity in zip(bars, quantities):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + max(1, max_value * 0.025),
                str(quantity),
                ha="center",
                va="bottom",
                fontsize=9,
                fontweight="bold",
                color=CHART_TEXT,
            )

    ax.tick_params(axis="both", length=0)
    fig.tight_layout(pad=1.0)

    return _chart_to_base64(fig)


def create_bed_chart(phc_id):
    """Create a grouped bar chart for total and available beds."""
    beds = get_beds(phc_id)
    if not beds:
        return None

    ward_names = [str(item["ward_type"]).upper() for item in beds]
    total_beds = [
        int(item.get("total_beds") or 0)
        for item in beds
    ]
    available_beds = [
        int(item.get("available_beds") or 0)
        for item in beds
    ]

    _prepare_chart()
    fig, ax = plt.subplots(figsize=(9.0, 4.8))

    positions = list(range(len(ward_names)))
    width = 0.34

    total_bars = ax.bar(
        [p - width / 2 for p in positions],
        total_beds,
        width,
        label="Total beds",
        color=CHART_GREEN_LIGHT,
        edgecolor="none",
    )
    available_bars = ax.bar(
        [p + width / 2 for p in positions],
        available_beds,
        width,
        label="Available beds",
        color=CHART_GREEN,
        edgecolor="none",
    )

    ax.set_xticks(positions)
    ax.set_xticklabels(ward_names)
    ax.set_ylabel("Number of beds", color=CHART_MUTED, labelpad=8)
    ax.grid(axis="y", color=CHART_GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#DCE7E2")
    ax.spines["bottom"].set_color("#DCE7E2")
    ax.tick_params(axis="both", length=0)

    max_value = max(total_beds + available_beds) if (total_beds or available_beds) else 1
    ax.set_ylim(0, max(1, max_value * 1.22))

    for bars in (total_bars, available_bars):
        for bar in bars:
            value = int(bar.get_height())
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + max(1, max_value * 0.025),
                str(value),
                ha="center",
                va="bottom",
                fontsize=9,
                fontweight="bold",
                color=CHART_TEXT,
            )

    ax.legend(
        loc="upper right",
        frameon=False,
        ncol=2,
        handlelength=1.2,
    )

    fig.tight_layout(pad=1.0)
    return _chart_to_base64(fig)


# ============================================================
# ADMIN ANALYTICS
# ============================================================

@app.route("/admin/analytics")
def admin_analytics():

    if not require_admin():
        return redirect(url_for("login"))

    phc_id = session["phc_id"]
    phc = get_phc(phc_id)

    if not phc:
        return redirect(url_for("login"))

    stock = get_stock(phc_id)
    beds = get_beds(phc_id)

    stock_chart = create_stock_status_chart(phc_id)
    medicine_chart = create_medicine_bar_chart(phc_id)
    bed_chart = create_bed_chart(phc_id)

    total_medicines = len(stock)
    total_units = sum(int(item.get("quantity_available") or 0) for item in stock)
    out_of_stock = sum(
        1 for item in stock if int(item.get("quantity_available") or 0) == 0
    )
    low_stock = sum(
        1 for item in stock
        if 0 < int(item.get("quantity_available") or 0) <= 5
    )
    total_beds = sum(int(item.get("total_beds") or 0) for item in beds)
    available_beds = sum(int(item.get("available_beds") or 0) for item in beds)

    stock_img = (
        f'<img class="chart-image chart-image-pie" '
        f'src="data:image/png;base64,{stock_chart}" alt="Medicine stock status chart">'
        if stock_chart else
        '<div class="chart-empty">No stock data available.</div>'
    )
    medicine_img = (
        f'<img class="chart-image" '
        f'src="data:image/png;base64,{medicine_chart}" alt="Medicine quantities chart">'
        if medicine_chart else
        '<div class="chart-empty">No medicine data available.</div>'
    )
    bed_img = (
        f'<img class="chart-image" '
        f'src="data:image/png;base64,{bed_chart}" alt="Bed availability chart">'
        if bed_chart else
        '<div class="chart-empty">No bed data available.</div>'
    )

    body = f"""
    <div class="dashboard-header">
        <div>
            <div class="section-kicker">ANALYTICS CENTRE</div>
            <h1>PHC Resource Analytics</h1>
            <p class="dashboard-subtitle">
                A visual snapshot of inventory and facility capacity at
                <strong>{phc['name']}</strong>.
            </p>
        </div>
        <a href="/admin" class="outline-button">← Dashboard</a>
    </div>

    <div class="kpi-grid analytics-kpis">
        <div class="kpi-card">
            <div class="kpi-icon">💊</div>
            <div><span class="kpi-label">Medicines tracked</span>
            <strong class="kpi-value">{total_medicines}</strong></div>
        </div>
        <div class="kpi-card">
            <div class="kpi-icon">📦</div>
            <div><span class="kpi-label">Units in stock</span>
            <strong class="kpi-value">{total_units}</strong></div>
        </div>
        <div class="kpi-card kpi-warning">
            <div class="kpi-icon">⚠️</div>
            <div><span class="kpi-label">Low stock</span>
            <strong class="kpi-value">{low_stock}</strong></div>
        </div>
        <div class="kpi-card kpi-danger">
            <div class="kpi-icon">🚨</div>
            <div><span class="kpi-label">Out of stock</span>
            <strong class="kpi-value">{out_of_stock}</strong></div>
        </div>
        <div class="kpi-card">
            <div class="kpi-icon">🛏️</div>
            <div><span class="kpi-label">Beds available</span>
            <strong class="kpi-value">{available_beds}<span class="kpi-small"> / {total_beds}</span></strong></div>
        </div>
    </div>

    <section class="analytics-section analytics-page-section">
        <div class="analytics-grid">
            <article class="chart-card chart-card-pie">
                <div class="chart-card-heading">
                    <div>
                        <span class="chart-number">01</span>
                        <h3>Medicine Stock Status</h3>
                        <p>Availability split across the current medicine inventory.</p>
                    </div>
                    <span class="chart-badge">Inventory</span>
                </div>
                <div class="chart-wrap pie-wrap">{stock_img}</div>
            </article>

            <article class="chart-card">
                <div class="chart-card-heading">
                    <div>
                        <span class="chart-number">02</span>
                        <h3>Medicine Quantities</h3>
                        <p>Current units available for each medicine.</p>
                    </div>
                    <span class="chart-badge">Stock</span>
                </div>
                <div class="chart-wrap">{medicine_img}</div>
            </article>

            <article class="chart-card chart-card-wide">
                <div class="chart-card-heading">
                    <div>
                        <span class="chart-number">03</span>
                        <h3>Bed Availability by Ward</h3>
                        <p>Compare total capacity with beds currently available.</p>
                    </div>
                    <span class="chart-badge">Facilities</span>
                </div>
                <div class="chart-wrap">{bed_img}</div>
            </article>
        </div>
    </section>

    <div class="analytics-note">
        <span>💡</span>
        <div>
            <strong>How to read this dashboard</strong>
            <p>
                Green indicates healthy availability, amber indicates low stock,
                and red indicates an item that is out of stock.
            </p>
        </div>
    </div>

    <a class="back" href="/admin">← Back to Dashboard</a>
    """

    return page("PHC Analytics", body, floating_ai=True)



# ============================================================
# API ENDPOINTS
# ============================================================

@app.route("/api/predictive-stock", methods=["GET"])
def predictive_stock_api():
    if not require_admin():
        return jsonify({"error": "Access denied."}), 403

    phc_id = session.get("phc_id")
    if not phc_id:
        return jsonify({"error": "No PHC assigned."}), 403

    try:
        predictions = get_predictive_stock(phc_id)
        return jsonify({"success": True, "predictions": predictions})
    except Exception as e:
        print("Predictive model error:", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/coven", methods=["POST"])
def coven_api():
    if not require_admin():
        return jsonify({"error": "Access denied."}), 403

    phc_id = session.get("phc_id")
    if not phc_id:
        return jsonify({"error": "No PHC is assigned to this account."}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No request data received."}), 400

    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "Please provide a question."}), 400

    try:
        answer = ask_gemini(question, admin_phc_id=phc_id)
        return jsonify({"response": answer})
    except Exception as e:
        print("Coven AI API error:", e)
        return jsonify({"error": f"Coven AI request failed: {e}"}), 500


@app.route("/api/coven-citizen", methods=["POST"])
def coven_citizen_api():
    if not require_customer():
        return jsonify({"error": "Access denied."}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No request data received."}), 400

    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "Please provide a question."}), 400

    user_lat = data.get("lat")
    user_lon = data.get("lon")

    try:
        result = get_citizen_answer(question, user_lat, user_lon)
        return jsonify({"response": result["text"], "locations": result["locations"]})
    except Exception as e:
        print("Coven AI citizen API error:", e)
        return jsonify({"error": f"Coven AI request failed: {e}"}), 500


@app.route("/api/nearby-phcs", methods=["GET"])
def nearby_phcs_api():
    if not require_customer():
        return jsonify({"error": "Access denied."}), 403

    lat_raw = request.args.get("lat")
    lon_raw = request.args.get("lon")
    try:
        user_lat = float(lat_raw) if lat_raw not in (None, "", "null") else None
        user_lon = float(lon_raw) if lon_raw not in (None, "", "null") else None
    except ValueError:
        user_lat = user_lon = None

    try:
        phcs = get_phcs_with_distance(user_lat, user_lon)
        return jsonify({"phcs": phcs})
    except Exception as e:
        print("Nearby PHCs error:", e)
        return jsonify({"error": str(e)}), 500


# ============================================================
# PROGRAM START
# ============================================================

if __name__ == "__main__":
    print("STARTING FLASK...", flush=True)
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
