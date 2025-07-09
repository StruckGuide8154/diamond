
import os
import json
import redis
import datetime
from io import BytesIO

from flask import Flask, render_template, request, redirect, url_for, Response, jsonify, session, render_template_string, send_from_directory, flash, send_file
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

# --- App Initialization ---
# By default, Flask looks for templates in a 'templates' folder.
# To make it look in the current directory ('.') where app.py is,
# we specify the 'template_folder' argument.
app = Flask(__name__, template_folder='.')

# --- Configuration ---
app.secret_key = os.environ.get('SECRET_KEY', 'default-dev-secret-key-for-pie-services')
REDIS_URL = os.environ.get('REDIS_URL')
REDIS_KEY = 'PIE'  # Unique key for this business in Redis

# --- Redis Connection ---
if not REDIS_URL:
    raise RuntimeError("REDIS_URL environment variable is not set. The application cannot start.")
try:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
    print("Successfully connected to Redis for PIE Services.")
except redis.exceptions.ConnectionError as e:
    raise RuntimeError(f"Could not connect to Redis: {e}")

# --- User and Login Management ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, id, username, password_hash):
        self.id = id
        self.username = username
        self.password_hash = password_hash

@login_manager.user_loader
def load_user(user_id):
    try:
        all_data = get_all_data()
        user_data = all_data.get('users', {}).get(str(user_id))
        if user_data:
            return User(id=int(user_id), username=user_data['username'], password_hash=user_data['password_hash'])
    except Exception as e:
        print(f"Error loading user: {e}")
    return None

# --- Data Helper Functions (Redis-centric) ---
def get_default_data():
    """Returns the default data structure for the entire application."""
    admin_user_id = '1'
    return {
        "contact_submissions": [],
        "users": {
            admin_user_id: {"username": "admin", "password_hash": generate_password_hash('password')}
        }
    }

def get_all_data():
    """Loads all business data from Redis. Initializes with defaults if not found."""
    json_data = redis_client.get(REDIS_KEY)
    if not json_data:
        print(f"No data found for key '{REDIS_KEY}'. Initializing with default data.")
        default_data = get_default_data()
        save_all_data(default_data)
        return default_data
    return json.loads(json_data)

def save_all_data(data):
    """Saves the entire data blob to Redis as a JSON string."""
    redis_client.set(REDIS_KEY, json.dumps(data, indent=2))

# --- Main Site Routes ---
@app.route('/')
def home():
    """Serves the index.html file."""
    return render_template('index.html')

@app.route('/img.jpg')
def serve_image():
    """Serves the img.jpg file from the current directory."""
    return send_from_directory('.', 'img.jpg')

@app.route('/contact', methods=['POST'])
def contact():
    """Handles contact form submissions and saves them to Redis."""
    try:
        data = request.get_json()
        if not data or not data.get('name') or not data.get('email') or not data.get('message'):
            return jsonify({'success': False, 'error': 'Missing required fields.'}), 400
        
        submission = {
            'name': data.get('name'),
            'email': data.get('email'),
            'company': data.get('company', ''),
            'service': data.get('service', ''),
            'message': data.get('message'),
            'timestamp': datetime.datetime.utcnow().isoformat()
        }
        
        all_data = get_all_data()
        all_data.setdefault('contact_submissions', []).append(submission)
        save_all_data(all_data)
        
        return jsonify({'success': True, 'message': 'Message received!'})
    except Exception as e:
        print(f"Error in /contact: {e}")
        return jsonify({'success': False, 'error': 'Server error.'}), 500

# --- Admin Panel Routes ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('admin'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        all_data = get_all_data()
        for user_id, user_data in all_data.get('users', {}).items():
            if user_data['username'] == username and check_password_hash(user_data['password_hash'], password):
                user_obj = User(id=int(user_id), username=user_data['username'], password_hash=user_data['password_hash'])
                login_user(user_obj)
                return redirect(url_for('admin'))
        flash("Invalid username or password", "error")
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))

@app.route('/admin')
@login_required
def admin():
    all_data = get_all_data()
    submissions = sorted(all_data.get('contact_submissions', []), key=lambda x: x['timestamp'], reverse=True)
    return render_template_string(ADMIN_TEMPLATE, submissions=submissions)

