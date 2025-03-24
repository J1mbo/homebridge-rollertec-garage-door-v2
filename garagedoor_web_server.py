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

This version uses LED event buffering with suppression after an action.
After an open or close command, the LED event buffer is cleared and new events are recorded.
However, door state interpretation (and hence SSE updates) are deferred until at least 3 seconds of data have been collected.
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
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='[%(levelname)s] %(message)s')

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
    API_KEY = None
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

# ======= Global Variables =======
door_state = "UNKNOWN"  # current door state as text
temperature = 0.0
# event_list stores (timestamp, led_value) tuples.
event_list = []
state_lock = threading.Lock()

# Sliding window duration (in seconds) for LED event analysis.
LED_EVENT_WINDOW = 3.0

# Suppression mode: when True, we are waiting for a fresh 3-sec buffer after an action.
suppress_mode = False
suppression_start_time = 0.0

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

def clean_event_list():
    """
    Remove events older than LED_EVENT_WINDOW seconds.
    """
    global event_list
    now = time.time()
    while event_list and (now - event_list[0][0] > LED_EVENT_WINDOW):
        event_list.pop(0)

def classify_door_state():
    """
    Analyze the sliding window of LED events and classify the door state.
    Uses the predominant LED value and transition frequency.
    """
    clean_event_list()
    if not event_list:
        return "No Input"

    window_duration = event_list[-1][0] - event_list[0][0]
    if window_duration <= 0:
        window_duration = 1.0

    transitions = 0
    previous = event_list[0][1]
    for t, led in event_list[1:]:
        if led != previous:
            transitions += 1
        previous = led
    frequency = transitions / window_duration

    counts = {LED_OFF: 0, LED_GREEN: 0, LED_RED: 0, LED_ORANGE: 0}
    for _, led in event_list:
        counts[led] += 1
    total_events = len(event_list)
    predominant_led = max(counts, key=counts.get)
    ratio = counts[predominant_led] / float(total_events)

    if ratio > 0.8:
        if predominant_led == LED_OFF:
            return "No Input"
        elif predominant_led == LED_GREEN:
            return "OPEN"
        elif predominant_led == LED_RED:
            return "CLOSED"
        elif predominant_led == LED_ORANGE:
            return "MANUALLY STOPPED"

    if predominant_led == LED_GREEN:
        if 0.7 <= frequency <= 1.3:
            return "OPENING"
    elif predominant_led == LED_RED:
        if 0.7 <= frequency <= 1.3:
            return "CLOSING"
    elif predominant_led == LED_ORANGE:
        if frequency >= 3.0:
            return "ERROR"
        else:
            return "MANUALLY STOPPED"

    if frequency >= 3.0:
        return "ERROR"

    return "INDETERMINATE"

def _check_suppression():
    """
    If we are in suppression mode, check whether we have accumulated at least
    LED_EVENT_WINDOW seconds worth of events (or a backup timer has elapsed).
    If so, disable suppression.
    """
    global suppress_mode, suppression_start_time
    now = time.time()
    if event_list:
        span = event_list[-1][0] - event_list[0][0]
    else:
        span = 0
    # If we have a full window of events or 3 seconds have elapsed since suppression began, disable suppression.
    if span >= LED_EVENT_WINDOW or (now - suppression_start_time) >= LED_EVENT_WINDOW:
        suppress_mode = False

def update_state():
    """
    Force a fresh sensor reading, record the LED event, and update door_state.
    When suppression mode is active, record events but do not interpret them until
    a full LED_EVENT_WINDOW of data has been gathered.
    """
    global door_state, suppress_mode
    with state_lock:
        now = time.time()
        # If suppression is active, check if we can end it.
        if suppress_mode:
            # Continue recording events
            open_val = GPIO.input(SENSOR_OPEN)
            close_val = GPIO.input(SENSOR_CLOSE)
            led = led_colour(open_val, close_val)
            if not event_list or event_list[-1][1] != led:
                event_list.append((now, led))
            _check_suppression()
            return door_state  # Return the current state (e.g. OPENING or CLOSING)

        # Normal mode: record the event and interpret it.
        open_val = GPIO.input(SENSOR_OPEN)
        close_val = GPIO.input(SENSOR_CLOSE)
        led = led_colour(open_val, close_val)
        if not event_list or event_list[-1][1] != led:
            event_list.append((now, led))
        new_state = classify_door_state()
        if new_state != door_state:
            logging.info("Door state changed from %s to %s", door_state, new_state)
            door_state = new_state
            event_data = json.dumps({"doorState": door_state, "temperature": temperature})
            push_event(event_data)
        return door_state

