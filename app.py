import datetime
import os
import json
import math
import heapq
from functools import wraps
from flask import Flask, render_template, request, redirect, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# Only allow HTTP (non-HTTPS) OAuth when running locally without a database
# On cloud deployments (DATABASE_URL is set) we use HTTPS so this is not needed
if not os.environ.get('DATABASE_URL'):
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-only-insecure-key-change-me')

# ── DATABASE (PostgreSQL when deployed, JSON files locally) ──────────────────
DATABASE_URL = os.environ.get('DATABASE_URL')

try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False


def get_db_conn():
    if not DATABASE_URL or not PSYCOPG2_AVAILABLE:
        return None
    url = DATABASE_URL
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    try:
        return psycopg2.connect(url)
    except Exception as e:
        print(f'DB connection error: {e}')
        return None


def init_db():
    conn = get_db_conn()
    if not conn:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        username TEXT PRIMARY KEY,
                        password_hash TEXT NOT NULL,
                        display_name TEXT,
                        created_at TEXT
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_data (
                        username TEXT PRIMARY KEY,
                        data JSONB NOT NULL DEFAULT '{}'::jsonb
                    )
                """)
    finally:
        conn.close()


# ── GOOGLE OAUTH CONFIG ──────────────────────────────────
GOOGLE_CLIENT_SECRETS_FILE = 'client_secret_592676366237-538t3lj119h6hfical63sc4sf3b42l5a.apps.googleusercontent.com.json'
GOOGLE_CLIENT_ID     = os.environ.get('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET')
GOOGLE_REDIRECT_URI  = os.environ.get('GOOGLE_REDIRECT_URI', 'http://localhost:5000/oauth2callback')
GOOGLE_SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    'https://www.googleapis.com/auth/calendar.readonly',
]

SPOTIFY_CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID', '')

# ── DATA STORAGE ─────────────────────────────────────────
DATA_DIR = 'data'
USERS_FILE = os.path.join(DATA_DIR, 'users.json')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, 'userdata'), exist_ok=True)

WIDGET_NAMES = {
    'priority': 'Priority Queue',
    'form':     'Add Assignment',
    'calendar': 'Calendar',
    'pomodoro': 'Focus Timer',
    'schedule': 'Weekly Schedule',
    'spotify':  'Study Music',
    'videos':   'Study Videos',
    'practice': 'Practice Test',
    'progress': 'Progress Tracking',
    'coach':    'Study Coach',
}

DEFAULT_LAYOUT = {
    'rows': [
        [{'id': 'priority', 'cols': 7}, {'id': 'form',     'cols': 5}],
        [{'id': 'calendar', 'cols': 7}, {'id': 'pomodoro', 'cols': 5}],
        [{'id': 'schedule', 'cols': 8}, {'id': 'spotify',  'cols': 4}],
    ]
}

RESEARCH_SOURCES = [
    {'title': 'Dunlosky et al. (2013): Effective Learning Techniques',
     'url':   'https://doi.org/10.1177/1529100612453266'},
    {'title': 'Roediger & Karpicke (2006): Testing Effect',
     'url':   'https://doi.org/10.1111/j.1467-9280.2006.01693.x'},
    {'title': 'Cepeda et al. (2006): Distributed Practice Review',
     'url':   'https://doi.org/10.1037/0033-2909.132.3.354'},
    {'title': 'Aeon et al. (2021): Time Management Meta-analysis',
     'url':   'https://doi.org/10.1371/journal.pone.0245066'},
    {'title': 'CDC (2026): Physical Activity Benefits for Students',
     'url':   'https://www.cdc.gov/physical-activity-basics/health-benefits/children.html'},
    {'title': 'AASM Consensus (2016): Recommended Sleep for Teens',
     'url':   'https://doi.org/10.5664/jcsm.5866'},
]


# ── USER STORAGE HELPERS ─────────────────────────────────

def _default_user_data():
    return {
        'assignments':      [],
        'pq':               [],
        'notes':            {},
        'schedule_blocks':  [],
        'career_interests': [],
        'dashboard_layout': DEFAULT_LAYOUT,
    }


def load_users():
    conn = get_db_conn()
    if conn:
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT username, password_hash, display_name, created_at FROM users")
                rows = cur.fetchall()
            return {r['username']: dict(r) for r in rows}
        except Exception as e:
            print(f'DB load_users error: {e}')
        finally:
            conn.close()
    # File fallback
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE) as f:
        return json.load(f)


def save_users(users):
    conn = get_db_conn()
    if conn:
        try:
            with conn:
                with conn.cursor() as cur:
                    for username, u in users.items():
                        cur.execute("""
                            INSERT INTO users (username, password_hash, display_name, created_at)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (username) DO UPDATE SET
                                password_hash = EXCLUDED.password_hash,
                                display_name  = EXCLUDED.display_name,
                                created_at    = EXCLUDED.created_at
                        """, (username, u['password_hash'], u.get('display_name', username), u.get('created_at')))
            return
        except Exception as e:
            print(f'DB save_users error: {e}')
        finally:
            conn.close()
    # File fallback
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)


def get_user_data_file(username):
    return os.path.join(DATA_DIR, 'userdata', f'{username}.json')


def load_user_data(username):
    conn = get_db_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM user_data WHERE username = %s", (username,))
                row = cur.fetchone()
            if row:
                data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                if 'dashboard_layout' not in data:
                    data['dashboard_layout'] = DEFAULT_LAYOUT
                return data
            return _default_user_data()
        except Exception as e:
            print(f'DB load_user_data error: {e}')
        finally:
            conn.close()
    # File fallback
    path = get_user_data_file(username)
    if not os.path.exists(path):
        return _default_user_data()
    with open(path) as f:
        data = json.load(f)
    if 'dashboard_layout' not in data:
        data['dashboard_layout'] = DEFAULT_LAYOUT
    return data


def save_user_data(username, data):
    conn = get_db_conn()
    if conn:
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO user_data (username, data) VALUES (%s, %s)
                        ON CONFLICT (username) DO UPDATE SET data = EXCLUDED.data
                    """, (username, json.dumps(data)))
            return
        except Exception as e:
            print(f'DB save_user_data error: {e}')
        finally:
            conn.close()
    # File fallback
    with open(get_user_data_file(username), 'w') as f:
        json.dump(data, f, indent=2)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


