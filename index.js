const https = require('https');
const EventSource = require('eventsource');  // Ensure you install this package via npm install eventsource

module.exports = (api) => {
  api.registerAccessory('homebridge-rollertec-garage-door-v2', GarageDoorWebAccessory);
};

class GarageDoorWebAccessory {
  constructor(log, config, api) {
    this.log = log;
    this.config = config;
    this.api = api;
    this.Service = this.api.hap.Service;
    this.Characteristic = this.api.hap.Characteristic;

    // Configuration for the remote web server
    this.name             = config.name               || 'Garage Door';
    this.doorSerialNumber = config.doorSerialNumber   || '(not set)';
    this.serverIP         = config.doorServer         || "127.0.0.1";  // e.g., "192.168.1.100"
    this.serverPort       = config.doorServerPort     || 8443;  // Use correct port for HTTPS
    // Use doorPSK from configuration for both configuration and all API calls.
    this.doorPSK          = config.doorPSK;
    this.apiKey           = null; // Will be set after initialization

    // Certificate pinning properties.
    this.expectedFingerprint = null;
    this.certificateError = false;

    // create an information service...
    this.informationService = new this.Service.AccessoryInformation()
      .setCharacteristic(this.Characteristic.Manufacturer, "Lo-tech")
      .setCharacteristic(this.Characteristic.Model, "PDT Rollertech V2")
      .setCharacteristic(this.Characteristic.SerialNumber, this.doorSerialNumber);

    // Create the Garage Door service
    this.doorService = new this.Service.GarageDoorOpener(this.name);
    this.doorService.getCharacteristic(this.Characteristic.CurrentDoorState)
      .on('get', this.handleCurrentDoorStateGet.bind(this));
    this.doorService.getCharacteristic(this.Characteristic.TargetDoorState)
      .on('get', this.handleTargetDoorStateGet.bind(this))
      .on('set', this.handleTargetDoorStateSet.bind(this));
    this.doorService.getCharacteristic(this.Characteristic.ObstructionDetected)
      .on('get', this.handleObstructionDetectedGet.bind(this));

    // Create a Temperature Sensor service
    this.temperatureService = new this.Service.TemperatureSensor(this.name + " Temperature");
    this.temperatureService.getCharacteristic(this.Characteristic.CurrentTemperature)
      .on('get', this.getTemperature.bind(this));

    // Internal state variables
    this.currentDoorState = this.Characteristic.CurrentDoorState.CLOSED;
    this.targetDoorState = this.Characteristic.TargetDoorState.CLOSED;

    // Begin initialization
    this.retryInitialization();
  }

  retryInitialization() {
    this.initializeConfiguration(() => {
      if (this.apiKey) {
        this.log("Initialization successful. Proceeding to connect SSE.");
        this.connectSSE();
      } else {
        this.log("Initialization failed (apiKey is null). Retrying in 5 seconds...");
        setTimeout(() => {
          this.retryInitialization();
        }, 5000);
      }
    });
  }

  // Certificate pinning callback.
  // If no expected fingerprint is set, store the current one.
  // If one is set and it doesn't match, mark an error.
  checkCert(host, cert) {
    // Use fingerprint256 if available
    const fingerprint = cert.fingerprint256 || cert.fingerprint;
    if (!this.expectedFingerprint) {
      this.expectedFingerprint = fingerprint;
      this.log(`Storing initial certificate fingerprint: ${fingerprint}`);
    } else if (this.expectedFingerprint !== fingerprint) {
      this.certificateError = true;
      const errorMsg = `Certificate fingerprint mismatch. Expected ${this.expectedFingerprint} but got ${fingerprint}. Potential man-in-the-middle attack detected.`;
      this.log(errorMsg);
      return new Error(errorMsg);
    }
    return undefined;
  }

