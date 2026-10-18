from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import requests
from bs4 import BeautifulSoup
import time
import threading
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import json

app = Flask(__name__)

LETTERBOXD_BASE = "https://letterboxd.com"
# Use a realistic browser User-Agent to avoid lightweight bot responses that may omit content
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
REQUESTS_SESSION = requests.Session()
POSTER_CACHE = {}
POSTER_CACHE_LOCK = threading.Lock()

# Alternative headers to try if initial fetch returns empty film lists
ALT_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

# simple in-memory cache for watchlists to avoid re-scraping repeatedly
WATCHLIST_CACHE = {}
# TTL in seconds (6 hours)
WATCHLIST_CACHE_TTL = 6 * 3600
CACHE_LOCK = threading.Lock()

# Simple separate cache used by the streaming compare endpoint
USER_CACHE = {}
# Set to a number (seconds) to enable automatic expiry, or None to keep cached until process restart
CACHE_TTL_SECONDS = None


def _letterboxd_get(url: str):
    """Fetch a Letterboxd URL with a primary and alternative header set."""
    for headers in (HEADERS, ALT_HEADERS):
        try:
            r = requests.get(url, headers=headers, timeout=10)
        except Exception:
            continue
        if r is None:
            continue
        if r.status_code == 200:
            return r
        # allow retries on common bot-blocking or temporary failure codes
        if r.status_code in (403, 404, 429, 503):
            continue
        return r
    return None


def is_valid_letterboxd_username(username: str) -> bool:
    if not username or not username.strip():
        return False
    username = username.strip().strip('/')
    urls = [f"{LETTERBOXD_BASE}/{username}/", f"{LETTERBOXD_BASE}/{username}/watchlist/"]
    for url in urls:
        try:
            r = _letterboxd_get(url)
            if not r:
                continue
            if r.status_code != 200:
                continue
            text = r.text
            if 'page not found' in text.lower():
                continue
            soup = BeautifulSoup(text, "html.parser")
            title = soup.title.string if soup.title else ""
            if 'letterboxd' in title.lower() or username.lower() in title.lower():
                return True
            return True
        except Exception:
            continue
    return False


def _get_cached_watchlist(username: str):
    with CACHE_LOCK:
        rec = WATCHLIST_CACHE.get(username)
        if not rec:
            return None
        if time.time() - rec['ts'] > WATCHLIST_CACHE_TTL:
            # expired
            del WATCHLIST_CACHE[username]
            return None
        # return a copy of the dict
        return dict(rec['films'])


def _set_cached_watchlist(username: str, films: dict):
    with CACHE_LOCK:
        WATCHLIST_CACHE[username] = {"films": dict(films), "ts": time.time()}


def _normalize_film_path(href: str) -> str:
    """Normalize a film href into a stable key like '/film/<slug>'.
    Handles full URLs, relative paths, and strips query strings and extra segments.
    """
    if not href:
        return ''
    try:
        s = href.split('?')[0]
    except Exception:
        s = href
    # If there is a clear '/film/<slug>' segment, capture it
    m = re.search(r'(/film/[^/]+)', s)
    if m:
        return m.group(1)
    # Fallback: strip domain if present and return the path without trailing slash
    if s.startswith('http'):
        try:
            idx = s.find('://')
            s2 = s[idx+3:]
            p = s2.find('/')
            if p != -1:
                path = s2[p:]
            else:
                path = ''
        except Exception:
            path = s
        return path.rstrip('/')

    return s.rstrip('/')

def scrape_watchlist_once(username: str) -> dict:
    """Scrape the first page of a user's watchlist and return a dict mapping film path -> title.
    """
    username = username.strip().strip('/')
    url = f"{LETTERBOXD_BASE}/{username}/watchlist/"
    films = {}
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return films
        soup = BeautifulSoup(r.text, "html.parser")
        # Find anchors that link to film pages
        for a in soup.find_all('a', href=True):
            href = a['href']
            if '/film/' in href:
                key = _normalize_film_path(href)
                title = a.get('title') or a.get_text(strip=True)
                if key and title:
                    films[key] = title
        # fallback: if nothing found, try again with ALT_HEADERS
        if not films:
            try:
                r2 = requests.get(url, headers=ALT_HEADERS, timeout=12)
                if r2.status_code == 200:
                    soup2 = BeautifulSoup(r2.text, "html.parser")
                    for a in soup2.find_all('a', href=True):
                        href = a['href']
                        if '/film/' in href:
                            key = _normalize_film_path(href)
                            title = a.get('title') or a.get_text(strip=True)
                            if key and title:
                                films[key] = title
            except Exception:
                pass
        return films
    except Exception:
        return films