# ── STUDY LOGIC HELPERS ──────────────────────────────────

def _parse_date(date_str):
    try:
        return datetime.datetime.strptime(str(date_str), '%Y-%m-%d').date()
    except Exception:
        return None


def _build_spaced_dates(due_date, today):
    if due_date is None:
        return []
    spaced = []
    for d in (7, 3, 1):
        candidate = due_date - datetime.timedelta(days=d)
        if candidate >= today:
            spaced.append(candidate.isoformat())
    if not spaced and due_date >= today:
        spaced.append(today.isoformat())
    return spaced


def _compute_study_coach(assignments_list, scores_by_assignment):
    today = datetime.date.today()
    pending = []
    total_pending_hours = 0.0

    for a in assignments_list:
        if a.get('status') == 'done':
            continue
        due_date = _parse_date(a.get('due_date'))
        days_left = (due_date - today).days if due_date else 999
        hours = float(a.get('hours', 1) or 1)
        total_pending_hours += hours
        focus_blocks = max(1, math.ceil((hours * 60) / 40))
        score = scores_by_assignment.get(a['assignment'], 0)
        urgency = 'High' if days_left <= 2 else ('Medium' if days_left <= 5 else 'Low')
        if days_left <= 1:
            next_action = 'Run a closed-book retrieval check, then submit a final pass.'
        elif days_left <= 3:
            next_action = 'Do retrieval practice now and schedule one spaced review tomorrow.'
        else:
            next_action = 'Start early with one focus block and spaced reviews this week.'
        pending.append({
            'assignment':     a['assignment'],
            'course':         a['course'],
            'due_date':       a.get('due_date'),
            'days_left':      days_left,
            'focus_blocks':   focus_blocks,
            'retrieval_rounds': 2 if days_left > 1 else 1,
            'spaced_dates':   _build_spaced_dates(due_date, today),
            'interleave_hint': 'Pair this with a different course in your next block.',
            'priority_score': round(score, 2),
            'urgency':        urgency,
            'next_action':    next_action,
        })

    pending_sorted = sorted(pending, key=lambda x: (x['days_left'], -x['priority_score']))
    top_actions = pending_sorted[:3]

    if pending_sorted:
        non_neg = [x['days_left'] for x in pending_sorted if x['days_left'] >= 0]
        furthest = max(non_neg) if non_neg else 1
        planning_days = max(1, min(7, furthest if furthest > 0 else 1))
        daily_minutes = math.ceil((total_pending_hours * 60) / planning_days)
    else:
        daily_minutes = 0

    weekly_protocol = [
        {'id': 'retrieval',  'label': 'Use retrieval practice (self-test, no notes first)'},
        {'id': 'spacing',    'label': 'Schedule at least 2 spaced reviews per major task'},
        {'id': 'interleave', 'label': 'Alternate subjects instead of one long single-subject block'},
        {'id': 'timeblock',  'label': 'Protect daily study time blocks in your calendar'},
        {'id': 'sleep',      'label': 'Sleep target: 8+ hours/night (teens: 8-10)'},
        {'id': 'movement',   'label': 'Add movement breaks to improve attention and memory'},
    ]

    return {
        'top_actions':          top_actions,
        'assignment_plans':     pending_sorted,
        'daily_minutes_target': daily_minutes,
        'weekly_protocol':      weekly_protocol,
    }


