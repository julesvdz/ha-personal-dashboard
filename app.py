import os
import sqlite3
import requests
from flask import Flask, render_template, redirect, url_for, g, jsonify, request
from datetime import datetime, timedelta
import logging
import websocket # For WebSocket API interaction
import json
from urllib.parse import urlparse

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)

app.logger.setLevel(logging.INFO) # Set logging level to INFO by default

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
        db.row_factory = sqlite3.Row
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

with app.app_context():
    init_db()

def fetch_ha_data_rest(endpoint):
    """Fetches data from Home Assistant REST API."""
    try:
        response = requests.get(f"{HA_URL}/api/{endpoint}", headers=HEADERS)
        response.raise_for_status()
        data = response.json()
        if endpoint == 'config/area_registry' or endpoint == 'config/entity_registry':
            app.logger.debug(f"HA REST API Response for {endpoint}: {data}") # Only debug log for specific endpoints
        return data
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Error fetching data from Home Assistant REST API at {endpoint}: {e}")
        return None

def call_ha_service(domain, service, entity_id):
    """Calls a service on Home Assistant REST API."""
    try:
        data = {"entity_id": entity_id}
        response = requests.post(f"{HA_URL}/api/services/{domain}/{service}", headers=HEADERS, json=data)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Error calling Home Assistant service {domain}.{service} for {entity_id}: {e}")
        return None

def get_all_scripts_and_scenes():
    """
    Fetches all scripts and scenes, and their associated area_ids using WebSocket API.
    Combines data from states and entity registry.
    """
    all_states = fetch_ha_data_rest('states')
    if all_states is None:
        app.logger.debug("No states data fetched from HA.")
        return []

    # Determine WebSocket URL
    parsed = urlparse(HA_URL)
    ws_scheme = 'wss' if parsed.scheme == 'https' else 'ws'
    ws_url = f"{ws_scheme}://{parsed.netloc}/api/websocket"

    entity_area_map = {}
    
    try:
        ws = websocket.create_connection(ws_url)
        
        # Receive auth_required
        auth_req = json.loads(ws.recv())
        if auth_req.get('type') != 'auth_required':
            app.logger.error(f"Unexpected WS response during auth_required: {auth_req.get('type', '')}")
            raise RuntimeError('Unexpected WS response during auth_required.')

        # Authenticate
        ws.send(json.dumps({'type': 'auth', 'access_token': HA_TOKEN}))
        auth_res = json.loads(ws.recv())
        if auth_res.get('type') != 'auth_ok':
            app.logger.error(f"WS authentication failed: {auth_res.get('message', '')}")
            raise RuntimeError('WS auth failed.')

        # Request entity registry
        ws.send(json.dumps({'id': 1, 'type': 'config/entity_registry/list'}))
        er_res = json.loads(ws.recv())
        app.logger.debug(f"HA WS Entity Registry Response: {er_res}")
        
        for entry in er_res.get('result', []):
            eid = entry.get('entity_id')
            if eid: # Ensure entity_id exists
                entity_area_map[eid] = entry.get('area_id')

        ws.close()
    except Exception as e:
        app.logger.error(f"WebSocket error during entity/area registry fetch: {e}")
        # Proceed with potentially incomplete maps if WebSocket fails

    app.logger.debug(f"Entity Area Map from WS Registry: {entity_area_map}") # Keep this debug log

    entities = []
    for state_entity in all_states:
        entity_id = state_entity['entity_id']
        if entity_id.startswith(('script.', 'scene.')):
            name = state_entity['attributes'].get('friendly_name', entity_id)
            area_id = entity_area_map.get(entity_id) # Get area_id from the WebSocket-fetched map

            entities.append({
                'entity_id': entity_id,
                'name': name,
                'area_id': area_id
            })
    app.logger.debug(f"Processed Scripts and Scenes with Areas: {entities}") # Keep this debug log
    return entities

def get_areas():
    """Fetches areas from Home Assistant WebSocket API."""
    # Determine WebSocket URL
    parsed = urlparse(HA_URL)
    ws_scheme = 'wss' if parsed.scheme == 'https' else 'ws'
    ws_url = f"{ws_scheme}://{parsed.netloc}/api/websocket"

    areas_map = {}
    
    try:
        ws = websocket.create_connection(ws_url)
        
        # Receive auth_required
        auth_req = json.loads(ws.recv())
        if auth_req.get('type') != 'auth_required':
            app.logger.error(f"Unexpected WS response during auth_required: {auth_req.get('type', '')}")
            raise RuntimeError('Unexpected WS response during auth_required.')

        # Authenticate
        ws.send(json.dumps({'type': 'auth', 'access_token': HA_TOKEN}))
        auth_res = json.loads(ws.recv())
        if auth_res.get('type') != 'auth_ok':
            app.logger.error(f"WS authentication failed: {auth_res.get('message', '')}")
            raise RuntimeError('WS auth failed.')

        # Request area registry
        ws.send(json.dumps({'id': 1, 'type': 'config/area_registry/list'}))
        ar_res = json.loads(ws.recv())
        app.logger.debug(f"HA WS Area Registry Response: {ar_res}")
        
        for area in ar_res.get('result', []):
            aid = area.get('area_id')
            name = area.get('name') or 'Unnamed Area'
            areas_map[aid] = name

        ws.close()
    except Exception as e:
        app.logger.error(f"WebSocket error during area registry fetch: {e}")
        # Proceed with empty areas_map if WebSocket fails

    app.logger.debug(f"Processed Areas Map: {areas_map}") # Keep this debug log
    return areas_map