  // Initialization: check if server is configured using doorPSK.
  initializeConfiguration(callback) {
    const options = {
      hostname: this.serverIP,
      port: this.serverPort,
      path: `/status?api_key=${this.doorPSK}`,
      method: 'GET',
      rejectUnauthorized: false,
      checkServerIdentity: this.checkCert.bind(this)
    };
    const req = https.request(options, (res) => {
      let responseData = '';
      res.on('data', chunk => { responseData += chunk; });
      res.on('end', () => {
        try {
          const result = JSON.parse(responseData);
          if (result.doorState === "unconfigured") {
            this.log("Server is unconfigured. Attempting to configure with doorPSK...");
            this.configureServer(callback);
          } else {
            this.log("Server already configured. Using doorPSK for API interactions.");
            this.apiKey = this.doorPSK;
            callback();
          }
        } catch (e) {
          this.log("Error parsing /status response during initialization: " + e);
          callback();
        }
      });
    });
    req.on('error', (e) => {
      this.log("HTTPS request error during initialization: " + e);
      callback();
    });
    req.end();
  }

  // Configure the server by sending a POST to /set-key with doorPSK.
  configureServer(callback) {
    const options = {
      hostname: this.serverIP,
      port: this.serverPort,
      path: `/set-key`,
      method: 'POST',
      rejectUnauthorized: false,
      checkServerIdentity: this.checkCert.bind(this),
      headers: {
        'Content-Type': 'application/json'
      }
    };
    const req = https.request(options, (res) => {
      let responseData = '';
      res.on('data', chunk => { responseData += chunk; });
      res.on('end', () => {
        try {
          const result = JSON.parse(responseData);
          if (result.result && result.result.includes("successfully")) {
            this.log("Server configured successfully with doorPSK.");
            this.apiKey = this.doorPSK;
          } else {
            this.log("Failed to configure server: " + responseData);
          }
        } catch (e) {
          this.log("Error parsing /set-key response: " + e);
        }
        callback();
      });
    });
    req.on('error', (e) => {
      this.log("HTTPS request error during configuration: " + e);
      callback();
    });
    const postData = JSON.stringify({ api_key: this.doorPSK });
    req.write(postData);
    req.end();
  }


  refreshStatus() {
    const options = {
      hostname: this.serverIP,
      port: this.serverPort,
      path: `/status?api_key=${this.apiKey}`,
      method: 'GET',
      rejectUnauthorized: false,
      checkServerIdentity: this.checkCert.bind(this)
    };

    const req = https.request(options, (res) => {
      let responseData = '';
      res.on('data', chunk => { responseData += chunk; });
      res.on('end', () => {
        try {
          const result = JSON.parse(responseData);
          this.log("Refreshed status: " + responseData);
          this.updateDoorState(result);
        } catch (e) {
          this.log("Error parsing /status response during refresh: " + e);
        }
      });
    });

    req.on('error', (e) => {
      this.log("Error fetching status during refresh: " + e);
    });
    req.end();
  }


