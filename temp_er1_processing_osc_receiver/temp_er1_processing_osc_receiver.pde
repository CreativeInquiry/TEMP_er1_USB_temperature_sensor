// OSC receiver for TEMP_er1 USB temperature sensor data.
// Known to work with Processing (Java) 4.5.1.
// By Golan Levin, June 2026
//
// This sketch does not read the USB HID sensor directly. 
// Start the Python reader first, from the repository root:
//     python3 temp_er1_python/temper_cli.py --osc --quiet
//
// By default the Python reader sends OSC to 127.0.0.1:8367 with the
// /temp_er1 prefix, matching OSC_PORT and OSC_PREFIX below.

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.SocketException;
import java.util.ArrayList;

/**
 * OSC UDP port used by the Python TEMP_er1 reader.
 * 8367 is mnemonic for TEMP on a US phone keypad.
 */
final int OSC_PORT = 8367;

/** OSC address prefix emitted by temper_cli.py. */
final String OSC_PREFIX = "/temp_er1";

/** Maximum number of temperature samples kept in the in-memory history ring. */
final int HISTORY_CAPACITY = 360;

/** Width of the scrolling temperature chart, in seconds. */
final int HISTORY_WINDOW_SECONDS = 90;

/** Seconds between bottom-axis tick marks. */
final int HISTORY_TICK_SECONDS = 10;

/** Background turns pink after this many seconds without temperature OSC. */
final int OSC_TIMEOUT_SECONDS = 10;

/** Lower bound of the chart's Celsius scale. */
final float GRAPH_MIN_C = 0;

/** Upper bound of the chart's Celsius scale. */
final float GRAPH_MAX_C = 40;

DatagramSocket oscSocket;
Thread oscThread;
volatile boolean oscReceiverRunning = false;
Object oscLock = new Object();

float temperatureC = Float.NaN;
float temperatureF = Float.NaN;
String status = "Listening for OSC on port " + OSC_PORT;
String deviceInfo = "HID device: waiting for OSC metadata...";
String oscInfo = "OSC: UDP port " + OSC_PORT + ", prefix " + OSC_PREFIX + ", run `python3 temp_er1_python/temper_cli.py --osc --quiet`";
String devicePath = "";
long lastReadingMillis = 0;

boolean pendingReading = false;
float pendingTemperatureC = Float.NaN;
float pendingTemperatureF = Float.NaN;
String pendingStatus = null;
String pendingDeviceInfo = null;
String pendingDevicePath = null;

float[] temperatureHistory = new float[HISTORY_CAPACITY];
long[] historyMillis = new long[HISTORY_CAPACITY];
int historyCount = 0;
int historyStart = 0;

/**
 * Initializes the Processing window and starts the background OSC receiver.
 */
void setup() {
  size(640, 480);
  surface.setTitle("TEMP_er1");
  textFont(createFont("Arial", 18));
  startOscReceiver();
}

/**
 * Main render loop. Pulls any OSC data handed off by the receiver thread,
 * then redraws the current reading, metadata, and history chart.
 */
void draw() {
  consumePendingOscMessages();

  background(oscConnectionIsFresh() ? color(248) : color(255, 210, 224));
  fill(24);
  noStroke();

  textSize(18);
  text("TEMP_er1 USB temperature sensor", 32, 42);
  text(displayStatus(), 330, 42);

  textSize(13);
  fill(86);
  text(displayDeviceInfo(), 32, 66);
  text(oscInfo, 32, 86);

  textSize(72);
  fill(24);
  if (Float.isNaN(temperatureC)) {
    text("--.-° C", 32, 188);
  } else {
    text(nf(temperatureC, 0, 2) + "° C", 32, 188);
  }

  textSize(28);
  fill(56);
  if (Float.isNaN(temperatureF)) {
    text("--.-° F", 36, 230);
  } else {
    text(nf(temperatureF, 0, 2) + "° F", 36, 230);
  }

  drawTemperatureHistory();

  fill(110);
  textSize(14);
  String age = lastReadingMillis == 0
    ? "No OSC temperature received yet"
    : "Last reading " + nf((millis() - lastReadingMillis) / 1000.0, 0, 1) + " seconds ago";
  text(age, 32, height - 14);
}