# ======= LED Status Updater Thread =======
def led_status_updater():
    """
    Every 1 second, re-read the LED input status and append a sample to the event_list.
    If suppression mode is active, do not interpret the data until the buffer spans LED_EVENT_WINDOW.
    """
    global door_state, suppress_mode
    logging.info("Starting LED status updater thread.")
    while True:
        with state_lock:
            now = time.time()
            # If suppression is active, record events and check if we can end suppression.
            if suppress_mode:
                if event_list and (now - event_list[-1][0] >= 1.0):
                    open_val = GPIO.input(SENSOR_OPEN)
                    close_val = GPIO.input(SENSOR_CLOSE)
                    led = led_colour(open_val, close_val)
                    event_list.append((now, led))
                _check_suppression()
            else:
                # Normal mode: record event if enough time has elapsed since the last sample.
                if not event_list or (now - event_list[-1][0]) >= 1.0:
                    open_val = GPIO.input(SENSOR_OPEN)
                    close_val = GPIO.input(SENSOR_CLOSE)
                    led = led_colour(open_val, close_val)
                    event_list.append((now, led))
                    new_state = classify_door_state()
                    if new_state != door_state:
                        logging.info("LED updater: door state changed from %s to %s", door_state, new_state)
                        door_state = new_state
                        event_data = json.dumps({"doorState": door_state, "temperature": temperature})
                        push_event(event_data)
        time.sleep(1)

# ======= GPIO Event Callback =======
def door_sensor_callback(channel):
    """
    Callback triggered by GPIO events.
    If suppression mode is active, record the event but do not trigger classification
    until a full LED_EVENT_WINDOW of data is available.
    """
    global door_state, suppress_mode
    with state_lock:
        now = time.time()
        if suppress_mode:
            # Record the new event and check suppression.
            open_val = GPIO.input(SENSOR_OPEN)
            close_val = GPIO.input(SENSOR_CLOSE)
            led = led_colour(open_val, close_val)
            if not event_list or event_list[-1][1] != led:
                event_list.append((now, led))
            _check_suppression()
            return
        # Normal mode: record the event and update classification.
        open_val = GPIO.input(SENSOR_OPEN)
        close_val = GPIO.input(SENSOR_CLOSE)
        led = led_colour(open_val, close_val)
        if not event_list or event_list[-1][1] != led:
            event_list.append((now, led))
        new_state = classify_door_state()
        if new_state != door_state:
            logging.info("Sensor callback: door state changed from %s to %s", door_state, new_state)
            door_state = new_state
            event_data = json.dumps({"doorState": door_state, "temperature": temperature})
            push_event(event_data)

