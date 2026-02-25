
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



# --- Main Site Routes ---
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/prices.html')
def prices():
    return render_template('prices.html')

@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory('.', filename)


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