/**
 * Returns true when a temperature OSC packet has arrived recently.
 */
boolean oscConnectionIsFresh() {
  return lastReadingMillis > 0 && millis() - lastReadingMillis <= OSC_TIMEOUT_SECONDS * 1000L;
}

/**
 * Returns the status text shown in the header.
 */
String displayStatus() {
  if (lastReadingMillis == 0) {
    return status;
  }
  if (!oscConnectionIsFresh()) {
    return ": OSC timed out";
  }
  return status;
}

/**
 * Opens the UDP socket and starts a background thread that blocks on incoming
 * OSC datagrams. Processing drawing must stay on the main animation thread.
 */
void startOscReceiver() {
  try {
    oscSocket = new DatagramSocket(OSC_PORT);
    oscReceiverRunning = true;
    oscThread = new Thread(new Runnable() {
      public void run() {
        receiveOscLoop();
      }
    });
    oscThread.start();
  } catch (SocketException e) {
    status = "Could not listen for OSC on port " + OSC_PORT + ": " + e.getMessage();
  }
}

/**
 * Background receive loop. It parses each UDP datagram as a single OSC message
 * and hands useful values to the main thread through the pending fields.
 */
void receiveOscLoop() {
  byte[] buffer = new byte[4096];
  while (oscReceiverRunning) {
    try {
      DatagramPacket packet = new DatagramPacket(buffer, buffer.length);
      oscSocket.receive(packet);
      OscMessage message = parseOscMessage(packet.getData(), packet.getLength());
      if (message != null) {
        handleOscMessage(message);
      }
    } catch (SocketException e) {
      if (oscReceiverRunning) {
        setPendingStatus("OSC socket error: " + e.getMessage());
      }
    } catch (Exception e) {
      setPendingStatus("OSC read error: " + e.getMessage());
    }
  }
}

/**
 * Routes OSC messages from the Python reader into pending UI updates.
 *
 * Supported addresses:
 * /temp_er1/temperature             celsius fahrenheit
 * /temp_er1/temperature/celsius     celsius
 * /temp_er1/device                  device label
 * /temp_er1/device/path             HID path
 */
void handleOscMessage(OscMessage message) {
  String address = message.address;

  if (address.equals(OSC_PREFIX + "/temperature") && message.arguments.size() >= 2) {
    Float c = oscFloat(message.arguments.get(0));
    Float f = oscFloat(message.arguments.get(1));
    if (c != null && f != null) {
      setPendingReading(c, f);
    }
    return;
  }

  if (address.equals(OSC_PREFIX + "/temperature/celsius") && message.arguments.size() >= 1) {
    Float c = oscFloat(message.arguments.get(0));
    if (c != null) {
      setPendingReading(c, celsiusToFahrenheit(c));
    }
    return;
  }

  if (address.equals(OSC_PREFIX + "/device") && message.arguments.size() >= 1) {
    String value = oscString(message.arguments.get(0));
    if (value != null) {
      synchronized (oscLock) {
        pendingDeviceInfo = "HID device: " + value;
      }
    }
    return;
  }

  if (address.equals(OSC_PREFIX + "/device/path") && message.arguments.size() >= 1) {
    String value = oscString(message.arguments.get(0));
    if (value != null) {
      synchronized (oscLock) {
        pendingDevicePath = value;
      }
    }
  }
}

/**
 * Stores a new temperature reading for the animation thread to consume.
 * This method can be called by the OSC receiver thread.
 */
void setPendingReading(float celsius, float fahrenheit) {
  synchronized (oscLock) {
    pendingTemperatureC = celsius;
    pendingTemperatureF = fahrenheit;
    pendingReading = true;
    pendingStatus = ": Sensor connected";
  }
}

/**
 * Stores a status message for the animation thread to consume.
 * This avoids writing UI state directly from the background thread.
 */
void setPendingStatus(String value) {
  synchronized (oscLock) {
    pendingStatus = value;
  }
}

/**
 * Transfers pending OSC data into normal sketch state.
 * This is called once per frame from draw(), so graph/history mutation happens
 * on the Processing animation thread.
 */
