#!/usr/bin/env python
"""
garage_door_server.py

A lightweight Flask web server to control the garage door.
Provides endpoints:
   /status   returns JSON with door state and DS18B20 temperature.
   /open     triggers door open action.
   /close    triggers door close action.
   /events   an SSE endpoint that pushes door state updates.
   /set-key  sets the API key remotely (allowed only if not yet configured).

This version implements advanced logic for determining door state
by analyzing LED sensor events (as in garagedoormonitor.py) and is compatible
with both Python 2.7 and 3.x.
"""

from __future__ import print_function
import threading
import time
import os
import json
import ssl
import sys
import logging

# Configure logging for systemd
try:
    from systemd.journal import JournalHandler
    journal_handler = JournalHandler()
    journal_handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    logging.getLogger().addHandler(journal_handler)
    logging.getLogger().setLevel(logging.INFO)
except ImportError:
    # Fall back to basic configuration if JournalHandler is not available.
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='[%(levelname)s] %(message)s')

# Use the correct Queue module for Python 2 or 3.
try:
    from queue import Queue
except ImportError:
    from Queue import Queue

from flask import Flask, request, jsonify, abort, Response
import RPi.GPIO as GPIO

# --- Begin: Certificate Generation Dependencies ---
import socket
import datetime
try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:
    raise ImportError("The cryptography package is required. Install it with: pip install cryptography")
# --- End: Certificate Generation Dependencies ---

app = Flask(__name__)

# ======= Configuration =======
API_KEY_FILE = "api_key.txt"
if os.path.exists(API_KEY_FILE):
    with open(API_KEY_FILE, "r") as f:
        API_KEY = f.read().strip()
    logging.info("Loaded existing API key.")
else:
    API_KEY = None  # System unconfigured until a key is set
    logging.info("No API key configured yet.")

# GPIO definitions:
SENSOR_OPEN    = 27  # OPEN sensor (green LED)
SENSOR_CLOSE   = 22  # CLOSE sensor (red LED)
OPEN_CMD_PIN   = 24  # triggers open action
CLOSE_CMD_PIN  = 23  # triggers close action

# DS18B20 sensor settings (for temperature)
DS18B20_BASE = "/sys/bus/w1/devices/"
DS18B20_SENSOR_ID = None  # Auto-detect if None

# ======= LED and State Constants =======
LED_OFF    = 0
LED_RED    = 1
LED_GREEN  = 2
LED_ORANGE = 3

STATE_NOINPUT   = 0
STATE_OPEN      = 1
STATE_OPENING   = 2
STATE_CLOSED    = 3
STATE_CLOSING   = 4
STATE_ERROR     = 5
INDETERMINATE   = 6

def state_val_to_text(state):
    if state == STATE_NOINPUT:
        return "STOPPED"
    elif state == STATE_OPEN:
        return "OPEN"
    elif state == STATE_OPENING:
        return "OPENING"
    elif state == STATE_CLOSED:
        return "CLOSED"
    elif state == STATE_CLOSING:
        return "CLOSING"
    elif state == STATE_ERROR:
        return "STOPPED"
    elif state == INDETERMINATE:
        return "INDETERMINATE"
    else:
        return "UNKNOWN"

# ======= Global Variables =======
door_state = "UNKNOWN"  # current door state as text
temperature = 0.0
event_list = []  # List of (timestamp, led_color) tuples.
state_lock = threading.Lock()

# ======= SSE Push Mechanism =======
subscribers = []

def push_event(data):
    """Push JSON-formatted data to all connected SSE subscribers."""
    for q in subscribers:
        q.put(data)
    logging.debug("Pushed event data to %d subscribers.", len(subscribers))

# ======= Helper Functions for LED Logic =======
def led_colour(open_val, close_val):
    """
    Determine LED color based on sensor readings.
    (Sensors are active LOW.)
    """
    if (open_val == 1) and (close_val == 1):
        return LED_OFF
    elif (open_val == 1) and (close_val == 0):
        return LED_RED
    elif (open_val == 0) and (close_val == 1):
        return LED_GREEN
    elif (open_val == 0) and (close_val == 0):
        return LED_ORANGE
    return LED_OFF

