void setup() {

  Serial.begin(115200);

  delay(500);

  Serial.println("\n====================================");
  Serial.println(" ESP32 CYBERTWIN - MAZDA BT-50");
  Serial.println("====================================");

  bootId = esp_random();

  // ------------------------------------------------
  // WIFI
  // ------------------------------------------------

  WiFi.mode(WIFI_STA);

  WiFi.setAutoReconnect(false);

  maintainWiFi();

  // ------------------------------------------------
  // MQTT
  // ------------------------------------------------

  mqttClient.setServer(MQTT_SERVER, MQTT_PORT);

  mqttClient.setBufferSize(1536);

  mqttClient.setKeepAlive(30);

  mqttClient.setSocketTimeout(3);

  // ------------------------------------------------
  // LITTLEFS
  // ------------------------------------------------

  fsReady = LittleFS.begin(true);

  if (fsReady) {

    Serial.println("[FS] LittleFS listo.");

  } else {

    Serial.println(
      "[FS] ERROR: LittleFS no disponible. No habrá cola persistente."
    );
  }

  // ------------------------------------------------
  // ZONA HORARIA COLOMBIA
  // ------------------------------------------------

  setenv("TZ", "COT5", 1);

  tzset();

  // ------------------------------------------------
  // BLUETOOTH
  // ------------------------------------------------

  SerialBT.begin("ESP32_CyberTwin_OBD", true);

  if (strlen(ELM_BT_PIN) > 0) {
    SerialBT.setPin(ELM_BT_PIN);
  }

  Serial.println(
    "[BT] Bluetooth Classic iniciado en modo maestro."
  );

  printBluetoothConfiguration();

  // ------------------------------------------------
  // FINALIZACIÓN
  // ------------------------------------------------

  Serial.println("[SISTEMA] Inicio completado.");

  Serial.println("[SISTEMA] Iniciando adquisición.");
}