def scrape_watchlist_full(username: str, max_pages: int = 50, pause_between: float = 0.2):
    """Scrape a user's entire watchlist by following page links until no more pages or up to max_pages.
    Results are cached in-memory for WATCHLIST_CACHE_TTL seconds.
    Returns a tuple (films_dict, log_dict) where films_dict maps film path -> title and
    log_dict contains per-page fetch information for debugging/visibility.
    """
    username = username.strip().strip('/')
    cached = _get_cached_watchlist(username)
    logs = {"username": username, "cached": False, "pages": [], "total": 0}
    if cached is not None:
        logs['cached'] = True
        logs['total'] = len(cached)
        return cached, logs

    films = {}

    for page in range(1, max_pages + 1):
        if page == 1:
            url = f"{LETTERBOXD_BASE}/{username}/watchlist/"
        else:
            url = f"{LETTERBOXD_BASE}/{username}/watchlist/page/{page}/"

        try:
            r = requests.get(url, headers=HEADERS, timeout=12)
            if r.status_code != 200:
                logs['pages'].append({"page": page, "url": url, "status": r.status_code, "found": 0})
                break
            soup = BeautifulSoup(r.text, "html.parser")

            page_films = {}
            for a in soup.find_all('a', href=True):
                href = a['href']
                if '/film/' in href:
                    key = _normalize_film_path(href)
                    title = a.get('title') or a.get_text(strip=True)
                    if key and title:
                        page_films[key] = title

            found = len(page_films)
            logs['pages'].append({"page": page, "url": url, "status": r.status_code, "found": found})

            # if this page yields no films, stop paginating
            if not page_films:
                # try again with an alternative header (some sites return different HTML for different UAs)
                try:
                    r2 = requests.get(url, headers=ALT_HEADERS, timeout=12)
                    if r2.status_code == 200:
                        soup2 = BeautifulSoup(r2.text, "html.parser")
                        page_films2 = {}
                        for a in soup2.find_all('a', href=True):
                            href = a['href']
                            if '/film/' in href:
                                key = _normalize_film_path(href)
                                title = a.get('title') or a.get_text(strip=True)
                                if key and title:
                                    page_films2[key] = title
                        if page_films2:
                            page_found = len(page_films2)
                            logs['pages'].append({"page": page, "url": url, "status": r2.status_code, "found": page_found, "note": "alt-ua"})
                            for k, v in page_films2.items():
                                if k not in films:
                                    films[k] = v
                            # continue pagination
                            continue
                except Exception:
                    pass
                break

            before = len(films)
            # merge page_films into films, prefer existing titles
            for k, v in page_films.items():
                if k not in films:
                    films[k] = v

            # if nothing new was added this page, assume we've reached the end
            if len(films) == before:
                break

            # be polite to the server
            time.sleep(pause_between)
        except Exception as ex:
            # on error, record and stop and return whatever we have so far
            logs['pages'].append({"page": page, "url": url, "status": 'error', "error": str(ex), "found": 0})
            break

    logs['total'] = len(films)
    _set_cached_watchlist(username, films)
    return films, logs


def _normalize_poster_url(poster):
    if not poster:
        return None
    poster = poster.strip()
    if not poster:
        return None
    if poster.startswith('//'):
        poster = 'https:' + poster
    if poster.startswith('http') and (' ' in poster or ',' in poster):
        parts = poster.split(',')
        first = parts[0].strip().split()[0].strip()
        poster = first
    if poster.startswith('http') and '?v=' in poster:
        poster = poster.split('?v=')[0]
    return poster or None