void consumePendingOscMessages() {
  boolean shouldAddHistory = false;
  float historyTemperature = Float.NaN;

  synchronized (oscLock) {
    // Copy and clear pending values while holding the lock briefly.
    if (pendingDeviceInfo != null) {
      deviceInfo = pendingDeviceInfo;
      pendingDeviceInfo = null;
    }
    if (pendingDevicePath != null) {
      devicePath = pendingDevicePath;
      pendingDevicePath = null;
    }
    if (pendingStatus != null) {
      status = pendingStatus;
      pendingStatus = null;
    }
    if (pendingReading) {
      temperatureC = pendingTemperatureC;
      temperatureF = pendingTemperatureF;
      lastReadingMillis = millis();
      historyTemperature = temperatureC;
      shouldAddHistory = true;
      pendingReading = false;
    }
  }

  if (shouldAddHistory) {
    addTemperatureToHistory(historyTemperature);
  }
}

/**
 * Returns the device metadata line shown in the UI.
 */
String displayDeviceInfo() {
  if (devicePath == null || devicePath.length() == 0) {
    return deviceInfo;
  }
  return deviceInfo + " path=" + devicePath;
}

/**
 * Converts an OSC argument object to a Float when the argument type is numeric.
 */
Float oscFloat(Object value) {
  if (value instanceof Float) {
    return (Float)value;
  }
  if (value instanceof Integer) {
    return ((Integer)value).floatValue();
  }
  return null;
}

/**
 * Converts an OSC argument object to a String when the argument type matches.
 */
String oscString(Object value) {
  if (value instanceof String) {
    return (String)value;
  }
  return null;
}

/**
 * Parses the subset of OSC needed by this sketch.
 *
 * This parser intentionally supports only standalone OSC messages with float,
 * int, and string arguments. It does not implement OSC bundles because the
 * Python sender emits individual messages.
 */
OscMessage parseOscMessage(byte[] data, int length) {
  OscString address = readOscString(data, 0, length);
  if (address == null || !address.value.startsWith("/")) {
    return null;
  }

  OscString typeTags = readOscString(data, address.nextOffset, length);
  if (typeTags == null || !typeTags.value.startsWith(",")) {
    return null;
  }

  OscMessage message = new OscMessage();
  message.address = address.value;
  int offset = typeTags.nextOffset;

  // OSC type tags start with a comma; each later character describes one arg.
  for (int i = 1; i < typeTags.value.length(); i++) {
    char type = typeTags.value.charAt(i);
    if (type == 'f') {
      if (offset + 4 > length) {
        return null;
      }
      message.arguments.add(Float.intBitsToFloat(readOscInt(data, offset)));
      offset += 4;
    } else if (type == 'i') {
      if (offset + 4 > length) {
        return null;
      }
      message.arguments.add(readOscInt(data, offset));
      offset += 4;
    } else if (type == 's') {
      OscString value = readOscString(data, offset, length);
      if (value == null) {
        return null;
      }
      message.arguments.add(value.value);
      offset = value.nextOffset;
    }
  }

  return message;
}

/**
 * Reads a big-endian 32-bit integer from an OSC datagram.
 */
int readOscInt(byte[] data, int offset) {
  return ((data[offset] & 0xff) << 24)
    | ((data[offset + 1] & 0xff) << 16)
    | ((data[offset + 2] & 0xff) << 8)
    | (data[offset + 3] & 0xff);
}

/**
 * Reads a null-terminated OSC string and advances to the next 4-byte boundary.
 */
OscString readOscString(byte[] data, int offset, int length) {
  int end = offset;
  while (end < length && data[end] != 0) {
    end++;
  }
  if (end >= length) {
    return null;
  }

  OscString result = new OscString();
  result.value = new String(data, offset, end - offset);
  result.nextOffset = alignOscOffset(end + 1);
  if (result.nextOffset > length) {
    return null;
  }
  return result;
}

/**
 * Returns the next OSC 4-byte aligned offset.
 */
int alignOscOffset(int offset) {
  return (offset + 3) & ~3;
}

/**
 * Converts Celsius to Fahrenheit.
 */
