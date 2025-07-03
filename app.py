import os
import sqlite3
import requests
from flask import Flask, render_template, redirect, url_for, g
from datetime import datetime, timedelta

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24) # For session management, if needed later

# Configuration from environment variables
HA_URL = os.environ.get('HA_URL')
HA_TOKEN = os.environ.get('HA_TOKEN')

if not HA_URL or not HA_TOKEN:
    raise ValueError("HA_URL and HA_TOKEN environment variables must be set.")

HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

DATABASE = 'data/usage.db'

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row # This allows accessing columns by name
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        db.commit()

# Initialize database on app startup
with app.app_context():
    init_db()

def fetch_ha_data(endpoint):
    try:
        response = requests.get(f"{HA_URL}/api/{endpoint}", headers=HEADERS)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Error fetching data from Home Assistant API at {endpoint}: {e}")
        return None

def call_ha_service(domain, service, entity_id):
    try:
        data = {"entity_id": entity_id}
        response = requests.post(f"{HA_URL}/api/services/{domain}/{service}", headers=HEADERS, json=data)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Error calling Home Assistant service {domain}.{service} for {entity_id}: {e}")
        return None

def get_all_scripts_and_scenes():
    scripts = fetch_ha_data('states')
    if scripts is None:
        return []

    entities = []
    for entity in scripts:
        if entity['entity_id'].startswith(('script.', 'scene.')):
            entities.append({
                'entity_id': entity['entity_id'],
                'name': entity['attributes'].get('friendly_name', entity['entity_id']),
                'area_id': entity['attributes'].get('area_id')
            })
    return entities

def get_areas():
    areas_data = fetch_ha_data('config/areas')
    if areas_data is None:
        return {}
    return {area['area_id']: area['name'] for area in areas_data}

@app.route('/')
def home():
    all_entities = get_all_scripts_and_scenes()
    areas_map = get_areas()

    # Group entities by area
    entities_by_area = {"other": []} # "other" for entities without an area_id
    for area_id in areas_map:
        entities_by_area[area_id] = []

    for entity in all_entities:
        area_id = entity.get('area_id')
        if area_id and area_id in entities_by_area:
            entities_by_area[area_id].append(entity)
        else:
            entities_by_area["other"].append(entity)

    # Prepare areas for display
    display_areas = []
    if entities_by_area["other"]:
        display_areas.append({"area_id": "other", "name": "Other"})

    sorted_area_names = sorted([name for id, name in areas_map.items()])
    for area_name in sorted_area_names:
        for area_id, name in areas_map.items():
            if name == area_name and entities_by_area.get(area_id): # Only add if there are entities in the area
                display_areas.append({"area_id": area_id, "name": name})
                break # Found the area, move to next sorted name

    # Get most used scripts/scenes
    most_used = get_most_used_entities()

    return render_template('home.html', most_used=most_used, areas=display_areas)

@app.route('/area/<area_id>')
def area_detail(area_id):
    all_entities = get_all_scripts_and_scenes()
    areas_map = get_areas()

    if area_id == "other":
        area_name = "Other"
        filtered_entities = [e for e in all_entities if e.get('area_id') not in areas_map]
    else:
        area_name = areas_map.get(area_id, "Unknown Area")
        filtered_entities = [e for e in all_entities if e.get('area_id') == area_id]

    # Sort entities alphabetically by name
    filtered_entities.sort(key=lambda x: x['name'].lower())

    return render_template('area.html', area_name=area_name, entities=filtered_entities)

@app.route('/activate/<entity_id>')
def activate_entity(entity_id):
    domain = entity_id.split('.')[0]
    service = "turn_on" # Scripts and scenes typically use 'turn_on'

    # Log usage
    db = get_db()
    cursor = db.cursor()
    cursor.execute("INSERT INTO usage_log (entity_id) VALUES (?)", (entity_id,))
    db.commit()

    call_ha_service(domain, service, entity_id)
    return redirect(url_for('home'))

def get_most_used_entities():
    db = get_db()
    cursor = db.cursor()

    # Calculate time window: last 30 days, current time +/- 1 hour
    now = datetime.now()
    start_time_window = (now - timedelta(hours=1)).time()
    end_time_window = (now + timedelta(hours=1)).time()
    thirty_days_ago = now - timedelta(days=30)

    # Fetch usage data within the last 30 days
    cursor.execute('''
        SELECT entity_id, COUNT(*) as count, STRFTIME('%H:%M:%S', timestamp) as time_of_day
        FROM usage_log
        WHERE timestamp >= ?
        GROUP BY entity_id, time_of_day
        ORDER BY count DESC
    ''', (thirty_days_ago,))
    raw_usage_data = cursor.fetchall()

    # Filter by time window and aggregate counts
    filtered_usage = {}
    for row in raw_usage_data:
        log_time = datetime.strptime(row['time_of_day'], '%H:%M:%S').time()
        # Check if log_time falls within the current time window
        if start_time_window <= end_time_window: # Normal case (e.g., 10:00-12:00)
            if start_time_window <= log_time <= end_time_window:
                filtered_usage[row['entity_id']] = filtered_usage.get(row['entity_id'], 0) + row['count']
        else: # Case where window crosses midnight (e.g., 23:00-01:00)
            if log_time >= start_time_window or log_time <= end_time_window:
                filtered_usage[row['entity_id']] = filtered_usage.get(row['entity_id'], 0) + row['count']

    # Get friendly names for entities
    all_entities = get_all_scripts_and_scenes()
    entity_name_map = {e['entity_id']: e['name'] for e in all_entities}

    # Sort by count and prepare for display
    most_used_list = []
    for entity_id, count in sorted(filtered_usage.items(), key=lambda item: item[1], reverse=True):
        if entity_id in entity_name_map:
            most_used_list.append({
                'entity_id': entity_id,
                'name': entity_name_map[entity_id],
                'count': count
            })
    return most_used_list

if __name__ == '__main__':
    app.run(debug=True)