@app.route('/admin/download_db')
@login_required
def download_db():
    all_data = get_all_data()
    # Remove password hashes from backup for security
    if 'users' in all_data:
        for uid, user in all_data.get('users', {}).items():
            user.pop('password_hash', None)
            
    json_data_str = json.dumps(all_data, indent=2)
    buffer = BytesIO(json_data_str.encode('utf-8'))
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f'pie_services_backup_{timestamp}.json',
        mimetype='application/json'
    )

@app.route('/admin/restore_db', methods=['POST'])
@login_required
def restore_db():
    if 'backup_file' not in request.files:
        flash('No backup file provided.', 'error')
        return redirect(url_for('admin'))
    file = request.files['backup_file']
    if file.filename == '' or not file.filename.endswith('.json'):
        flash('Invalid backup file type. Please upload a .json file.', 'error')
        return redirect(url_for('admin'))
    try:
        backup_content = file.read().decode('utf-8')
        new_data = json.loads(backup_content)
        if 'contact_submissions' not in new_data:
            flash('Invalid backup file structure. Missing "contact_submissions" key.', 'error')
            return redirect(url_for('admin'))
        
        # Restore user data but keep current passwords
        current_data = get_all_data()
        new_data['users'] = current_data.get('users', {})
        
        save_all_data(new_data)
        flash('Database restored successfully!', 'success')
    except Exception as e:
        flash(f'Error restoring database: {e}', 'error')
    return redirect(url_for('admin'))