float celsiusToFahrenheit(float celsius) {
  return (celsius * 9.0 / 5.0) + 32.0;
}

/**
 * Adds a temperature sample to the history ring buffer.
 */
void addTemperatureToHistory(float temperature) {
  int insertIndex = (historyStart + historyCount) % HISTORY_CAPACITY;
  temperatureHistory[insertIndex] = temperature;
  historyMillis[insertIndex] = millis();
  if (historyCount < HISTORY_CAPACITY) {
    historyCount++;
  } else {
    historyStart = (historyStart + 1) % HISTORY_CAPACITY;
  }
}

/**
 * Returns a history sample by visible age order, oldest to newest.
 */
float historyValueAt(int visibleIndex) {
  return temperatureHistory[(historyStart + visibleIndex) % HISTORY_CAPACITY];
}

/**
 * Returns a history sample timestamp by visible age order, oldest to newest.
 */
long historyMillisAt(int visibleIndex) {
  return historyMillis[(historyStart + visibleIndex) % HISTORY_CAPACITY];
}

/**
 * Maps a Celsius temperature into the chart's y coordinate.
 */
float graphY(float temperature) {
  return map(constrain(temperature, GRAPH_MIN_C, GRAPH_MAX_C), GRAPH_MIN_C, GRAPH_MAX_C, 432, 252);
}

/**
 * Draws the fixed-scale, time-based temperature history chart.
 */
void drawTemperatureHistory() {
  int graphLeft = 36;
  int graphRight = width - 72;
  int graphTop = 252;
  int graphBottom = 432;
  int scaleX = graphRight;
  long now = millis();
  long windowMillis = HISTORY_WINDOW_SECONDS * 1000L;

  stroke(218);
  strokeWeight(1);
  line(graphLeft, graphBottom, graphRight, graphBottom);
  line(scaleX, graphTop, scaleX, graphBottom);

  textSize(12);
  fill(80);
  noStroke();
  textAlign(LEFT, CENTER);
  int clo = (int)GRAPH_MIN_C;
  int chi = (int)GRAPH_MAX_C;
  for (int tick = clo; tick <= chi; tick += 10) {
    float y = graphY(tick);
    stroke(226);
    line(graphLeft, y, graphRight, y);
    stroke(24);
    line(scaleX - 6, y, scaleX, y);
    noStroke();
    fill(70);
    text(tick + "° C", scaleX + 7, y);
  }

  textSize(9);
  textAlign(CENTER, TOP);
  for (int secondsAgo = 0; secondsAgo <= HISTORY_WINDOW_SECONDS; secondsAgo += HISTORY_TICK_SECONDS) {
    float x = map(secondsAgo, HISTORY_WINDOW_SECONDS, 0, graphLeft, graphRight);
    stroke(24);
    line(x, graphBottom, x, graphBottom + 5);
    noStroke();
    fill(70);
    text(secondsAgo == 0 ? "now" : str(secondsAgo), x, graphBottom + 9);
  }

  if (historyCount > 1) {
    noFill();
    stroke(0);
    strokeWeight(2);
    beginShape();
    for (int i = 0; i < historyCount; i++) {
      long readingMillis = historyMillisAt(i);
      long ageMillis = now - readingMillis;
      if (ageMillis < 0 || ageMillis > windowMillis) {
        continue;
      }
      // The newest reading sits at the right edge; older readings drift left.
      float x = map(ageMillis, windowMillis, 0, graphLeft, graphRight);
      vertex(x, graphY(historyValueAt(i)));
    }
    endShape();
  }

  textAlign(LEFT, BASELINE);
  noStroke();
}

/**
 * Stops the background OSC receiver when the sketch exits.
 */
void stop() {
  oscReceiverRunning = false;
  if (oscSocket != null) {
    oscSocket.close();
  }
  super.stop();
}

/**
 * Minimal representation of one parsed OSC message.
 */
class OscMessage {
  String address = "";
  ArrayList<Object> arguments = new ArrayList<Object>();
}

/**
 * OSC string parse result: decoded value plus the next aligned read offset.
 */
class OscString {
  String value = "";
  int nextOffset = 0;
}
