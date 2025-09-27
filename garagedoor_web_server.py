#!/usr/bin/env python
"""
garagedoor_server.py

See: https://github.com/J1mbo/homebridge-rollertec-garage-door-v2

A lightweight Flask web server to control the garage door.
Provides endpoints:
   /status   returns JSON with door state and DS18B20 temperature.
   /open     triggers door open action.
   /close    triggers door close action.
   /events   an SSE endpoint that pushes door state updates.
   /set-key  sets the API key remotely (allowed only if not yet configured).

This version adds a 2s inactivity heartbeat:
- Every 2 seconds, if and only if **no LED events** were received in that period,
  we poll the LEDs to determine steady state. If a new state is determined (e.g. OPEN),
  it is published to any connected SSE clients.

It retains LED event buffering and hysteresis logic for robust state transitions.

Additional logic in this update:
1) **STOPPED debounce for SSE** — If the last published state was CLOSED or OPEN,
   then a transition to STOPPED will **not** be published until STOPPED has been
   continuously recorded for 10 seconds. If the state reverts to the previous
   CLOSED/OPEN within those 10 seconds, neither the STOPPED nor the revert will
   be published.
2) **Enforced next-state rule** — The immediate next published state after
   CLOSED is always OPENING, and after OPEN is always CLOSING. From OPENING/CLOSING
   the door may transition to OPEN, CLOSED, or STOPPED.
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
import pigpio
import atexit

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



# ======= DEFINITIONS =======

# Certificate files and api key file
CERT_FILE    = "cert.pem"
KEY_FILE     = "key.pem"
API_KEY_FILE = "api_key.txt"
API_KEY      = None

# GPIO Pins:
SENSOR_OPEN    = 27  # OPEN sensor (green LED)
SENSOR_CLOSE   = 22  # CLOSE sensor (red LED)
OPEN_CMD_PIN   = 24  # triggers open action - connect to T15 on Rollertec
CLOSE_CMD_PIN  = 23  # triggers close action - connect to T13 on Rollertec

# DS18B20 sensor settings (provides system temperature)
# requires dtoverlay=w1-gpio in /boot/firmware/config.txt
DS18B20_BASE = "/sys/bus/w1/devices/"
DS18B20_SENSOR_ID = None  # Auto-detect if None

# ======= LED and State Constants =======
LED_OFF    = 0
LED_RED    = 1
LED_GREEN  = 2
LED_ORANGE = 3

# Note: Status values used within this script are aligned to Apple Homekit
# return values:
#   0 - No input detected
#   1 - OPEN
#   2 - Door Opening
#   3 - CLOSED
#   4 - Door Closing
#   5 - Jammed/Error
STATE_NOINPUT = 0
STATE_OPEN    = 1
STATE_OPENING = 2
STATE_CLOSED  = 3
STATE_CLOSING = 4
STATE_ERROR   = 5
INDETERMINATE = 6

# Heartbeat config
HEARTBEAT_INTERVAL_SEC = 2.0  # run heartbeat only if no events for this long

# ======= Global Variables =======

door_state  = STATE_NOINPUT
temperature = 0.0

# pigpio instance
pi = None

# Flash/event history for LED-based movement/error classification
# Each item: (timestamp_monotonic, led_colour)
eventlist = []
trim_lock = threading.Lock()
MAX_EVENTS = 400  # ~6-8s of history depending on flash rate

# ---- Movement/flash classification constants ----
FLASH_WINDOW_SEC       = 4.0   # how many seconds of samples to analyze
FLASH_PERIOD_MIN       = 0.6   # minimum expected half-cycle for moving flash
FLASH_PERIOD_MAX       = 1.6   # maximum expected half-cycle for moving flash
FLASH_CV_MAX           = 0.35  # max coeff of variation for intervals to count as regular
DWELL_SEC_STEADY       = 0.50  # dwell to commit to OPEN/CLOSED
DWELL_SEC_MOVING       = 0.10  # dwell to commit to OPENING/CLOSING
DWELL_SEC_ERROR        = 0.00  # dwell for ERROR (immediate)

# movement tracking for stronger state machine
MOVEMENT = {"active": False, "target": None, "since": 0.0}  # target: STATE_OPEN or STATE_CLOSED

# hysteresis tracking
STABLE   = {"state": STATE_NOINPUT, "since": 0.0}
PROPOSED = {"state": INDETERMINATE, "since": 0.0}

# STOPPED SSE debounce tracker (10s continuous STOPPED after CLOSED/OPEN)
STOPPED_SUPPRESS = {"active": False, "since": 0.0, "prior_text": None}
STOPPED_DEBOUNCE_SEC = 10.0

# locks to control access to critical elements
state_lock = threading.Lock()


# ======= SSE Push Mechanism =======
subscribers = []

def push_event(data):
    """Push JSON-formatted data to all connected SSE subscribers."""
    for q in subscribers:
        q.put(data)
    logging.debug("Pushed event data to %d subscribers.", len(subscribers))


def _maybe_publish_or_hold(old_text, new_text, payload_json):
    """Implements the STOPPED debounce/suppression policy for SSE notifications.

    Rules:
    - If transitioning from CLOSED/OPEN -> STOPPED, start a 10s timer and hold notifications.
    - While held:
      * If we remain STOPPED for >=10s, publish a single STOPPED notification, then clear hold.
      * If we revert to the prior CLOSED/OPEN within 10s, cancel the hold and suppress that revert too.
      * If we go to any other state (e.g., OPENING/CLOSING), cancel the hold and publish that new state normally.
    - All other transitions publish immediately.
    """
    # If a hold is active, decide what to do
    if STOPPED_SUPPRESS["active"]:
        held_prior = STOPPED_SUPPRESS["prior_text"]
        held_since = STOPPED_SUPPRESS["since"]
        elapsed = time.monotonic() - held_since

        if new_text == "STOPPED":
            if elapsed >= STOPPED_DEBOUNCE_SEC:
                # Now it's been STOPPED long enough — publish and clear hold
                STOPPED_SUPPRESS.update(active=False, since=0.0, prior_text=None)
                logging.info("STOPPED persisted %.1fs after %s; publishing STOPPED.", elapsed, held_prior)
                push_event(payload_json)
            else:
                # Still within debounce window; suppress
                logging.debug("Holding STOPPED (%.1fs < %.1fs)", elapsed, STOPPED_DEBOUNCE_SEC)
            return  # In either case we do not fall through

        if new_text == held_prior and elapsed < STOPPED_DEBOUNCE_SEC:
            # Reverted within window — cancel hold and suppress this revert
            logging.info("STOPPED reverted to %s within %.1fs; suppressing both.", held_prior, elapsed)
            STOPPED_SUPPRESS.update(active=False, since=0.0, prior_text=None)
            return

        # Any other state ends the hold; publish it normally
        if new_text not in ("STOPPED", held_prior):
            logging.info("STOPPED hold cancelled by transition to %s after %.1fs.", new_text, elapsed)
            STOPPED_SUPPRESS.update(active=False, since=0.0, prior_text=None)
            push_event(payload_json)
            return

    # No hold active: check if we should start one
    if old_text in ("CLOSED", "OPEN") and new_text == "STOPPED":
        STOPPED_SUPPRESS.update(active=True, since=time.monotonic(), prior_text=old_text)
        logging.info("Starting STOPPED debounce (10s) from %s.", old_text)
        return  # suppress for now

    # Default: publish immediately
    push_event(payload_json)


def publish_state_change(new_state):
    """
    Updates the global door_state (if changed), logs the change, and pushes an SSE event.
    Applies STOPPED debounce/suppression rules for SSE.
    """
    global door_state, temperature
    with state_lock:
        if new_state == door_state or new_state == INDETERMINATE:
            return
        old_state = door_state
        door_state = new_state
        old_text = state_val_to_text(old_state)
        new_text = state_val_to_text(new_state)
        temp_snapshot = temperature

    # Build payload outside the lock
    event_data = json.dumps({"doorState": new_text, "temperature": temp_snapshot})
    logging.info("Door state changed from %s to %s", old_text, new_text)

    # Decide whether to publish now or hold
    _maybe_publish_or_hold(old_text, new_text, event_data)


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
    Trims out records from the event list.

    If mode == 0, remove any record older than 2 seconds.
    If mode == 1, keep the most recent record (steady-state) even if older than 2 seconds.
    If mode == 2, removes all events

    Returns True if eventlist is non-empty after trimming, otherwise False.
    """
    global eventlist

    # Acquire the lock to prevent re-entrance
    if not trim_lock.acquire(blocking=False):
        return bool(eventlist)

    try:
        if mode == 2:
            eventlist.clear()
        else:
            # Remove events while the list is not empty and the oldest event is too old.
            # For mode 1, stop if only one event remains.
            now = time.time()
            while eventlist and (now - eventlist[0][0] > 2):
                if mode == 1 and len(eventlist) == 1:
                    break  # Keep the most recent record
                eventlist.pop(0)
    finally:
        trim_lock.release()


