[![Donate](https://badgen.net/badge/donate/paypal)](https://paypal.me/HomebridgeJ1mbo)

# homebridge-rollertec-garage-door-v2

A HomeBridge interface for PDT Rollertec controlled garage doors, capable of monitoring and controlling the door. Requires Lo-tech PDT Rollertec interface kit, or equivalent electrical connection. See www.lo-tech.co.uk/rollertec.

NOTE: v2 works differently to v1 and is not a drop-in replacement. v1 should be removed before deploying v2.

This plugin enables the use of "Hey Siri, Open my Garage Door" and similar voice commands.

Key features:

- Can open and close the door.
- Monitors the current door state via the Rollertec status LED, reporting open, closed, opening, closing, or jammed status through HomeKit.

When used with the Lo-tech interface, the PDT Rollertec controller can continue to be operated as normal with both push-buttons and RF remote controls if required.

Subscribable events:

- Door state

This plugin works with two components:

1. The plugin itself, index.js, which is run on the HomeBridge server.
2. A Python web server, garagedoor_web_server.py, which provides control of the door and is deployed on a Raspberry Pi physically connected to the garage door. Please see below.

The plugin can be adapted to other door openers by modifying the web server as needed (ChatGPT is your friend).

# V2

This version (v2) changes the solution architecture by the introduction of the web server component. It is therefore a breaking change and not offered as an in-place upgrade to v1, which remains available for installations having Homebridge running on the Raspberry Pi physically connected to the garage door.

The web server must be deployed on the Raspberry Pi manually, and configured to run as a service. The web server file is included here to assist this. Please see README.md and INSTALLATION. This architecture brings several benefits:

1. Homebridge no longer needs to be run on the Raspberry Pi connected to the door. If you have a central Homebridge server, this means there is less to maintain since the web server is unlikely to need updating much.
2. A single instance of the plugin can control multiple doors regardless of where they are.
3. Updating Homebridge on Raspberry Pi, and the underlying OS, can be difficult and using this approach the Pi needs to only run the web server providing a reduced attack surface. Homebridge itself can be run on (for example) Ubuntu elsewhere, which can be easier to maintain.
4. Easier to extend funcionality to other doors, since the plugin only needs the simple API provided by the web server.
5. Reduced resource requirements means the web server can be deployed on any Pi board that is to hand.


# SECURITY

This plugin uses a preshared key to authenticate with the web sevrer and a self-signed SSL certificate to protect the key in transit. The certificate is generated by the web server automatically when started, if not already in place.

The key is configured through the Homebridge plugin (along with the web server IP address) and is sent to the web server on first communication if a key has not already been set.

Note that:

1. Anyone with access to this key and network access to the web server can potentially open the door.
2. Applying firewall rules to the Pi to restrict access to the Homebridge server would be preferable.

If you need to reset the preshared key for any reason:

1. Delete /opt/garagedoor_web_server/api_key.txt on the web server and restart the service
2. Update the key to whatever you want through the pluging settings
3. Restart Homebridge (or the child bridge for the plugin).


# KNOWN ISSUES

1. Nearby 433MHz sources may cause erroneous notifications if recognised by the Rollertec CPU and trigger certain LED combinations.


# IMPORTANT INFORMATION

- MOVING DOORS CAN CAUSE SERIOUS INJURY. NEVER OPERATE THE DOOR UNLESS YOU CAN SEE IT.
- Not all doors are fitted with edge impact detectors, and even where fitted these should not be relied upon.
- Not all LED flash codes are correctly interpretted. This might lead to false alerts, for example if the door controller is receiving radio interference.
- This software has not been exhaustively tested and there could be bugs in the web server code (or Python itself) that enable an attacker to open the door without authentication.
- USE OF THIS SOFTWARE IS ENTIRELY AT YOUR OWN RISK.


# TERMS OF USE

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.

THE INTENDED USE OF THE SOFTWARE IS TO ENABLE THE END-USER TO BUILD THEIR OWN AUTOMATION SYSTEM AROUND THE PDT ROLLERTEC DOOR INTERFACE. THE SOFTWARE HAS NOT BEEN TESTED IN ALL POSSIBLE SCENARIOS AND IS NOT A FINISHED PRODUCT IN ITSELF. THE END USER IS RESPONSIBLE FOR TESTING THE COMPLETE SYSTEM AND ALL LIABILITY ARISING FROM ITS USE. BY USING THIS SOFTWARE, YOU ARE ACCEPTING THESE TERMS OF USE.

Copyright (c) 2020,2025 James Pearce.

# Plugin Configuration

- The plugin can be installed through HomeBridge plugins UI, and the settings are fully configurable in the UI.
- The webserver component must be installed manually on whatever will be directly connected to the door controller itself - usually a Raspberry Pi. See the INSTALLATION file for instructions.

# Issues and Contact

Please raise an issue should you come across one.
