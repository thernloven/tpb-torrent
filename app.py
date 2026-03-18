import os
import logging
import threading
import time
import glob

import requests
import libtorrent as lt
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from flask_cors import CORS
from functools import wraps

app = Flask(__name__)
CORS(app)
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = True

# Logging — ensures output shows in gunicorn error log
gunicorn_logger = logging.getLogger('gunicorn.error')
app.logger.handlers = gunicorn_logger.handlers
app.logger.setLevel(gunicorn_logger.level)
log = app.logger

# Config
API_KEY = os.getenv('API_KEY', 'change-me-in-production')
BACKEND_URL = os.getenv('BACKEND_URL', 'http://localhost:3000')
BASE_URL = os.getenv('TPB_URL', 'https://thepiratebay10.info/')
DOWNLOAD_PATH = os.getenv('DOWNLOAD_PATH', '/tmp/torrents')
IDLE_SHUTDOWN_MINUTES = int(os.getenv('IDLE_SHUTDOWN_MINUTES', '10'))
REQUEST_TIMEOUT = 15

os.makedirs(DOWNLOAD_PATH, exist_ok=True)

# Libtorrent session
ses = lt.session({
    'listen_interfaces': '0.0.0.0:6881,[::]:6881',
    'alert_mask': lt.alert.category_t.all_categories,
})

# Track torrents: info_hash -> {handle, r2_url, content_id, status, upload_progress}
active_torrents = {}
last_activity = time.time()

# -------------------------------------------------------------------
# Auth
# -------------------------------------------------------------------

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get('X-API-Key')
        if key != API_KEY:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

# -------------------------------------------------------------------
# Health
# -------------------------------------------------------------------

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'active_torrents': len(active_torrents)}), 200

# -------------------------------------------------------------------
# Search
# -------------------------------------------------------------------

SORT_FILTERS = {
    'title_asc': 1, 'title_desc': 2,
    'time_desc': 3, 'time_asc': 4,
    'size_desc': 5, 'size_asc': 6,
    'seeds_desc': 7, 'seeds_asc': 8,
    'leeches_desc': 9, 'leeches_asc': 10,
    'uploader_asc': 11, 'uploader_desc': 12,
    'category_asc': 13, 'category_desc': 14,
}


@app.route('/search/<term>', methods=['GET'])
@app.route('/search/<term>/<int:page>', methods=['GET'])
@require_auth
def search_torrents(term, page=1):
    sort = request.args.get('sort', '')
    sort_code = SORT_FILTERS.get(sort, 99)
    url = f'{BASE_URL}search/{term}/{page}/{sort_code}/0'
    log.info(f'[SEARCH] "{term}" page={page} sort={sort}')
    results = parse_page(url, sort=sort if sort in SORT_FILTERS else '')
    log.info(f'[SEARCH] "{term}" → {len(results)} results')
    return jsonify(results), 200


@app.route('/top/<int:cat>', methods=['GET'])
@require_auth
def top_torrents(cat=0):
    sort = request.args.get('sort', '')
    path = 'top/all' if cat == 0 else f'top/{cat}'
    results = parse_page(f'{BASE_URL}{path}', sort=sort if sort in SORT_FILTERS else '')
    return jsonify(results), 200


@app.route('/recent', methods=['GET'])
@app.route('/recent/<int:page>', methods=['GET'])
@require_auth
def recent_torrents(page=0):
    sort = request.args.get('sort', '')
    results = parse_page(f'{BASE_URL}recent/{page}', sort=sort if sort in SORT_FILTERS else '')
    return jsonify(results), 200


def parse_page(url, sort=None):
    try:
        data = requests.get(url, timeout=REQUEST_TIMEOUT).text
    except requests.RequestException:
        return []

    soup = BeautifulSoup(data, 'html.parser')
    table = soup.find('table', {'id': 'searchResult'})
    if table is None:
        return []

    torrents = []
    for row in table.find_all('tr'):
        cols = row.find_all('td')
        if len(cols) < 8:
            continue
        try:
            category_text = cols[0].get_text(strip=True).replace('\xa0', ' ')
            parts = category_text.split(' > ')
            cat = parts[0].strip() if parts else category_text
            subcat = parts[1].strip() if len(parts) > 1 else ''

            title = cols[1].find('a').get_text(strip=True).replace('\xa0', ' ') if cols[1].find('a') else ''
            link = cols[1].find('a')['href'] if cols[1].find('a') else ''

            magnet_tag = cols[3].find('a', href=lambda h: h and h.startswith('magnet:'))
            magnet = magnet_tag['href'] if magnet_tag else ''

            size_str = cols[4].get_text(strip=True).replace('\xa0', ' ')
            seeders = cols[5].get_text(strip=True)
            leechers = cols[6].get_text(strip=True)
            uploader = cols[7].get_text(strip=True).replace('\xa0', ' ')

            torrents.append({
                'title': title,
                'magnet': magnet,
                'time': cols[2].get_text(strip=True).replace('\xa0', ' '),
                'size': convert_to_bytes(size_str),
                'uploader': uploader,
                'seeds': int(seeders) if seeders.isdigit() else 0,
                'leeches': int(leechers) if leechers.isdigit() else 0,
                'category': cat,
                'subcat': subcat,
                'id': link,
            })
        except (ValueError, TypeError, AttributeError):
            continue

    if sort:
        parts = sort.split('_')
        torrents = sorted(torrents, key=lambda k: k.get(parts[0], ''), reverse=parts[1].upper() == 'DESC')

    return torrents