def determine_state():
    """
    Analyzes a snapshot of the global eventlist (protected by state_lock)
    to determine the current state.

    Returns one of the state codes:
      - STATE_NOINPUT: No valid input (disconnected or initialising)
      - STATE_OPEN:    Open state detected
      - STATE_CLOSED:  Closed state detected
      - STATE_ERROR:   Error state indicated
      - STATE_OPENING: Transitioning to open (flashing sequence)
      - STATE_CLOSING: Transitioning to closed (flashing sequence)
      - INDETERMINATE: Cannot determine state reliably
    """
    # Acquire the lock to trim and copy the event list
    with state_lock:
        trim_events(1)
        events_copy = eventlist.copy()

    # If no events are available, return no input.
    if not events_copy:
        return STATE_NOINPUT

    # Use the copied event list for processing
    latest_event_time, latest_led = events_copy[-1]
    now = time.time()

    # If the latest reading is older than 2 seconds, base state on that event
    if now - latest_event_time > 2:
        if latest_led == LED_OFF:
            return STATE_NOINPUT
        elif latest_led == LED_GREEN:
            return STATE_OPEN
        elif latest_led == LED_RED:
            return STATE_CLOSED
        elif latest_led == LED_ORANGE:
            return STATE_ERROR
    else:
        # For recent events, analyze the series for error states and flash durations
        min_duration = float('inf')
        previous_time = None
        state = INDETERMINATE

        for timestamp, led in events_copy:
            if previous_time is not None:
                interval = timestamp - previous_time
                if interval < min_duration:
                    min_duration = interval
            previous_time = timestamp

            # If any event indicates an error, mark the state as error.
            if led == LED_ORANGE:
                state = STATE_ERROR

        # Quick flashes (less than 0.6 seconds apart) yield an error state also
        if min_duration < 0.6:
            state = STATE_ERROR
        else:
            # Only update state if no error was found
            if state != STATE_ERROR:
                if latest_led == LED_OFF:
                    state = INDETERMINATE
                else:
                    # Check for a simple two-event sequence indicating opening/closing
                    if len(events_copy) == 2 and events_copy[0][1] == LED_OFF:
                        if events_copy[1][1] == LED_GREEN:
                            state = STATE_OPENING
                        elif events_copy[1][1] == LED_RED:
                            state = STATE_CLOSING
        return state