def _fetch_film_poster(film_url: str):
    """Fetch the real poster URL from Letterboxd's film-page JSON-LD metadata.
    The watchlist page often exposes only the empty placeholder poster, while the film page's
    "image" field contains the real poster URL.
    """
    if not film_url:
        return None

    normalized_url = film_url.strip().rstrip('/')
    with POSTER_CACHE_LOCK:
        if normalized_url in POSTER_CACHE:
            return POSTER_CACHE[normalized_url]

    try:
        r = REQUESTS_SESSION.get(film_url, headers=HEADERS, timeout=12)
        if r.status_code != 200:
            with POSTER_CACHE_LOCK:
                POSTER_CACHE[normalized_url] = None
            return None
        text = r.text

        # Prefer the film page's JSON-LD "image" field; this is the reliable source for the actual poster.
        m = re.search(r'"image"\s*:\s*"(https://[^"\\]+)"', text)
        if m:
            poster = m.group(1)
            poster = _normalize_poster_url(poster)
            if poster and 'empty-poster' not in poster:
                with POSTER_CACHE_LOCK:
                    POSTER_CACHE[normalized_url] = poster
                return poster

        # Last fallback: search the page source for direct resized film-poster URLs.
        m2 = re.search(r'https://a\.ltrbxd\.com/resized/film-poster[^"\'\s>]+', text)
        if m2:
            poster = m2.group(0)
            poster = _normalize_poster_url(poster)
            if poster and 'empty-poster' not in poster:
                with POSTER_CACHE_LOCK:
                    POSTER_CACHE[normalized_url] = poster
                return poster

        with POSTER_CACHE_LOCK:
            POSTER_CACHE[normalized_url] = None
        return None
    except Exception:
        with POSTER_CACHE_LOCK:
            POSTER_CACHE[normalized_url] = None
        return None


def _extract_film_entries_from_items(items):
    """Turn parsed poster items into film entries, but defer expensive poster fetches.
    This keeps the page fetch fast and lets poster URLs be resolved in parallel.
    """
    entries = []
    for d in items:
        title = None
        href = None
        poster = None

        if d.name == 'div':
            title = d.get('data-item-name') or d.get('data-title') or d.get('data-item-title')
            href = d.get('data-item-link') or d.get('data-url')
            img = d.select_one('img')
            if img:
                poster = img.get('srcset') or img.get('src')
        elif d.name == 'a':
            href = d.get('href')
            img = d.select_one('img')
            if img:
                poster = img.get('srcset') or img.get('src')
            title = d.get('title') or (img.get('alt') if img else None)

        if href and href.startswith('/'):
            link = LETTERBOXD_BASE + href
        else:
            link = href

        poster = _normalize_poster_url(poster)
        if title and link and '/film/' in link:
            entry = {'title': title.strip(), 'link': link, 'poster': poster}
            if not poster or 'empty-poster' in poster:
                entry['needs_poster_fetch'] = True
            entries.append(entry)
    return entries


def _resolve_page_posters(entries):
    """Resolve poster URLs for entries in parallel, preserving cached results."""
    if not entries:
        return []

    pending = [entry for entry in entries if entry.get('needs_poster_fetch')]
    if pending:
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(pending)))) as ex:
            futures = {ex.submit(_fetch_film_poster, entry['link']): entry for entry in pending}
            for fut in as_completed(futures):
                entry = futures[fut]
                result = fut.result()
                entry['poster'] = result or entry.get('poster')
                entry.pop('needs_poster_fetch', None)

    for entry in entries:
        entry.pop('needs_poster_fetch', None)
    return entries