def _get_credentials():
    cred_data = session.get('google_credentials')
    if not cred_data:
        return None
    try:
        creds = Credentials(
            token=cred_data['token'],
            refresh_token=cred_data.get('refresh_token'),
            token_uri=cred_data['token_uri'],
            client_id=cred_data['client_id'],
            client_secret=cred_data['client_secret'],
            scopes=cred_data['scopes'],
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            session['google_credentials']['token'] = creds.token
        return creds
    except Exception:
        return None


@app.template_filter('days_from_today')
def days_from_today(date_str):
    try:
        due = datetime.datetime.strptime(str(date_str), '%Y-%m-%d')
        return (due - datetime.datetime.now()).days
    except Exception:
        return 999


# ── AUTH ROUTES ──────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'username' in session:
        return redirect('/')
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        users = load_users()
        if username in users and check_password_hash(users[username]['password_hash'], password):
            session['username']     = username
            session['display_name'] = users[username].get('display_name', username)
            return redirect('/')
        error = 'Invalid username or password.'
    return render_template('login.html', error=error, mode='login')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'username' in session:
        return redirect('/')
    error = None
    if request.method == 'POST':
        username     = request.form.get('username', '').strip().lower()
        display_name = request.form.get('display_name', '').strip()
        password     = request.form.get('password', '')
        confirm      = request.form.get('confirm_password', '')

        if len(username) < 3:
            error = 'Username must be at least 3 characters.'
        elif not username.replace('_', '').isalnum():
            error = 'Username may only contain letters, numbers, and underscores.'
        elif len(password) < 6:
            error = 'Password must be at least 6 characters.'
        elif password != confirm:
            error = 'Passwords do not match.'
        else:
            users = load_users()
            if username in users:
                error = 'That username is already taken.'
            else:
                users[username] = {
                    'password_hash': generate_password_hash(password),
                    'display_name':  display_name or username,
                    'created_at':    datetime.datetime.now().isoformat(),
                }
                save_users(users)
                session['username']     = username
                session['display_name'] = display_name or username
                return redirect('/?new=1')
    return render_template('login.html', error=error, mode='register')


@app.route('/guest')
def guest():
    if 'username' in session:
        return redirect('/')
    # Clear any leftover guest data so each guest session starts fresh
    guest_file = get_user_data_file('__guest__')
    if os.path.exists(guest_file):
        os.remove(guest_file)
    session['username']     = '__guest__'
    session['display_name'] = 'Guest'
    session['is_guest']     = True
    return redirect('/')


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# ── GOOGLE OAUTH HELPERS ─────────────────────────────────

def _make_oauth_flow(state=None):
    """Build a Google OAuth Flow using env vars (for prod) or secrets file (for local)."""
    kwargs = dict(scopes=GOOGLE_SCOPES, redirect_uri=GOOGLE_REDIRECT_URI)
    if state:
        kwargs['state'] = state
    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        client_config = {
            "web": {
                "client_id":                  GOOGLE_CLIENT_ID,
                "client_secret":              GOOGLE_CLIENT_SECRET,
                "auth_uri":                   "https://accounts.google.com/o/oauth2/auth",
                "token_uri":                  "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "redirect_uris":              [GOOGLE_REDIRECT_URI],
            }
        }
        return Flow.from_client_config(client_config, **kwargs)
    return Flow.from_client_secrets_file(GOOGLE_CLIENT_SECRETS_FILE, **kwargs)


# ── GOOGLE OAUTH ROUTES ──────────────────────────────────

@app.route('/login/google')
@login_required
def login_google():
    flow = _make_oauth_flow()
    auth_url, state = flow.authorization_url(
        access_type='offline', prompt='consent', include_granted_scopes='true'
    )
    session['oauth_state'] = state
    return redirect(auth_url)


@app.route('/oauth2callback')
def oauth2callback():
    state = session.get('oauth_state')
    try:
        flow = _make_oauth_flow(state=state)
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        session['google_credentials'] = {
            'token':         creds.token,
            'refresh_token': creds.refresh_token,
            'token_uri':     creds.token_uri,
            'client_id':     creds.client_id,
            'client_secret': creds.client_secret,
            'scopes':        list(creds.scopes) if creds.scopes else GOOGLE_SCOPES,
        }
        service   = build('oauth2', 'v2', credentials=creds)
        user_info = service.userinfo().get().execute()
        session['google_user'] = {
            'name':    user_info.get('name', ''),
            'email':   user_info.get('email', ''),
            'picture': user_info.get('picture', ''),
        }
    except Exception as e:
        print(f'OAuth error: {e}')
    return redirect('/')


@app.route('/logout/google')
@login_required
def logout_google():
    session.pop('google_credentials', None)
    session.pop('google_user', None)
    session.pop('oauth_state', None)
    return redirect('/')


# ── GOOGLE CALENDAR ──────────────────────────────────────

@app.route('/api/google_calendar')
@login_required
def google_calendar():
    creds = _get_credentials()
    if not creds:
        return jsonify({'authenticated': False, 'events': []})
    try:
        service  = build('calendar', 'v3', credentials=creds)
        time_min = (datetime.datetime.utcnow() - datetime.timedelta(days=30)).isoformat() + 'Z'
        time_max = (datetime.datetime.utcnow() + datetime.timedelta(days=90)).isoformat() + 'Z'
        result   = service.events().list(
            calendarId='primary', timeMin=time_min, timeMax=time_max,
            singleEvents=True, orderBy='startTime', maxResults=200,
        ).execute()
        formatted = []
        for ev in result.get('items', []):
            start = ev.get('start', {})
            date  = start.get('date') or start.get('dateTime', '')[:10]
            if date:
                formatted.append({'title': ev.get('summary', 'Event'), 'date': date, 'google': True})
        return jsonify({'authenticated': True, 'events': formatted})
    except Exception as e:
        print(f'Calendar API error: {e}')
        return jsonify({'authenticated': True, 'events': [], 'error': str(e)})


# ── MAIN DASHBOARD ───────────────────────────────────────

@app.route('/')
@login_required
def home():
    username  = session['username']
    user_data = load_user_data(username)

    assignments      = user_data['assignments']
    pq_raw           = user_data.get('pq', [])
    notes            = user_data.get('notes', {})
    dashboard_layout = user_data.get('dashboard_layout', DEFAULT_LAYOUT)
    career_interests = user_data.get('career_interests', [])
    schedule_blocks  = user_data.get('schedule_blocks', [])

    score_map = {}
    if pq_raw:
        max_score   = max(s for s, _ in pq_raw)
        sorted_pq   = []
        for score, name in pq_raw:
            score_map[name] = max(score_map.get(name, 0), score)
            sorted_pq.append(((score / max_score) * 100, name))
        sorted_pq.sort(reverse=True)
    else:
        sorted_pq = []

    study_coach     = _compute_study_coach(assignments, score_map)
    calendar_events = [
        {'title': f"{a['course']}: {a['assignment']}", 'date': a['due_date'], 'course': a['course']}
        for a in assignments
    ]

    is_new_user = request.args.get('new') == '1'

    return render_template(
        'index.html',
        assignments      = assignments,
        pq               = sorted_pq,
        notes            = notes,
        calendar_events  = json.dumps(calendar_events),
        career_interests = career_interests,
        study_coach      = study_coach,
        research_sources = RESEARCH_SOURCES,
        google_user      = session.get('google_user'),
        spotify_client_id= SPOTIFY_CLIENT_ID,
        dashboard_layout = dashboard_layout,
        schedule_blocks  = json.dumps(schedule_blocks),
        username         = username,
        display_name     = session.get('display_name', username),
        widget_names     = WIDGET_NAMES,
        all_widgets      = list(WIDGET_NAMES.keys()),
        is_new_user      = is_new_user,
        is_guest         = session.get('is_guest', False),
    )


# ── LAYOUT API ───────────────────────────────────────────

@app.route('/api/save_layout', methods=['POST'])
@login_required
def save_layout():
    data   = request.get_json()
    layout = data.get('layout')
    if not layout:
        return jsonify({'ok': False, 'error': 'No layout provided'}), 400
    username  = session['username']
    user_data = load_user_data(username)
    user_data['dashboard_layout'] = layout
    save_user_data(username, user_data)
    return jsonify({'ok': True})


# ── SCHEDULE API ─────────────────────────────────────────

@app.route('/api/save_schedule', methods=['POST'])
@login_required
def save_schedule():
    data     = request.get_json()
    blocks   = data.get('blocks', [])
    username = session['username']
    user_data = load_user_data(username)
    user_data['schedule_blocks'] = blocks
    save_user_data(username, user_data)
    return jsonify({'ok': True})


# ── ASSIGNMENT ROUTES ────────────────────────────────────

@app.route('/add', methods=['POST'])
@login_required
def add_assignment():
    username  = session['username']
    user_data = load_user_data(username)

    new_a = {
        'course':      request.form['course'],
        'assignment':  request.form['assignment'],
        'description': request.form.get('description', '').strip(),
        'due_date':    request.form['due_date'],
        'status':      'not started',
    }

    due_date       = datetime.datetime.strptime(new_a['due_date'], '%Y-%m-%d')
    days_remaining = (due_date - datetime.datetime.now()).days

    if days_remaining <= 0:
        score = 1000
    elif days_remaining <= 1:
        score = 10
    elif days_remaining <= 3:
        score = 5
    else:
        score = 1 / days_remaining

    pq = user_data.get('pq', [])
    heapq.heappush(pq, (score, new_a['assignment']))
    user_data['pq'] = pq
    user_data['assignments'].append(new_a)
    save_user_data(username, user_data)
    return redirect('/')


@app.route('/update_status', methods=['POST'])
@login_required
def update_status():
    username  = session['username']
    user_data = load_user_data(username)
    data      = request.get_json()
    for a in user_data['assignments']:
        if a['assignment'] == data.get('assignment'):
            a['status'] = data.get('status')
            break
    save_user_data(username, user_data)
    return jsonify({'ok': True})


@app.route('/save_note', methods=['POST'])
@login_required
def save_note():
    username  = session['username']
    user_data = load_user_data(username)
    data      = request.get_json()
    user_data['notes'][data.get('assignment')] = data.get('note', '')
    save_user_data(username, user_data)
    return jsonify({'ok': True})


@app.route('/delete_assignment', methods=['POST'])
@login_required
def delete_assignment():
    username  = session['username']
    user_data = load_user_data(username)
    data      = request.get_json()
    name      = data.get('assignment')
    user_data['assignments'] = [a for a in user_data['assignments'] if a['assignment'] != name]
    user_data['pq']          = [(s, n) for s, n in user_data.get('pq', []) if n != name]
    heapq.heapify(user_data['pq'])
    user_data['notes'].pop(name, None)
    save_user_data(username, user_data)
    return jsonify({'ok': True})


@app.route('/study_videos')
@login_required
def study_videos():
    assignment_name = request.args.get('assignment', '')
    course          = request.args.get('course', '')
    description     = request.args.get('description', '')
    queries = []
    if description:
        queries.append(description)
    if course and assignment_name:
        queries.append(f'{course} {assignment_name} tutorial')
    if course:
        queries.append(f'{course} explained')
    if description:
        short = description[:60].rsplit(' ', 1)[0]
        if short and short != description:
            queries.append(short + ' tutorial')
    if assignment_name:
        queries.append(f'{assignment_name} how to')
    seen, unique_queries = set(), []
    for q in queries:
        if q.lower() not in seen:
            seen.add(q.lower())
            unique_queries.append(q)
    return render_template('study_videos.html', assignment_name=assignment_name,
                           course=course, description=description, queries=unique_queries)


@app.route('/break')
@login_required
def break_page():
    seconds  = request.args.get('seconds', 300, type=int)
    task     = request.args.get('task', 'your task')
    item_id  = request.args.get('id', '')
    return render_template('break.html', break_seconds=seconds, task_name=task, item_id=item_id)


@app.route('/spotify/callback')
def spotify_callback():
    qs     = request.query_string.decode('utf-8')
    target = f'/?{qs}' if qs else '/'
    return redirect(target)


init_db()

if __name__ == '__main__':
    app.run(debug=not DATABASE_URL)