def state_val_to_text(state):
  # converts a state code to HomeBridge return value
  # these will be logged in the status file and reported by status.sh when called by HomeBridge
  retval = ""
  if state == STATE_NOINPUT: retval = "STOPPED"
  if state == STATE_OPEN:    retval = "OPEN"
  if state == STATE_OPENING: retval = "OPENING"
  if state == STATE_CLOSED:  retval = "CLOSED"
  if state == STATE_CLOSING: retval = "CLOSING"
  if state == STATE_ERROR:   retval = "STOPPED"
  return retval



# ======= GPIO Callback & State Logic (pigpio) =======

def _map_led_to_state(open_val, close_val):
    # Inputs are pulled-up. H11L1M drives low when LED is on.
    if open_val == 1 and close_val == 1:
        return STATE_NOINPUT
    if open_val == 0 and close_val == 1:
        return STATE_OPEN
    if open_val == 1 and close_val == 0:
        return STATE_CLOSED
    # both low – treat as controller error
    return STATE_ERROR

def _append_led_event(led):
    """Append a LED sample if it differs from the previous; trim to MAX_EVENTS."""
    with trim_lock:
        now = time.monotonic()
        if eventlist and eventlist[-1][1] == led:
            return
        eventlist.append((now, led))
        cutoff = now - FLASH_WINDOW_SEC * 2
        while eventlist and (len(eventlist) > MAX_EVENTS or eventlist[0][0] < cutoff):
            eventlist.pop(0)