GPIO.setmode(GPIO.BCM)
GPIO.setup(SENSOR_OPEN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(SENSOR_CLOSE, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.add_event_detect(SENSOR_OPEN, GPIO.BOTH, callback=door_sensor_callback, bouncetime=200)
GPIO.add_event_detect(SENSOR_CLOSE, GPIO.BOTH, callback=door_sensor_callback, bouncetime=200)
logging.info("GPIO event detection set.")

# ======= Temperature Updater Thread =======
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
                temp_val = round(temp_val * 2) / 2.0  # round to nearest 0.5C
                return temp_val
        except Exception as e:
            logging.error("Temperature read error: %s", e)
            return None
    return None

# ======= API Key Validator =======

def require_api_key(func):
    def wrapper(*args, **kwargs):
        key = request.args.get('api_key') or request.headers.get('X-API-KEY')
        if key != API_KEY:
            logging.warning("Unauthorized access attempt to %s", request.path)
            abort(401)
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper


# ======= API Endpoints =======

@app.route('/status', methods=['GET'])
def status():
    """
    Returns current door state and temperature.
    The LED event buffer is maintained in the background.
    """
    global API_KEY
    client_ip = request.remote_addr
    logging.info("Status endpoint accessed by %s", client_ip)
    if API_KEY is None:
        return jsonify({"doorState": "unconfigured", "temperature": temperature})
    key = request.args.get('api_key') or request.headers.get('X-API-KEY')
    if key != API_KEY:
        logging.warning("Unauthorized /status access attempt from %s", client_ip)
        abort(401)
    with state_lock:
        current_state = door_state
        temp = temperature
    return jsonify({"doorState": current_state, "temperature": temp})

@app.route('/set-key', methods=['POST'])
def set_key():
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
    """
    Initiates door opening.
    Immediately sets door_state to OPENING, clears the LED event buffer,
    and enters suppression mode so that LED events are recorded but not interpreted
    until 3 seconds of new data have been collected.
    """
    global door_state, event_list, suppress_mode, suppression_start_time
    client_ip = request.remote_addr
    logging.info("Open door command received from %s", client_ip)
    with state_lock:
        door_state = "OPENING"
        event_list = []  # Invalidate LED event buffer.
        suppress_mode = True
        suppression_start_time = time.time()
        event_data = json.dumps({"doorState": door_state, "temperature": temperature})
        push_event(event_data)
    GPIO.output(OPEN_CMD_PIN, GPIO.HIGH)
    time.sleep(0.5)
    GPIO.output(OPEN_CMD_PIN, GPIO.LOW)
    logging.info("Door opening initiated.")
    return jsonify({"result": "OPENING"})

@app.route('/close', methods=['POST'])
@require_api_key
def close_door():
    """
    Initiates door closing.
    Immediately sets door_state to CLOSING, clears the LED event buffer,
    and enters suppression mode so that LED events are recorded but not interpreted
    until 3 seconds of new data have been collected.
    """
    global door_state, event_list, suppress_mode, suppression_start_time
    client_ip = request.remote_addr
    logging.info("Close door command received from %s", client_ip)
    with state_lock:
        door_state = "CLOSING"
        event_list = []  # Invalidate LED event buffer.
        suppress_mode = True
        suppression_start_time = time.time()
        event_data = json.dumps({"doorState": door_state, "temperature": temperature})
        push_event(event_data)
    GPIO.output(CLOSE_CMD_PIN, GPIO.HIGH)
    time.sleep(0.5)
    GPIO.output(CLOSE_CMD_PIN, GPIO.LOW)
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
    logging.info("Generating self-signed certificate...")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
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
    cert = x509.CertificateBuilder().subject_name(subject)\
         .issuer_name(issuer)\
         .public_key(key.public_key())\
         .serial_number(x509.random_serial_number())\
         .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))\
         .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))\
         .add_extension(x509.SubjectAlternativeName([x509.DNSName(host_ip)]), critical=False)\
         .sign(key, hashes.SHA256(), default_backend())
    try:
        with open(cert_file, "wb") as f:
             f.write(cert.public_bytes(serialization.Encoding.PEM))
        with open(key_file, "wb") as f:
             f.write(key.private_bytes(encoding=serialization.Encoding.PEM,
                                        format=serialization.PrivateFormat.TraditionalOpenSSL,
                                        encryption_algorithm=serialization.NoEncryption()))
        logging.info("Self-signed certificate and key saved to %s and %s", cert_file, key_file)
    except Exception as e:
        logging.error("Error saving self-signed certificate: %s", e)
# --- End: Self-signed Certificate Generation ---

if __name__ == '__main__':
    logging.info("Garage Door Web Server starting up.")
    time.sleep(0.5)
    update_state()
    CERT_FILE = 'cert.pem'
    KEY_FILE = 'key.pem'
    if not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)):
        generate_self_signed_cert(CERT_FILE, KEY_FILE)
    ssl_context = (CERT_FILE, KEY_FILE)
    t_temp = threading.Thread(target=temperature_updater)
    t_temp.daemon = True
    t_temp.start()
    t_led = threading.Thread(target=led_status_updater)
    t_led.daemon = True
    t_led.start()
    logging.info("Starting Flask server on 0.0.0.0:8443 with SSL.")
    app.run(host='0.0.0.0', port=8443, ssl_context=ssl_context, threaded=True)