def trim_events(mode):
    """
    Trim the global event_list to remove events older than 2 seconds.
    If mode == 1, the most recent event is retained even if older.
    """
    global event_list
    now = time.time()
    while len(event_list) > 1 and (now - event_list[0][0] > 2):
        event_list.pop(0)

def determine_door_state():
    """
    Analyze the event_list to determine the current door state.
    """
    global event_list
    now = time.time()
    trim_events(1)
    if not event_list:
        return state_val_to_text(STATE_NOINPUT)
    if len(event_list) == 1:
        t, led = event_list[0]
        if now - t > 0.3:
            if led == LED_OFF:
                return state_val_to_text(STATE_NOINPUT)
            elif led == LED_GREEN:
                return state_val_to_text(STATE_OPEN)
            elif led == LED_RED:
                return state_val_to_text(STATE_CLOSED)
            elif led == LED_ORANGE:
                return state_val_to_text(STATE_ERROR)
        else:
            return "INDETERMINATE"
    # More than one event exists.
    last_time, last_led = event_list[-1]
    if now - last_time > 2:
        if last_led == LED_OFF:
            return state_val_to_text(STATE_NOINPUT)
        elif last_led == LED_GREEN:
            return state_val_to_text(STATE_OPEN)
        elif last_led == LED_RED:
            return state_val_to_text(STATE_CLOSED)
        elif last_led == LED_ORANGE:
            return state_val_to_text(STATE_ERROR)
    else:
        minduration = 2
        lasteventtime = None
        error_flag = False
        for event in event_list:
            t, led = event
            if lasteventtime is None:
                lasteventtime = t
            else:
                dt = t - lasteventtime
                if dt < minduration:
                    minduration = dt
                lasteventtime = t
            if led == LED_ORANGE:
                error_flag = True
        if error_flag:
            return state_val_to_text(STATE_ERROR)
        if minduration < 0.5:
            return "INDETERMINATE"
        else:
            if len(event_list) == 2 and event_list[0][1] == LED_OFF:
                if event_list[1][1] == LED_GREEN:
                    return state_val_to_text(STATE_OPENING)
                elif event_list[1][1] == LED_RED:
                    return state_val_to_text(STATE_CLOSING)
        return "INDETERMINATE"

def update_state():
    """
    Force a fresh sensor reading and update door_state.
    """
    global door_state
    with state_lock:
        current_time = time.time()
        open_val = GPIO.input(SENSOR_OPEN)
        close_val = GPIO.input(SENSOR_CLOSE)
        led = led_colour(open_val, close_val)
        if not event_list or (event_list and event_list[-1][1] != led):
            event_list.append((current_time, led))
            logging.debug("Appended new sensor event: time=%s, led=%s", current_time, led)
        new_state = determine_door_state()
        if new_state != door_state:
            logging.info("Door state changed from %s to %s", door_state, new_state)
        door_state = new_state
        return new_state