  connectSSE() {
    // Create an HTTPS agent with our certificate pinning check.
    const httpsAgent = new https.Agent({
      rejectUnauthorized: false,
      checkServerIdentity: this.checkCert.bind(this)
    });
    // Build the SSE endpoint URL using the now configured API key.
    const sseUrl = `https://${this.serverIP}:${this.serverPort}/events?api_key=${this.apiKey}`;
    // Pass our custom agent to EventSource.
    const eventSourceInitDict = { agent: httpsAgent };
    this.eventSource = new EventSource(sseUrl, eventSourceInitDict);

    this.eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        this.log("SSE event received: " + event.data);
        this.updateDoorState(data);
      } catch (e) {
        this.log("Error parsing SSE event: " + e);
      }
    };

    this.eventSource.onerror = (err) => {
      this.log("SSE connection error: " + err);
      // Close the current connection to clean up.
      if (this.eventSource) {
        this.eventSource.close();
      }
      // Immediately refresh the door state by querying /status.
      this.refreshStatus();
      // Attempt to reconnect after a delay (e.g., 5 seconds).
      setTimeout(() => {
        this.log("Attempting to reconnect SSE...");
        this.connectSSE();
      }, 5000);
    };
  }

  updateDoorState(data) {
    // If the server sends an "unconfigured" state or certificate error has been flagged,
    // log and ignore the update.
    if (data.doorState === "unconfigured" || this.certificateError) {
      this.log("Ignoring event update due to unconfigured server or certificate error.");
      return;
    }
    // Map doorState string to HomeKit's numeric values:
    // 0 = OPEN, 1 = CLOSED, 2 = OPENING, 3 = CLOSING, 4 = STOPPED/unknown.
    let state;
    switch (data.doorState) {
      case 'OPEN':
        state = 0;
        this.targetDoorState = 0;
        break;
      case 'CLOSED':
        state = 1;
        this.targetDoorState = 1;
        break;
      case 'OPENING':
        state = 2;
        this.targetDoorState = 0;
        break;
      case 'CLOSING':
        state = 3;
        this.targetDoorState = 1;
        break;
      default:
        state = 4;
    }
    this.currentDoorState = state;
    // Update HomeKit characteristics
    this.doorService.updateCharacteristic(this.Characteristic.CurrentDoorState, state);
    this.doorService.updateCharacteristic(this.Characteristic.TargetDoorState, this.targetDoorState);
    this.temperatureService.updateCharacteristic(this.Characteristic.CurrentTemperature, data.temperature);
  }

  handleCurrentDoorStateGet(callback) {
    callback(null, this.currentDoorState);
  }

  handleTargetDoorStateGet(callback) {
    callback(null, this.targetDoorState);
  }

  handleTargetDoorStateSet(value, callback) {
    if (this.certificateError) {
      const err = new Error("Certificate error detected. Rejecting request due to potential MITM attack.");
      this.log(err.message);
      callback(err);
      return;
    }
    // Translate HomeKit target state (0 for OPEN, 1 for CLOSED) into the proper endpoint.
    const endpoint = (value === 0) ? '/open' : '/close';
    const options = {
      hostname: this.serverIP,
      port: this.serverPort,
      path: `${endpoint}?api_key=${this.apiKey}`,
      method: 'POST',
      rejectUnauthorized: false,
      checkServerIdentity: this.checkCert.bind(this)
    };

    const req = https.request(options, (res) => {
      let responseData = '';
      res.on('data', chunk => { responseData += chunk; });
      res.on('end', () => {
        this.log(`Command ${endpoint} response: ${responseData}`);
        callback(null);
      });
    });
    req.on('error', (e) => {
      this.log('HTTPS request error: ' + e);
      callback(e);
    });
    req.end();
  }

  handleObstructionDetectedGet(callback) {
    callback(null, false);
  }

  getTemperature(callback) {
    if (this.certificateError) {
      const err = new Error("Certificate error detected. Rejecting temperature query due to potential MITM attack.");
      this.log(err.message);
      callback(err);
      return;
    }
    // Query the /status endpoint using the configured API key.
    const options = {
      hostname: this.serverIP,
      port: this.serverPort,
      path: `/status?api_key=${this.apiKey}`,
      method: 'GET',
      rejectUnauthorized: false,
      checkServerIdentity: this.checkCert.bind(this)
    };

    const req = https.request(options, (res) => {
      let responseData = '';
      res.on('data', chunk => { responseData += chunk; });
      res.on('end', () => {
        try {
          const result = JSON.parse(responseData);
          callback(null, result.temperature);
        } catch (e) {
          callback(e);
        }
      });
    });
    req.on('error', (e) => {
      this.log('HTTPS request error: ' + e);
      callback(e);
    });
    req.end();
  }

  getServices() {
    return [
      this.informationService,
      this.doorService,
      this.temperatureService
    ];
  }
}