def convert_to_bytes(size_str):
    size_data = size_str.split()
    if len(size_data) < 2:
        return 0
    multipliers = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB']
    try:
        mag = float(size_data[0])
        exp = multipliers.index(size_data[1])
        return mag * (1024 ** exp if exp > 0 else 1)
    except (ValueError, IndexError):
        return 0

# -------------------------------------------------------------------
# Torrent management
# -------------------------------------------------------------------

@app.route('/torrents/add', methods=['POST'])
@require_auth
def add_torrent():
    data = request.json or {}
    magnet = data.get('magnet')
    if not magnet:
        return jsonify({'error': 'No magnet link provided'}), 400

    log.info(f'[ADD] Adding torrent, content_id={data.get("content_id")}, has_r2_key={bool(data.get("r2_key"))}')

    params = lt.parse_magnet_uri(magnet)
    params.save_path = DOWNLOAD_PATH
    handle = ses.add_torrent(params)
    info_hash = str(handle.info_hash())
    log.info(f'[ADD] Torrent added: hash={info_hash}')

    active_torrents[info_hash] = {
        'handle': handle,
        'r2_key': data.get('r2_key'),
        'content_id': data.get('content_id'),
        'callback_url': data.get('callback_url'),
        'status': 'downloading',
        'upload_progress': 0,
    }

    global last_activity
    last_activity = time.time()

    return jsonify({'status': 'ok', 'hash': info_hash}), 200


@app.route('/torrents', methods=['GET'])
@require_auth
def list_torrents():
    result = []
    for info_hash, t in list(active_torrents.items()):
        handle = t['handle']
        s = handle.status()

        eta = -1
        if s.download_rate > 0 and s.total_wanted > 0:
            remaining = s.total_wanted - s.total_wanted_done
            eta = int(remaining / s.download_rate)

        state_map = {
            0: 'queued', 1: 'checking', 2: 'downloading_metadata',
            3: 'downloading', 4: 'finished', 5: 'seeding',
            6: 'allocating', 7: 'checking_resume',
        }

        result.append({
            'hash': info_hash,
            'name': s.name or 'Fetching metadata...',
            'size': s.total_wanted,
            'progress': round(s.progress * 100, 1),
            'dlspeed': s.download_rate,
            'upspeed': s.upload_rate,
            'state': t['status'] if t['status'] == 'uploading' else state_map.get(s.state, str(s.state)),
            'seeds': s.num_seeds,
            'peers': s.num_peers,
            'eta': eta,
            'content_id': t.get('content_id'),
            'upload_progress': t.get('upload_progress', 0),
            'paused': s.paused,
        })
    return jsonify(result), 200


@app.route('/torrents/pause/<info_hash>', methods=['POST'])
@require_auth
def pause_torrent(info_hash):
    t = active_torrents.get(info_hash)
    if not t:
        return jsonify({'error': 'Not found'}), 404
    t['handle'].pause()
    return jsonify({'status': 'ok'}), 200


@app.route('/torrents/resume/<info_hash>', methods=['POST'])
@require_auth
def resume_torrent(info_hash):
    t = active_torrents.get(info_hash)
    if not t:
        return jsonify({'error': 'Not found'}), 404
    t['handle'].resume()
    return jsonify({'status': 'ok'}), 200


@app.route('/torrents/delete/<info_hash>', methods=['DELETE'])
@require_auth
def delete_torrent(info_hash):
    t = active_torrents.get(info_hash)
    if not t:
        return jsonify({'error': 'Not found'}), 404
    ses.remove_torrent(t['handle'], lt.options_t.delete_files)
    del active_torrents[info_hash]
    return jsonify({'status': 'ok'}), 200

# -------------------------------------------------------------------
# Background: monitor downloads, upload to R2, idle shutdown
# -------------------------------------------------------------------

def find_largest_file(directory):
    '''Find the largest file in the torrent download (the actual media file).'''
    largest = None
    largest_size = 0
    for root, _, files in os.walk(directory):
        for f in files:
            path = os.path.join(root, f)
            size = os.path.getsize(path)
            if size > largest_size:
                largest_size = size
                largest = path
    return largest