def scrape_user(username, max_pages=200, pause_between=0.05):
    """Scrape a user's watchlist with per-page logging.
    Returns {'films': [film_objs], 'pages': [page_logs]} or {'error': ...}
    Each film_obj: {'title','link','poster'}
    """
    ua = {'User-Agent': HEADERS.get('User-Agent', 'Mozilla/5.0'), 'Accept': HEADERS.get('Accept', '*/*')}
    films = []
    pages = []
    username = username.strip().strip('/')
    try:
        for page in range(1, max_pages + 1):
            url = f"{LETTERBOXD_BASE}/{username}/watchlist/" if page == 1 else f"{LETTERBOXD_BASE}/{username}/watchlist/page/{page}/"
            try:
                r = requests.get(url, headers=ua, timeout=12)
            except requests.RequestException as e:
                pages.append({'page': page, 'url': url, 'status': 'error', 'error': str(e), 'found': 0})
                break

            if r.status_code == 404:
                return {'error': f'user {username} was not found', 'pages': pages}

            soup = BeautifulSoup(r.text, 'html.parser')
            items = soup.select('div.react-component[data-component-class="LazyPoster"]') or []
            if not items:
                items = soup.select('li.poster-container') or []
            if not items:
                items = soup.select('a[href*="/film/"]') or []

            page_films = _resolve_page_posters(_extract_film_entries_from_items(items))
            found = len(page_films)
            pages.append({'page': page, 'url': url, 'status': r.status_code, 'found': found})

            if not page_films:
                try:
                    r2 = requests.get(url, headers=ALT_HEADERS, timeout=10)
                    if r2.status_code == 200:
                        soup2 = BeautifulSoup(r2.text, 'html.parser')
                        items2 = soup2.select('div.react-component[data-component-class="LazyPoster"]') or soup2.select('li.poster-container') or soup2.select('a[href*="/film/"]')
                        page_films2 = _resolve_page_posters(_extract_film_entries_from_items(items2))
                        if page_films2:
                            pages.append({'page': page, 'url': url, 'status': r2.status_code, 'found': len(page_films2), 'note': 'alt-ua'})
                            films.extend(page_films2)
                            time.sleep(pause_between)
                            continue
                except Exception:
                    pass
                break

            films.extend(page_films)
            time.sleep(pause_between)
    except Exception as e:
        return {'error': f'network error for {username}: {str(e)}', 'pages': pages}

    if not films:
        return {'error': f"user {username}'s watchlist is empty", 'pages': pages}

    try:
        USER_CACHE[username] = {'films': films, 'pages': pages, 'ts': time.time()}
    except Exception:
        pass

    return {'films': films, 'pages': pages}


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/validate_username', methods=['POST'])
def validate_username():
    data = request.get_json() or {}
    username = data.get('username', '')
    valid = is_valid_letterboxd_username(username)
    return jsonify({'username': username, 'valid': valid})


@app.route('/shared_watchlist', methods=['POST'])
def shared_watchlist():
    data = request.get_json() or {}
    usernames = data.get('usernames', [])
    usernames = [u.strip().strip('/') for u in usernames if u and u.strip()]
    if len(usernames) < 2:
        return jsonify({'error': 'need at least two usernames'}), 400

    # validate usernames quickly
    invalid = [u for u in usernames if not is_valid_letterboxd_username(u)]
    if invalid:
        return jsonify({'error': 'invalid_usernames', 'invalid': invalid}), 400

    # prepare cache and scrape lists
    user_watchlists = {}
    logs = []
    to_scrape = []

    for u in usernames:
        cached = USER_CACHE.get(u)
        if cached and (CACHE_TTL_SECONDS is None or (time.time() - cached['ts'] <= (CACHE_TTL_SECONDS or WATCHLIST_CACHE_TTL))):
            # build mapping key->film using normalized path
            mapping = {}
            for f in cached.get('films', []):
                key = _normalize_film_path(f.get('link') or '')
                if key:
                    mapping[key] = f
            user_watchlists[u] = mapping
            logs.append({'username': u, 'cached': True, 'pages': cached.get('pages', []), 'total': len(mapping)})
        else:
            to_scrape.append(u)

    if to_scrape:
        with ThreadPoolExecutor(max_workers=min(6, len(to_scrape))) as ex:
            futures = {ex.submit(scrape_user, u): u for u in to_scrape}
            for fut in as_completed(futures):
                u = futures[fut]
                try:
                    result = fut.result()
                except Exception as e:
                    logs.append({'username': u, 'cached': False, 'pages': [], 'total': 0, 'error': str(e)})
                    user_watchlists[u] = {}
                    continue

                if 'error' in result:
                    logs.append({'username': u, 'cached': False, 'pages': result.get('pages', []), 'total': 0, 'error': result.get('error')})
                    user_watchlists[u] = {}
                    continue

                films = result.get('films', [])
                pages = result.get('pages', [])
                mapping = {}
                for f in films:
                    key = _normalize_film_path(f.get('link') or '')
                    if key:
                        mapping[key] = f
                user_watchlists[u] = mapping
                logs.append({'username': u, 'cached': False, 'pages': pages, 'total': len(mapping)})

    # ensure logs order matches usernames
    logs = sorted(logs, key=lambda x: usernames.index(x['username']) if x['username'] in usernames else 0)

    # compute intersection
    sets = [set(d.keys()) for d in user_watchlists.values()]
    if not sets:
        shared_keys = set()
    else:
        shared_keys = sets[0].copy()
        for s in sets[1:]:
            shared_keys &= s

    # build shared titles (prefer first user's title entries)
    shared = []
    if shared_keys:
        first = usernames[0]
        first_map = user_watchlists.get(first, {})
        for key in sorted(shared_keys):
            title = None
            poster = None
            # prefer title and poster from first map, else any
            if key in first_map:
                title = first_map[key].get('title')
                poster = first_map[key].get('poster')
            else:
                for u in usernames:
                    if key in user_watchlists.get(u, {}):
                        film = user_watchlists[u][key]
                        title = film.get('title')
                        poster = film.get('poster')
                        break
            # build a full letterboxd URL for the film key when possible
            url = None
            try:
                if key and key.startswith('/'):
                    url = LETTERBOXD_BASE + key
                elif key and key.startswith('http'):
                    url = key
                else:
                    url = LETTERBOXD_BASE + '/' + key.lstrip('/')
            except Exception:
                url = None
            shared.append({'title': title or key, 'url': url, 'poster': poster})

    counts = {u: len(user_watchlists.get(u, {})) for u in usernames}

    return jsonify({'usernames': usernames, 'shared': shared, 'counts': counts, 'logs': logs})