def _last_event_time_monotonic():
    with trim_lock:
        return eventlist[-1][0] if eventlist else 0.0

def _classify_flash_pattern():
    """Analyze recent LED samples to decide MOVING vs ERROR vs steady.

    Returns one of: STATE_OPENING, STATE_CLOSING, STATE_ERROR, STATE_OPEN, STATE_CLOSED, STATE_NOINPUT, INDETERMINATE.
    Heuristics:
      - If any LED_ORANGE in window -> STATE_ERROR.
      - Consider a window of last ~4s. Build a sequence of transitions ignoring consecutive duplicates.
      - Detect a regular OFF<->ON cadence with period 0.6–1.6s and coefficient of variation < 0.35.
      - Direction: the most recent non-OFF colour decides: GREEN→OPENING, RED→CLOSING.
      - If cadence not regular enough, fall back to latest steady LED to map OPEN/CLOSED/NOINPUT.
    """
    with trim_lock:
        if not eventlist:
            return STATE_NOINPUT
        now = time.monotonic()
        window = [(t, c) for (t, c) in eventlist if now - t <= FLASH_WINDOW_SEC]
        if not window:
            return STATE_NOINPUT

    # Quick checks
    if any(c == LED_ORANGE for _, c in window):
        return STATE_ERROR

    # De-duplicate consecutive states to get edges
    seq = []
    for t, c in window:
        if not seq or seq[-1][1] != c:
            seq.append((t, c))

    if len(seq) < 4:
        # not enough edges to infer cadence; fall back to latest
        latest = seq[-1][1]
        if latest == LED_GREEN:
            return STATE_OPEN
        if latest == LED_RED:
            return STATE_CLOSED
        if latest == LED_OFF:
            return STATE_NOINPUT
        return STATE_ERROR

    # Build intervals between edges
    intervals = [seq[i][0] - seq[i-1][0] for i in range(1, len(seq))]
    if not intervals:
        return INDETERMINATE

    avg = sum(intervals) / len(intervals)
    # robust dispersion (mean absolute deviation)
    mad = sum(abs(x - avg) for x in intervals) / len(intervals)

    # Expect regular flashing in 0.6–1.6s per edge (i.e., ~0.3–0.8 Hz full cycle)
    regular = FLASH_PERIOD_MIN <= avg <= FLASH_PERIOD_MAX and (mad / max(avg, 1e-6)) < FLASH_CV_MAX

    # Ensure pattern is OFF<->(RED or GREEN) alternating, not OFF<->OFF noise
    alt_ok = all(seq[i][1] != seq[i-1][1] for i in range(1, len(seq))) and any(c in (LED_RED, LED_GREEN) for _, c in seq)

    if regular and alt_ok:
        # Direction by last non-OFF colour
        non_off = next((c for _, c in reversed(seq) if c != LED_OFF), LED_OFF)
        if non_off == LED_GREEN:
            return STATE_OPENING
        if non_off == LED_RED:
            return STATE_CLOSING

    # Fallback to steady-state mapping
    latest = seq[-1][1]
    if latest == LED_GREEN:
        return STATE_OPEN
    if latest == LED_RED:
        return STATE_CLOSED
    if latest == LED_OFF:
        return STATE_NOINPUT
    return STATE_ERROR