def upload_to_r2(file_path, r2_key, info_hash):
    '''Upload a file to R2 using multipart upload via the backend.'''
    t = active_torrents.get(info_hash)
    if not t:
        return False

    file_size = os.path.getsize(file_path)
    t['status'] = 'uploading'
    t['upload_progress'] = 0

    headers = {'X-API-Key': API_KEY, 'Content-Type': 'application/json'}

    try:
        # Step 1: Get multipart upload URLs from backend
        resp = requests.post(f'{BACKEND_URL}/api/torrents/multipart/create', json={
            'r2_key': r2_key,
            'file_size': file_size,
        }, headers=headers, timeout=30)

        if resp.status_code != 200:
            log.error(f'[UPLOAD] Failed to create multipart: {resp.text}')
            t['status'] = 'upload_failed'
            return False

        multipart = resp.json()
        upload_id = multipart['uploadId']
        parts = multipart['parts']
        total_parts = len(parts)
        log.info(f'[UPLOAD] Multipart created: {total_parts} parts for {file_size} bytes')

        # Step 2: Upload each part
        with open(file_path, 'rb') as f:
            for part in parts:
                chunk = f.read(part['size'])
                part_resp = requests.put(part['url'], data=chunk, headers={
                    'Content-Length': str(len(chunk)),
                }, timeout=600)

                if part_resp.status_code not in (200, 201):
                    log.error(f'[UPLOAD] Part {part["partNumber"]} failed: {part_resp.status_code}')
                    t['status'] = 'upload_failed'
                    return False

                progress = round((part['partNumber'] / total_parts) * 100, 1)
                t['upload_progress'] = progress
                log.info(f'[UPLOAD] Part {part["partNumber"]}/{total_parts} done ({progress}%)')

        # Step 3: Complete multipart upload
        complete_resp = requests.post(f'{BACKEND_URL}/api/torrents/multipart/complete', json={
            'r2_key': r2_key,
            'upload_id': upload_id,
        }, headers=headers, timeout=30)

        if complete_resp.status_code != 200:
            log.error(f'[UPLOAD] Failed to complete multipart: {complete_resp.text}')
            t['status'] = 'upload_failed'
            return False

        t['upload_progress'] = 100
        log.info(f'[UPLOAD] Multipart upload complete: {r2_key}')
        return True

    except Exception as e:
        log.error(f'[UPLOAD] Exception: {e}')
        t['status'] = 'upload_failed'
        return False


def notify_callback(callback_url, info_hash, content_id, status):
    '''Notify the backend of status changes.'''
    if not callback_url:
        return
    try:
        requests.post(callback_url, json={
            'info_hash': info_hash,
            'content_id': content_id,
            'status': status,
        }, headers={'X-API-Key': API_KEY}, timeout=10)
    except Exception:
        pass


def monitor_loop():
    '''Background thread: watch for completed downloads, upload to R2, idle shutdown.'''
    global last_activity

    while True:
        time.sleep(2)

        for info_hash, t in list(active_torrents.items()):
            if t['status'] != 'downloading':
                continue

            handle = t['handle']
            s = handle.status()

            # Check if download is complete
            if s.progress >= 1.0 and s.state in (4, 5):  # finished or seeding
                log.info(f'[MONITOR] Download complete: {s.name} ({info_hash})')
                handle.pause()  # stop seeding

                # Find the downloaded file
                save_path = handle.save_path()
                torrent_info = handle.torrent_file()
                if torrent_info and torrent_info.num_files() == 1:
                    file_path = os.path.join(save_path, torrent_info.files().file_path(0))
                else:
                    file_path = find_largest_file(save_path)

                if not file_path or not os.path.exists(file_path):
                    log.error(f'[MONITOR] File not found after download: {info_hash}')
                    t['status'] = 'error'
                    continue

                r2_key = t.get('r2_key')
                if r2_key:
                    # Notify backend: status → uploading
                    notify_callback(t.get('callback_url'), info_hash, t.get('content_id'), 'uploading')

                    # Upload to R2 via multipart
                    file_size = os.path.getsize(file_path)
                    log.info(f'[MONITOR] Uploading to R2: {file_path} ({file_size} bytes)')
                    success = upload_to_r2(file_path, r2_key, info_hash)
                    log.info(f'[MONITOR] R2 upload {"success" if success else "FAILED"}: {info_hash}')
                    notify_callback(t.get('callback_url'), info_hash, t.get('content_id'), 'uploaded' if success else 'failed')

                    if success:
                        # Clean up: remove torrent + files
                        ses.remove_torrent(handle, lt.options_t.delete_files)
                        del active_torrents[info_hash]
                        last_activity = time.time()
                        log.info(f'[MONITOR] Cleaned up {info_hash}')
                else:
                    # No R2 URL — just mark as done (local download mode)
                    t['status'] = 'completed'
                    last_activity = time.time()
                    log.info(f'[MONITOR] Local download complete: {info_hash}')

        # Idle shutdown
        if IDLE_SHUTDOWN_MINUTES > 0 and not active_torrents:
            idle_seconds = time.time() - last_activity
            if idle_seconds > IDLE_SHUTDOWN_MINUTES * 60:
                os.system('shutdown -h now')


# Start background monitor
monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
monitor_thread.start()

if __name__ == '__main__':
    port = int(os.getenv('PORT', '8080'))
    app.run(host='0.0.0.0', port=port)