# --- HTML Templates for Admin Panel ---
LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PIE Services Admin Login</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&family=Playfair+Display:wght@700&display=swap" rel="stylesheet">
    <style>
        :root { --primary-dark: #0a1220; --primary-navy: #1a2332; --accent-gold: #c9a876; --text-primary: #ffffff; }
        body { font-family: 'Inter', sans-serif; background: var(--primary-dark); color: var(--text-primary); display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
        .login-container { background: var(--primary-navy); padding: 3rem; border-radius: 16px; border: 1px solid rgba(255,255,255,0.1); box-shadow: 0 8px 32px rgba(0,0,0,0.1); width: 100%; max-width: 400px; text-align: center; }
        .login-title { font-family: 'Playfair Display', serif; font-size: 2rem; margin-bottom: 2rem; }
        .login-title span { color: var(--accent-gold); }
        .form-group { margin-bottom: 1.5rem; text-align: left; }
        label { display: block; margin-bottom: 0.5rem; font-weight: 500; }
        input { width: 100%; padding: 0.75rem 1rem; background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.2); border-radius: 8px; color: var(--text-primary); font-size: 1rem; }
        .btn { padding: 0.75rem 1.5rem; border: none; border-radius: 50px; background: var(--accent-gold); color: var(--primary-dark); font-weight: 600; cursor: pointer; width: 100%; font-size: 1rem; }
        .flash { padding: 1rem; margin-bottom: 1.5rem; border-radius: 8px; font-weight: 500; }
        .flash.error { background: #5c2c31; border: 1px solid #dc3545; color: #f8d7da; }
    </style>
</head>
<body>
    <div class="login-container">
        <h1 class="login-title">PIE Services <span>Admin</span></h1>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash {{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <form method="post">
            <div class="form-group"><label for="username">Username</label><input type="text" id="username" name="username" required></div>
            <div class="form-group"><label for="password">Password</label><input type="password" id="password" name="password" required></div>
            <button type="submit" class="btn">Login</button>
        </form>
    </div>
</body>
</html>
"""

ADMIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PIE Services Admin Panel</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Playfair+Display:wght@700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
    <style>
        :root { --primary-dark: #0a1220; --primary-navy: #1a2332; --accent-gold: #c9a876; --text-primary: #ffffff; --text-secondary: #b8bcc8; --glass-border: rgba(255, 255, 255, 0.2); }
        body { font-family: 'Inter', sans-serif; background-color: var(--primary-dark); color: var(--text-secondary); margin: 0; padding: 0; }
        .container { max-width: 1200px; margin: 2rem auto; padding: 0 1rem; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 2rem; }
        .title { font-family: 'Playfair Display', serif; font-size: 2.5rem; color: var(--text-primary); }
        .title span { color: var(--accent-gold); }
        .btn { display: inline-block; padding: 0.75rem 1.5rem; border-radius: 50px; text-decoration: none; font-weight: 600; cursor: pointer; border: none; }
        .btn-logout { background: transparent; border: 1px solid var(--glass-border); color: var(--text-primary); }
        .btn-primary { background: var(--accent-gold); color: var(--primary-dark); }
        .card { background: var(--primary-navy); border: 1px solid var(--glass-border); border-radius: 16px; padding: 1.5rem; margin-bottom: 2rem; }
        .card-header { font-family: 'Playfair Display', serif; font-size: 1.5rem; color: var(--text-primary); margin-bottom: 1rem; }
        .db-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 2rem; }
        .db-actions p { margin: 0 0 1rem 0; font-size: 0.9rem; }
        .db-actions input[type="file"] { margin-bottom: 1rem; }
        .submissions-list { list-style: none; padding: 0; }
        .submission-item { background: rgba(0,0,0,0.2); padding: 1.5rem; border-radius: 12px; margin-bottom: 1rem; border-left: 4px solid var(--accent-gold); }
        .submission-item p { margin: 0 0 0.5rem 0; }
        .submission-item strong { color: var(--text-primary); }
        .timestamp { font-size: 0.8rem; opacity: 0.7; text-align: right; }
        .flash { padding: 1rem; margin-bottom: 1.5rem; border-radius: 8px; font-weight: 500; }
        .flash.error { background: #5c2c31; border: 1px solid #dc3545; color: #f8d7da; }
        .flash.success { background: #1c4b4f; border: 1px solid #28a745; color: #d1e7dd; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1 class="title">PIE Services <span>Admin</span></h1>
            <a href="{{ url_for('logout') }}" class="btn btn-logout">Logout</a>
        </div>
        
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash {{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        <div class="card">
            <h2 class="card-header">Database Management</h2>
            <div class="db-actions">
                <div>
                    <h4>Download Backup</h4>
                    <p>Download all contact submissions as a single JSON file.</p>
                    <a href="{{ url_for('download_db') }}" class="btn btn-primary"><i class="fas fa-download"></i> Download</a>
                </div>
                <div>
                    <h4>Restore from Backup</h4>
                    <p>Replace database with a .json file. <strong style="color:#dc3545;">This will overwrite current submissions.</strong></p>
                    <form action="{{ url_for('restore_db') }}" method="post" enctype="multipart/form-data">
                        <input type="file" name="backup_file" accept=".json" required>
                        <button type="submit" class="btn btn-primary"><i class="fas fa-upload"></i> Restore</button>
                    </form>
                </div>
            </div>
        </div>

        <div class="card">
            <h2 class="card-header">Contact Form Submissions</h2>
            {% if submissions %}
                <ul class="submissions-list">
                {% for sub in submissions %}
                    <li class="submission-item">
                        <p><strong>Name:</strong> {{ sub.name }}</p>
                        <p><strong>Email:</strong> <a href="mailto:{{ sub.email }}" style="color:var(--accent-gold);">{{ sub.email }}</a></p>
                        {% if sub.company %}<p><strong>Company:</strong> {{ sub.company }}</p>{% endif %}
                        {% if sub.service %}<p><strong>Service Interest:</strong> {{ sub.service }}</p>{% endif %}
                        <p><strong>Message:</strong> {{ sub.message }}</p>
                        <p class="timestamp">Received: {{ sub.timestamp.split('T')[0] }} at {{ sub.timestamp.split('T')[1].split('.')[0] }} UTC</p>
                    </li>
                {% endfor %}
                </ul>
            {% else %}
                <p>No contact submissions yet.</p>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""

# --- Main Entry Point ---
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 80))
    print("=================================================================")
    print(">>> Starting PIE Services Web Server...")
    print(f">>> Server will run on host 0.0.0.0 and port {port}.")
    if port == 80:
      print(">>> NOTE: Running on port 80 may require administrator rights.")
    print(">>> Access the site at http://127.0.0.1" + (f":{port}" if port != 80 else ""))
    print("=================================================================")
    app.run(host='0.0.0.0', port=port, debug=False)