def _enforce_next_state_rule(current_stable, proposed):
    """Enforce canonical path and prevent false direction flips.
    Rules:
      - CLOSED -> OPENING (never CLOSED -> OPEN directly)
      - OPEN   -> CLOSING (never OPEN -> CLOSED directly)
      - While moving, don't flip OPENING<->CLOSING unless a stable end state or STOPPED/ERROR intervenes.
      - Coerce nonsensical proposals (e.g., CLOSED->CLOSING) to the only valid direction.
    """
    # From CLOSED, don't jump straight to OPEN; require OPENING first
    if current_stable == STATE_CLOSED and proposed == STATE_OPEN:
        return STATE_OPENING
    # From OPEN, don't jump straight to CLOSED; require CLOSING first
    if current_stable == STATE_OPEN and proposed == STATE_CLOSED:
        return STATE_CLOSING
    # Don't reverse direction mid-move without stopping or reaching an end
    if current_stable == STATE_OPENING and proposed == STATE_CLOSING:
        return STATE_OPENING
    if current_stable == STATE_CLOSING and proposed == STATE_OPENING:
        return STATE_CLOSING
    # If current is CLOSED, 'CLOSING' is impossible -> coerce to OPENING
    if current_stable == STATE_CLOSED and proposed == STATE_CLOSING:
        return STATE_OPENING
    # If current is OPEN, 'OPENING' is impossible -> coerce to CLOSING
    if current_stable == STATE_OPEN and proposed == STATE_OPENING:
        return STATE_CLOSING
    return proposed


def _update_state_with_hysteresis(proposed):
    """Hysteresis: require dwell before committing to a new stable state.
    Also enforces the next-state rule for transitions out of OPEN/CLOSED.
    Uses state_lock to protect PROPOSED/STABLE updates, but releases before publishing.
    """
    if proposed in (INDETERMINATE, STATE_NOINPUT):
        return

    # Enforce next-state rule when a moving state is proposed
    with state_lock:
        current_stable = STABLE['state']
    proposed = _enforce_next_state_rule(current_stable, proposed)

    now = time.monotonic()
    if proposed in (STATE_OPEN, STATE_CLOSED):
        dwell = DWELL_SEC_STEADY
    elif proposed in (STATE_OPENING, STATE_CLOSING):
        dwell = DWELL_SEC_MOVING
    elif proposed == STATE_ERROR:
        dwell = DWELL_SEC_ERROR
    else:
        dwell = 0.0

    to_publish = None
    with state_lock:
        if PROPOSED['state'] != proposed:
            PROPOSED.update(state=proposed, since=now)
        else:
            # proposed unchanged; check dwell vs stable
            if STABLE['state'] != proposed and (now - PROPOSED['since'] >= dwell):
                STABLE.update(state=proposed, since=now)
                to_publish = proposed

    if to_publish is not None:
        publish_state_change(to_publish)

def _movement_watchdog_check():
    """If moving for too long, flag jam/error."""
    if MOVEMENT["active"]:
        if time.monotonic() - MOVEMENT["since"] > 30.0:
            MOVEMENT.update(active=False, target=None, since=0.0)
            _update_state_with_hysteresis(STATE_ERROR)

def gpio_cb(_gpio, _level, _tick):
    """pigpio callback for either sensor pin. Read both pins instantly and update state."""
    try:
        open_val  = pi.read(SENSOR_OPEN)
        close_val = pi.read(SENSOR_CLOSE)
        base = _map_led_to_state(open_val, close_val)

        # Also record LED colour transitions for flash analysis
        _append_led_event(led_colour(open_val, close_val))

        if base == STATE_ERROR:
            _update_state_with_hysteresis(STATE_ERROR)
            return

        if MOVEMENT["active"]:
            target_state = STATE_OPEN if MOVEMENT["target"] == STATE_OPEN else STATE_CLOSED
            moving_state = STATE_OPENING if target_state == STATE_OPEN else STATE_CLOSING
            elapsed = time.monotonic() - MOVEMENT["since"]

            MIN_MOVE_TIME = 4.0
            if base == target_state:
                if elapsed < MIN_MOVE_TIME:
                    _update_state_with_hysteresis(moving_state)
                else:
                    _update_state_with_hysteresis(target_state)
                    if STABLE["state"] == target_state:
                        MOVEMENT.update(active=False, target=None, since=0.0)
            elif base in (STATE_NOINPUT, STATE_ERROR):
                _update_state_with_hysteresis(moving_state)
                _movement_watchdog_check()
            else:
                _update_state_with_hysteresis(moving_state)
                _movement_watchdog_check()
        else:
            # No command in-flight. Use flash-pattern analysis to infer moving vs steady.
            inferred = _classify_flash_pattern()
            if inferred in (STATE_OPENING, STATE_CLOSING, STATE_ERROR, STATE_OPEN, STATE_CLOSED):
                _update_state_with_hysteresis(inferred)
            else:
                _update_state_with_hysteresis(base)

    except Exception as e:
        logging.error("GPIO callback error: %s", e)