# ======= GPIO Setup =======
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)
GPIO.setup(SENSOR_OPEN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(SENSOR_CLOSE, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(OPEN_CMD_PIN, GPIO.OUT)
GPIO.setup(CLOSE_CMD_PIN, GPIO.OUT)
GPIO.output(OPEN_CMD_PIN, GPIO.LOW)
GPIO.output(CLOSE_CMD_PIN, GPIO.LOW)
logging.info("GPIO initialized.")

# ======= DS18B20 Temperature Reading =======
def read_temperature():
    sensor_folder = None
    if DS18B20_SENSOR_ID is None:
        for d in os.listdir(DS18B20_BASE):
            if d.startswith("28-"):
                sensor_folder = d
                break
    else:
        sensor_folder = DS18B20_SENSOR_ID

    if sensor_folder:
        sensor_file = os.path.join(DS18B20_BASE, sensor_folder, "w1_slave")
        try:
            with open(sensor_file, "r") as f:
                lines = f.readlines()
            if not lines[0].strip().endswith("YES"):
                logging.warning("Temperature sensor read: invalid checksum.")
                return None
            equals_pos = lines[1].find("t=")
            if equals_pos != -1:
                temp_str = lines[1][equals_pos+2:]
                temp_val = float(temp_str) / 1000.0
                return temp_val
        except Exception as e:
            logging.error("Temperature read error: %s", e)
            return None
    return None

# ======= GPIO Event Callback =======
def door_sensor_callback(channel):
    global door_state, event_list
    with state_lock:
        current_time = time.time()
        open_val = GPIO.input(SENSOR_OPEN)
        close_val = GPIO.input(SENSOR_CLOSE)
        led = led_colour(open_val, close_val)
        if not event_list or (event_list and event_list[-1][1] != led):
            event_list.append((current_time, led))
            logging.debug("Sensor callback on channel %s: event appended (time=%s, led=%s).", channel, current_time, led)
        new_state = determine_door_state()
        if new_state != door_state:
            logging.info("Sensor callback: door state changed from %s to %s", door_state, new_state)
            door_state = new_state
            event_data = json.dumps({"doorState": door_state, "temperature": temperature})
            push_event(event_data)

GPIO.add_event_detect(SENSOR_OPEN, GPIO.BOTH, callback=door_sensor_callback, bouncetime=200)
GPIO.add_event_detect(SENSOR_CLOSE, GPIO.BOTH, callback=door_sensor_callback, bouncetime=200)
logging.info("GPIO event detection set.")

# ======= Background Temperature Updater =======
def temperature_updater():
    global temperature, door_state
    logging.info("Starting temperature updater thread.")
    while True:
        temp = read_temperature()
        if temp is not None:
            with state_lock:
                if temp != temperature:
                    logging.info("Temperature changed from %s to %s", temperature, temp)
                    temperature = temp
                    event_data = json.dumps({"doorState": door_state, "temperature": temperature})
                    push_event(event_data)
        time.sleep(5)

t = threading.Thread(target=temperature_updater)
t.daemon = True
t.start()

# ======= Security: API Key Check =======
def require_api_key(func):
    def wrapper(*args, **kwargs):
        key = request.args.get('api_key') or request.headers.get('X-API-KEY')
        if key != API_KEY:
            logging.warning("Unauthorized access attempt to %s from %s", request.path, request.remote_addr)
            abort(401)
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper

# ======= API Endpoints =======

@app.route('/status', methods=['GET'])
def status():
    """
    Returns current door state and temperature.
    """
    global API_KEY
    client_ip = request.remote_addr
    logging.info("Status endpoint accessed by %s", client_ip)
    if API_KEY is None:
        return jsonify({"doorState": "unconfigured", "temperature": temperature})
    else:
        key = request.args.get('api_key') or request.headers.get('X-API-KEY')
        if key != API_KEY:
            logging.warning("Unauthorized /status access attempt from %s", client_ip)
            abort(401)
        update_state()
        with state_lock:
            current_state = door_state
            temp = temperature
        return jsonify({"doorState": current_state, "temperature": temp})

@app.route('/set-key', methods=['POST'])
def set_key():
    """
    Sets the API key remotely if not yet configured.
    """
    global API_KEY
    client_ip = request.remote_addr
    logging.info("Set-key endpoint accessed by %s", client_ip)
    if API_KEY is not None:
        logging.warning("Attempt to reset API key from %s", client_ip)
        abort(401)
    new_key = None
    if request.is_json:
        data = request.get_json()
        new_key = data.get('api_key')
    else:
        new_key = request.form.get('api_key')
    if not new_key:
        logging.error("No API key provided in /set-key request from %s", client_ip)
        return jsonify({"error": "No API key provided"}), 400
    try:
        with open(API_KEY_FILE, "w") as f:
            f.write(new_key)
        os.chmod(API_KEY_FILE, 0o600)
        logging.info("API key saved to file by %s", client_ip)
    except Exception as e:
        logging.error("Failed to save API key: %s", e)
        return jsonify({"error": "Failed to save API key: " + str(e)}), 500
    API_KEY = new_key
    return jsonify({"result": "API key set successfully."})

@app.route('/open', methods=['POST'])
@require_api_key
def open_door():
    client_ip = request.remote_addr
    logging.info("Open door command received from %s", client_ip)
    GPIO.output(OPEN_CMD_PIN, GPIO.HIGH)
    time.sleep(0.5)
    GPIO.output(OPEN_CMD_PIN, GPIO.LOW)
    with state_lock:
        door_state_local = "OPENING"
        door_state_update = door_state_local
        event_data = json.dumps({"doorState": door_state_update, "temperature": temperature})
        push_event(event_data)
    logging.info("Door opening initiated.")
    return jsonify({"result": "OPENING"})

@app.route('/close', methods=['POST'])
@require_api_key
def close_door():
    client_ip = request.remote_addr
    logging.info("Close door command received from %s", client_ip)
    GPIO.output(CLOSE_CMD_PIN, GPIO.HIGH)
    time.sleep(0.5)
    GPIO.output(CLOSE_CMD_PIN, GPIO.LOW)
    with state_lock:
        door_state_local = "CLOSING"
        door_state_update = door_state_local
        event_data = json.dumps({"doorState": door_state_update, "temperature": temperature})
        push_event(event_data)
    logging.info("Door closing initiated.")
    return jsonify({"result": "CLOSING"})

@app.route('/events')
@require_api_key
def events():
    client_ip = request.remote_addr
    logging.info("SSE events connection established from %s", client_ip)
    def event_stream():
        q = Queue()
        subscribers.append(q)
        try:
            while True:
                data = q.get()
                yield "data: " + data + "\n\n"
        except GeneratorExit:
            subscribers.remove(q)
            logging.info("SSE subscriber from %s disconnected.", client_ip)
    return Response(event_stream(), mimetype="text/event-stream")

# --- Begin: Self-signed Certificate Generation ---
def get_primary_ip():
    """Determine the host's primary IPv4 address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    logging.debug("Primary IP determined as %s", ip)
    return ip

def generate_self_signed_cert(cert_file, key_file):
    """Generate a self-signed certificate with the host's primary IPv4 address as the CN."""
    logging.info("Generating self-signed certificate...")
    key = rsa.generate_private_key(
         public_exponent=65537,
         key_size=2048,
         backend=default_backend()
    )
    host_ip = get_primary_ip()
    try:
        if not isinstance(host_ip, unicode):
            host_ip = unicode(host_ip, 'utf-8')
    except NameError:
        pass

    subject = issuer = x509.Name([
         x509.NameAttribute(NameOID.COUNTRY_NAME, u"US"),
         x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, u"State"),
         x509.NameAttribute(NameOID.LOCALITY_NAME, u"Locality"),
         x509.NameAttribute(NameOID.ORGANIZATION_NAME, u"Self-Signed"),
         x509.NameAttribute(NameOID.COMMON_NAME, host_ip),
    ])
    cert = x509.CertificateBuilder().subject_name(subject) \
         .issuer_name(issuer) \
         .public_key(key.public_key()) \
         .serial_number(x509.random_serial_number()) \
         .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1)) \
         .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365)) \
         .add_extension(x509.SubjectAlternativeName([x509.DNSName(host_ip)]), critical=False) \
         .sign(key, hashes.SHA256(), default_backend())
    try:
        with open(cert_file, "wb") as f:
             f.write(cert.public_bytes(serialization.Encoding.PEM))
        with open(key_file, "wb") as f:
             f.write(key.private_bytes(
                 encoding=serialization.Encoding.PEM,
                 format=serialization.PrivateFormat.TraditionalOpenSSL,
                 encryption_algorithm=serialization.NoEncryption()
             ))
        logging.info("Self-signed certificate and key saved to %s and %s", cert_file, key_file)
    except Exception as e:
        logging.error("Error saving self-signed certificate: %s", e)
# --- End: Self-signed Certificate Generation ---

# ======= Initial Sensor Read and Main Runner =======
if __name__ == '__main__':
    logging.info("Garage Door Web Server starting up.")
    # Brief wait for initial sensor read.
    time.sleep(0.5)
    update_state()

    # Check for certificate files; generate if missing.
    CERT_FILE = 'cert.pem'
    KEY_FILE = 'key.pem'
    if not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)):
        generate_self_signed_cert(CERT_FILE, KEY_FILE)

    ssl_context = (CERT_FILE, KEY_FILE)
    logging.info("Starting Flask server on 0.0.0.0:8443 with SSL.")
    app.run(host='0.0.0.0', port=8443, ssl_context=ssl_context, threaded=True)