@app.route('/clear_cache', methods=['POST'])
def clear_cache():
    """Clear cached watchlists. Accepts JSON {usernames: [u1,u2]} to clear specific entries,
    or empty body to clear the entire in-memory cache.
    """
    data = request.get_json() or {}
    usernames = data.get('usernames')
    cleared = []
    with CACHE_LOCK:
        if not usernames:
            cleared = list(WATCHLIST_CACHE.keys())
            WATCHLIST_CACHE.clear()
        else:
            for u in usernames:
                key = u.strip().strip('/')
                if key in WATCHLIST_CACHE:
                    del WATCHLIST_CACHE[key]
                    cleared.append(key)
    with POSTER_CACHE_LOCK:
        POSTER_CACHE.clear()
    return jsonify({'cleared': cleared})


@app.route('/compare-stream')
def compare_stream():
    usernames = request.args.getlist('user')

    def generate():
        if len(usernames) < 2:
            yield "data: ERROR: Provide at least two usernames\n\n"
            return
        if len(set(usernames)) < 2:
            yield "data: Please enter two distinct usernames\n\n"
            return

        user_watchlists = {}
        to_scrape = []
        for u in usernames:
            cached = USER_CACHE.get(u)
            if cached and (CACHE_TTL_SECONDS is None or (time.time() - cached['ts'] <= CACHE_TTL_SECONDS)):
                user_watchlists[u] = {film['title']: film for film in cached['films']}
                yield f"data: Using cached watchlist for {u}. {len(cached['films'])} films.\n\n"
                continue
            to_scrape.append(u)

        if to_scrape:
            with ThreadPoolExecutor(max_workers=min(4, len(to_scrape))) as ex:
                futures = {ex.submit(scrape_user, u): u for u in to_scrape}
                for fut in as_completed(futures):
                    username = futures[fut]
                    try:
                        result = fut.result()
                    except Exception as e:
                        yield f"data: ERROR scraping {username}: {str(e)}\n\n"
                        continue

                    if 'error' in result:
                        yield f"data: ERROR: {result['error']}.\n\n"
                        continue

                    films = result.get('films', [])
                    yield f"data: {username} done scraping. {len(films)} films collected.\n\n"
                    user_watchlists[username] = {film['title']: film for film in films}

        if len(user_watchlists) >= 2:
            common_titles = set.intersection(*(set(d.keys()) for d in user_watchlists.values()))
            first_user = list(user_watchlists.keys())[0]
            common_films = [user_watchlists[first_user][title] for title in sorted(common_titles)]
            yield f"data: COMPARISON_RESULT:{json.dumps(common_films)}\n\n"
        else:
            yield f"data: ERROR: Not enough valid users to compare.\n\n"

    return Response(stream_with_context(generate()), content_type='text/event-stream')


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)