# ======= GPIO Initialisation (pigpio) =======

def init_gpio():
    """Configure GPIO via pigpio and register callbacks with glitch filters."""
    global pi
    pi = pigpio.pi()
    if not pi.connected:
        raise RuntimeError("pigpio daemon not running. Start with: sudo pigpiod")

    # Inputs (active-low via optocouplers)
    pi.set_mode(SENSOR_OPEN, pigpio.INPUT)
    pi.set_mode(SENSOR_CLOSE, pigpio.INPUT)
    pi.set_pull_up_down(SENSOR_OPEN, pigpio.PUD_UP)
    pi.set_pull_up_down(SENSOR_CLOSE, pigpio.PUD_UP)

    # Outputs (relays) – start low
    pi.set_mode(OPEN_CMD_PIN, pigpio.OUTPUT)
    pi.set_mode(CLOSE_CMD_PIN, pigpio.OUTPUT)
    pi.write(OPEN_CMD_PIN, 0)
    pi.write(CLOSE_CMD_PIN, 0)

    # Reject pulses < 10 ms on both inputs
    pi.set_glitch_filter(SENSOR_OPEN, 10000)
    pi.set_glitch_filter(SENSOR_CLOSE, 10000)

    # Register callbacks for either edge on both inputs
    pi.callback(SENSOR_OPEN, pigpio.EITHER_EDGE, gpio_cb)
    pi.callback(SENSOR_CLOSE, pigpio.EITHER_EDGE, gpio_cb)

    logging.info("pigpio initialised; 10 ms glitch filters active on sensor pins.")

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
                    event_data = json.dumps({"doorState": state_val_to_text(door_state), "temperature": temperature})
                    push_event(event_data)
        time.sleep(60)

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
            logging.error("Temperature read error (is dtoverlay=w1-gpio in /boot/firmware/config.txt?): %s", e)
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
    The LED event buffer is maintained in the background and by interrupt on state changes
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
    return jsonify({"doorState": state_val_to_text(current_state), "temperature": temp})

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
    Initiates door opening and immediately sets door_state to OPENING
    """
    client_ip = request.remote_addr
    logging.info("Open door command received from %s", client_ip)
    with state_lock:
        MOVEMENT.update(active=True, target=STATE_OPEN, since=time.monotonic())
    _update_state_with_hysteresis(STATE_OPENING)
    pi.write(OPEN_CMD_PIN, 1)
    time.sleep(0.5)
    pi.write(OPEN_CMD_PIN, 0)
    logging.info("Door opening pulse sent.")
    return jsonify({"result": "OPENING"})

@app.route('/close', methods=['POST'])
@require_api_key
def close_door():
    """
    Initiates door closing and immediately sets door_state to CLOSING
    """
    client_ip = request.remote_addr
    logging.info("Close door command received from %s", client_ip)
    with state_lock:
        MOVEMENT.update(active=True, target=STATE_CLOSED, since=time.monotonic())
    _update_state_with_hysteresis(STATE_CLOSING)
    pi.write(CLOSE_CMD_PIN, 1)
    time.sleep(0.5)
    pi.write(CLOSE_CMD_PIN, 0)
    logging.info("Door closing pulse sent.")
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
            # Open fast, then keep-alive
            yield ":ok\n\n"
            last_beat = time.monotonic()
            while True:
                try:
                    data = q.get(timeout=15)
                    yield "data: " + data + "\n\n"
                except Exception:
                    if time.monotonic() - last_beat >= 15:
                        yield f": ping {int(time.time())}\n\n"
                        last_beat = time.monotonic()
        except GeneratorExit:
            subscribers.remove(q)
            logging.info("SSE subscriber from %s disconnected.", client_ip)
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
        "Content-Type": "text/event-stream; charset=utf-8",
    }
    return Response(event_stream(), headers=headers)

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


def init_api_key():
    global API_KEY
    if os.path.exists(API_KEY_FILE):
        with open(API_KEY_FILE, "r") as f:
            API_KEY = f.read().strip()
        logging.info("Loaded existing API key. Door control functions enabled.")
    else:
        API_KEY = None
        logging.info("API key not configured. Door control functions will be disabled until key has been set.")
        logging.info("To set the key, POST /set-key?api_key=[KEY]")


# ======= 2s Inactivity Heartbeat =======

def inactivity_heartbeat():
    """Every 2 seconds, *only if* no LED events were seen in that time, poll LEDs and
    run the usual classification/hysteresis. Any committed new state is published to SSE clients."""
    logging.info("Starting inactivity heartbeat thread (2s).")
    last_poll = 0.0
    while True:
        try:
            now = time.monotonic()
            last_evt = _last_event_time_monotonic()
            idle_for = now - last_evt if last_evt else float('inf')

            # Run at most once every HEARTBEAT_INTERVAL_SEC, and only if we've been idle that long
            if (now - last_poll) >= HEARTBEAT_INTERVAL_SEC and idle_for >= HEARTBEAT_INTERVAL_SEC:
                open_val  = pi.read(SENSOR_OPEN)
                close_val = pi.read(SENSOR_CLOSE)
                # Record this sampled LED state into history
                _append_led_event(led_colour(open_val, close_val))

                # Use same logic as idle GPIO path to determine state
                base = _map_led_to_state(open_val, close_val)
                inferred = _classify_flash_pattern()
                if inferred in (STATE_OPENING, STATE_CLOSING, STATE_ERROR, STATE_OPEN, STATE_CLOSED):
                    _update_state_with_hysteresis(inferred)
                else:
                    _update_state_with_hysteresis(base)

                last_poll = now

            # Sleep a bit to keep loop responsive but light; poll resolution ~0.25s
            time.sleep(0.25)
        except Exception as e:
            logging.error("Heartbeat error: %s", e)
            time.sleep(1.0)


# MAIN ENTRY POINT

def init_app():
    logging.info("Garage Door Web Server starting up.")

    logging.info("Initialising pigpio for Lo-tech Rollertec interface...")
    atexit.register(lambda: (pi and pi.stop()))
    init_gpio()

    # Seed initial door state (no edges yet at startup)
    try:
        open_val  = pi.read(SENSOR_OPEN)
        close_val = pi.read(SENSOR_CLOSE)
        base = _map_led_to_state(open_val, close_val)
        # Force hysteresis to accept immediately
        PROPOSED.update(state=base, since=time.monotonic() - 1.0)
        _update_state_with_hysteresis(base)
    except Exception as e:
        logging.warning("Initial state seed failed: %s", e)

    # Initialize the API key from file, if available
    init_api_key()

    # Open (or create) SSL certificate
    if not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)):
        logging.info("SSL certificate not found: generating new certificate...")
        generate_self_signed_cert(CERT_FILE, KEY_FILE)
    logging.info("Loading certificate...")
    ssl_ctx = (CERT_FILE, KEY_FILE)

    t_temp = threading.Thread(target=temperature_updater, daemon=True)
    t_temp.start()

    # Start inactivity heartbeat thread
    t_hb = threading.Thread(target=inactivity_heartbeat, daemon=True)
    t_hb.start()

    logging.info("Initialization complete.")
    return ssl_ctx


if __name__ == '__main__':
    ssl_context = init_app()
    logging.info("Starting Flask server on 0.0.0.0:8443 with SSL.")
    app.run(host='0.0.0.0', port=8443, ssl_context=ssl_context, threaded=True)