@app.route('/')
def home():
    all_entities = get_all_scripts_and_scenes()
    areas_map = get_areas()

    # Keep these debug logs for diagnosing area display
    app.logger.debug(f"Home Route - All Entities: {all_entities}")
    app.logger.debug(f"Home Route - Areas Map: {areas_map}")

    # Group entities by area
    entities_by_area = {"other": []} # "other" for entities without an area_id or unmapped area_id
    for area_id in areas_map:
        entities_by_area[area_id] = []

    for entity in all_entities:
        area_id = entity.get('area_id')
        if area_id and area_id in entities_by_area:
            entities_by_area[area_id].append(entity)
        else:
            entities_by_area["other"].append(entity)
    app.logger.debug(f"Home Route - Entities by Area: {entities_by_area}") # Keep this debug log

    # Prepare areas for display
    display_areas = []
    if entities_by_area["other"]:
        display_areas.append({"area_id": "other", "name": "Other"})

    # Sort actual areas by name and add them if they contain entities
    sorted_area_names = sorted([name for id, name in areas_map.items()])
    for area_name in sorted_area_names:
        for area_id, name in areas_map.items():
            if name == area_name and entities_by_area.get(area_id):
                display_areas.append({"area_id": area_id, "name": name})
                break
    app.logger.debug(f"Home Route - Display Areas: {display_areas}") # Keep this debug log

    # Get most used scripts/scenes
    most_used = get_most_used_entities()
    app.logger.debug(f"Home Route - Most Used Entities: {most_used}") # Keep this debug log

    return render_template('home.html', most_used=most_used, areas=display_areas, entities_by_area=entities_by_area, areas_map=areas_map)

@app.route('/api/data')
def api_data():
    """API endpoint that returns all data as JSON for SPA functionality"""
    all_entities = get_all_scripts_and_scenes()
    areas_map = get_areas()

    # Group entities by area
    entities_by_area = {"other": []}
    for area_id in areas_map:
        entities_by_area[area_id] = []

    for entity in all_entities:
        area_id = entity.get('area_id')
        if area_id and area_id in entities_by_area:
            entities_by_area[area_id].append(entity)
        else:
            entities_by_area["other"].append(entity)

    # Sort entities within each area
    for area_id in entities_by_area:
        entities_by_area[area_id].sort(key=lambda x: x['name'].lower())

    # Prepare areas for display
    display_areas = []
    if entities_by_area["other"]:
        display_areas.append({"area_id": "other", "name": "Other"})

    sorted_area_names = sorted([name for id, name in areas_map.items()])
    for area_name in sorted_area_names:
        for area_id, name in areas_map.items():
            if name == area_name and entities_by_area.get(area_id):
                display_areas.append({"area_id": area_id, "name": name})
                break

    # Get most used scripts/scenes
    most_used = get_most_used_entities()

    return jsonify({
        'most_used': most_used,
        'areas': display_areas,
        'entities_by_area': entities_by_area,
        'areas_map': areas_map
    })

@app.route('/area/<area_id>')
def area_detail(area_id):
    """Legacy route for backwards compatibility - redirects to home with hash"""
    return redirect(url_for('home') + f'#{area_id}')

@app.route('/activate/<entity_id>')
def activate_entity(entity_id):
    domain = entity_id.split('.')[0]
    service = "turn_on"

    db = get_db()
    cursor = db.cursor()
    cursor.execute("INSERT INTO usage_log (entity_id) VALUES (?)", (entity_id,))
    db.commit()

    result = call_ha_service(domain, service, entity_id)
    
    # Check if this is an AJAX request
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        if result:
            return jsonify({'success': True, 'message': 'Entity activated successfully'})
        else:
            return jsonify({'success': False, 'message': 'Failed to activate entity'}), 500
    else:
        # Legacy support for direct URL access
        return redirect(url_for('home'))

def get_most_used_entities():
    db = get_db()
    cursor = db.cursor()

    now = datetime.now()
    start_time_window = (now - timedelta(hours=1)).time()
    end_time_window = (now + timedelta(hours=1)).time()
    thirty_days_ago = now - timedelta(days=30)

    cursor.execute('''
        SELECT entity_id, COUNT(*) as count, STRFTIME('%H:%M:%S', timestamp) as time_of_day
        FROM usage_log
        WHERE timestamp >= ?
        GROUP BY entity_id, time_of_day
        ORDER BY count DESC
    ''', (thirty_days_ago,))
    raw_usage_data = cursor.fetchall()

    filtered_usage = {}
    for row in raw_usage_data:
        log_time = datetime.strptime(row['time_of_day'], '%H:%M:%S').time()
        if start_time_window <= end_time_window:
            if start_time_window <= log_time <= end_time_window:
                filtered_usage[row['entity_id']] = filtered_usage.get(row['entity_id'], 0) + row['count']
        else:
            if log_time >= start_time_window or log_time <= end_time_window:
                filtered_usage[row['entity_id']] = filtered_usage.get(row['entity_id'], 0) + row['count']

    all_entities = get_all_scripts_and_scenes()
    entity_name_map = {e['entity_id']: e['name'] for e in all_entities}

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
    app.run(debug=True, port=5003)