<<<<<<< HEAD
\# Latte Art Machine – Technischer Projektplan



\## Projektübersicht



Ziel ist die Entwicklung einer automatisierten Latte Art Machine, die auf einem Raspberry Pi basiert. Das System konvertiert Bilddateien (PNG oder SVG) in Druckpfade, steuert einen Marlin-basierten Druckkopf per UART, regelt die Milchaufschäumung über Relays und stellt eine mobile-optimierte Web-Oberfläche zur Bedienung bereit. Der gesamte Betrieb soll kabellos über einen eigenen WLAN-Hotspot des Pi erfolgen.



\*\*\*



\## Zieldefinition



Das fertige System soll folgendes leisten:



\- Ein Nutzer lädt über das Browser-UI eine Bilddatei (PNG oder SVG) hoch

\- Das System konvertiert das Bild automatisch in G-Code mit korrekten Fahrbefehlen und Pumpensteuerung

\- Vor dem Druck wird die Milchoberfläche per ToF-Sensor vermessen und der Z-Offset kalibriert

\- Der Druckkopf fährt das Motiv in die aufgeschäumte Milch und gibt Kaffeemilch präzise dosiert ab

\- Gleichzeitig wird die Milch nach einem konfigurierbaren Aufschäum-Rezept aufbereitet

\- Das UI zeigt Echtzeit-Feedback zu Druckfortschritt, Sensorwerten und Warnungen

\- Alle Einstellungen können über einen Advanced Tab im Browser geändert und gespeichert werden



\*\*\*



\## Systemarchitektur



Das Herzstück ist ein \*\*Python-Flask-Prozess\*\* auf dem Raspberry Pi, der alle Subsysteme koordiniert. Der Browser kommuniziert per HTTP-REST und WebSocket (SocketIO) mit diesem Backend. Hardwarezugriffe (UART, I2C, GPIO) laufen in separaten Python-Threads, die über eine gemeinsame Event-Queue mit dem Webserver kommunizieren.



```

Browser (Mobile)

&#x20;    │  HTTP + WebSocket

&#x20;    ▼

Flask + SocketIO (app.py)

&#x20;    │

&#x20;    ├── PySerial → /dev/ttyAMA0 → Marlin Board (UART)

&#x20;    ├── I2C      → VL53L0X ToF-Sensor

&#x20;    ├── GPIO     → Relays (Pumpe, Heizer, Ventile, Vakuumpumpe)

&#x20;    ├── GPIO PWM → Servo (Tank)

&#x20;    ├── GPIO PWM → WS2812B LED (Kopf + Tank)

&#x20;    └── Filesystem → /recipes/\*.json (Settings)

```



\*\*\*



\## Modul 1 – Web-Backend (Flask)



\*\*Sprache:\*\* Python 3.11+  

\*\*Pakete:\*\* `flask`, `flask-socketio`, `eventlet`



Der Flask-Server läuft auf Port 5000 und bedient alle API-Endpunkte sowie die statischen Frontend-Dateien. `eventlet.monkey\_patch()` wird als allererstes im Hauptskript aufgerufen – das ist zwingend erforderlich, damit SocketIO-Emits aus GPIO-Callbacks heraus zuverlässig funktionieren.



\*\*Wichtige API-Endpunkte:\*\*



| Endpunkt | Methode | Funktion |

|---|---|---|

| `/api/upload` | POST | SVG/PNG hochladen |

| `/api/start-print` | POST | Druckjob starten |

| `/api/abort` | POST | Druck sofort abbrechen |

| `/api/recipes` | GET | Alle Aufschäum-Rezepte laden |

| `/api/recipes/<name>` | PUT | Rezept speichern |

| `/api/tank` | POST | Servo manuell steuern |

| `/api/tof` | GET | Aktuelle ToF-Messung abrufen |



\*\*WebSocket-Events (Server → Browser):\*\*



| Event | Payload | Bedeutung |

|---|---|---|

| `print\_progress` | `{percent, line, total}` | Fortschritt in % |

| `marlin\_status` | `{msg}` | Antworten von Marlin |

| `relay\_update` | `{pump, heater, ...}` | Relay-Zustand |

| `warning` | `{msg, level}` | Warnungen und Fehler |

| `tof\_reading` | `{distance\_mm}` | Sensor-Messwert |



\*\*\*



\## Modul 2 – Marlin UART-Kommunikation



\*\*Bibliothek:\*\* `pyserial`  

\*\*Schnittstelle:\*\* `/dev/ttyAMA0` (Pi Hardware-UART) oder `/dev/ttyUSB0` (USB-Adapter)  

\*\*Baudrate:\*\* 115200



Marlin verarbeitet G-Code-Commands seriell und bestätigt jeden Befehl mit `ok`. Erst nach diesem `ok` darf der nächste Command gesendet werden. Die Implementierung verwendet eine Thread-basierte Command-Queue: Das Frontend oder der G-Code-Generator legt Commands in die Queue, ein dedizierter Worker-Thread sendet sie sequenziell und wartet jeweils auf `ok`.



\*\*Ablauf bei jedem Druck:\*\*

1\. `G28` – Homing aller Achsen

2\. ToF-Messung → `G92 Z{messwert}` – Z-Offset setzen

3\. G-Code-Zeilen aus Datei senden (mit Fortschritts-Tracking)

4\. `M42 P{pin} S255` / `M42 P{pin} S0` – Pumpe ein/aus schalten



\*\*Testen ohne Hardware:\*\* Virtuellen Serial-Port per `socat` erstellen und den Marlin-Simulator (MarlinSimUI) anschließen. Alle Commands und Antworten können so am PC vollständig simuliert werden.



\*\*\*



\## Modul 3 – SVG → G-Code Konvertierung



\*\*Bibliothek:\*\* `svgpathtools`  

\*\*Algorithmus:\*\* Horizontale Schraffur (Hatch-Fill)



SVG-Pfade werden eingelesen und durch horizontale Scan-Linien in festem Abstand (konfigurierbar, z.B. 1,0 mm) geschnitten. Die Schnittpunkte definieren Start- und Endpunkte jeder Fahrlinie. Zwischen diesen Punkten ist die Pumpe aktiv, außerhalb fährt der Kopf ohne Ausgabe.



\*\*Konvertierungsschritte:\*\*

1\. SVG laden und Pfade extrahieren

2\. Bounding Box bestimmen, Koordinatensystem auf Druckbereich skalieren

3\. Scan-Linien berechnen, Schnittpunkte mit Pfad ermitteln

4\. G-Code erzeugen: `G0` (Eilgang ohne Pumpe), `G1` (Fahren mit Pumpe), `M42` (Pumpe ein/aus)

5\. G-Code-Datei im `/uploads/`-Ordner speichern



\*\*PNG-Eingabe:\*\* PNG-Dateien werden serverseitig mit `potrace` (CLI-Tool) in SVG konvertiert, bevor der obige Algorithmus greift. Der Schritt ist automatisch – der Nutzer lädt einfach PNG oder SVG hoch.



\*\*Testen:\*\* Generierter G-Code kann direkt in \[ncviewer.com](https://ncviewer.com) eingefügt werden, um die Fahrwege visuell zu prüfen – ohne Drucker.



\*\*\*



\## Modul 4 – Aufschäum-Sequenzer



\*\*Bibliothek:\*\* `RPi.GPIO`  

\*\*Settings-Format:\*\* JSON-Dateien in `/recipes/`



Das Aufschäum-Verhalten wird als zeitbasierte Sequenz von Relay-Zuständen definiert. Jede Milchsorte kann ein eigenes Rezept haben. Die Rezepte sind einfache JSON-Dateien, die über den Advanced Tab im Browser geladen, bearbeitet und gespeichert werden können.



\*\*Beispiel-Rezept (`oatmilk.json`):\*\*

```json

{

&#x20; "name": "Hafermilch Standard",

&#x20; "duration\_ms": 12000,

&#x20; "steps": \[

&#x20;   {"time\_ms": 0,    "pump": true,  "heater": true,  "flow\_stop": false, "vacuum": false},

&#x20;   {"time\_ms": 3000, "pump": true,  "heater": false, "flow\_stop": true,  "vacuum": true},

&#x20;   {"time\_ms": 8000, "pump": false, "heater": false, "flow\_stop": false, "vacuum": true},

&#x20;   {"time\_ms": 11000,"pump": false, "heater": false, "flow\_stop": false, "vacuum": false}

&#x20; ]

}

```



Der Sequenzer läuft in einem eigenen Thread und emittiert nach jedem Schritt ein `relay\_update`-Event ans UI. Start/Stop kann jederzeit per API ausgelöst werden. Neue Rezepte werden ohne Neustart des Servers aktiv.



\*\*Testen:\*\* `gpiozero` mit `MockFactory` ermöglicht vollständige Simulation auf dem PC. Pin-Zustände werden per `print()` geloggt.



\*\*\*



\## Modul 5 – Druckkopf: ToF-Sensor \& Status-LED



\*\*ToF-Sensor:\*\* VL53L0X / VL53L1X per I2C (`/dev/i2c-1`)  

\*\*Bibliothek:\*\* `adafruit-circuitpython-vl53l0x`  

\*\*LED:\*\* WS2812B (NeoPixel) per GPIO 18  

\*\*Bibliothek:\*\* `rpi\_ws281x`



Der ToF-Sensor misst vor jedem Druck den Abstand zur Milchoberfläche. Der gemessene Wert wird als Z-Offset an Marlin übergeben (`G92 Z{wert}`), sodass der Druckkopf unabhängig von der Füllmenge immer im richtigen Abstand zur Oberfläche arbeitet.



Die LED im Druckkopf zeigt den aktuellen Status farblich an:



| Zustand | Farbe | Muster |

|---|---|---|

| Idle | Aus | — |

| Homing | Orange | Statisch |

| Drucken | Grün | Statisch |

| Fehler | Rot | Statisch |

| Warten | Weiß | Pulsierend |



\*\*Testen:\*\* `i2cdetect -y 1` zeigt erkannte I2C-Geräte im Terminal. VL53L0X erscheint auf Adresse `0x29`.



\*\*\*



\## Modul 6 – Tank: Servo, Licht \& Button



\*\*Button:\*\* GPIO-Eingang mit internem Pull-Up  

\*\*Servo:\*\* GPIO PWM (50 Hz)  

\*\*Tank-LED:\*\* GPIO PWM oder WS2812B



Der Tank-Button fungiert als Sicherheitssensor. Ist er gedrückt (Tank eingesetzt), dreht der Servo auf Position „offen" und die Tank-LED pulsiert. Ist er nicht gedrückt (kein Tank), dreht der Servo auf „geschlossen", die LED ist aus und das UI zeigt eine Warnung.



Der Button wird per GPIO-Interrupt ausgewertet (kein Polling), damit der Hauptprozess nicht belastet wird. Bei jedem Zustandswechsel wird ein SocketIO-Event ans UI gesendet.



\*\*\*



\## Modul 7 – Frontend (UI)



Die bestehende mobile-first Website wird um folgende Bereiche erweitert:



\*\*Haupt-Tab:\*\*

\- Datei-Upload (PNG / SVG) mit Vorschau

\- Start-Button (erst aktiv nach erfolgreichem Upload und Tank-Check)

\- Fortschrittsanzeige: Prozent-Balken + geschätzte Restzeit

\- Live-Statusanzeige: aktueller Druckkopf-Zustand, Relay-Zustände

\- Warnungsbereich (Tank fehlt, Marlin-Fehler, Sensor-Fehler)



\*\*Advanced Tab:\*\*

\- Rezept-Auswahl (Dropdown aus vorhandenen JSON-Dateien)

\- Inline-Editor für Sequenz-Schritte (Zeit + Relay-Toggles)

\- Speichern-Button → `PUT /api/recipes/<name>`

\- Neues Rezept anlegen, vorhandenes löschen



\*\*\*



\## Modul 8 – WLAN-Hotspot



\*\*Tools:\*\* `hostapd`, `dnsmasq`  

\*\*Pi-IP im Hotspot:\*\* `192.168.4.1`



Der Pi spannt einen eigenen WLAN-Access-Point auf. Verbundene Geräte rufen `http://192.168.4.1:5000` im Browser auf und landen direkt im UI. Optional kann ein Captive Portal eingerichtet werden, das automatisch öffnet.



Dieser Schritt wird \*\*zuletzt\*\* eingerichtet, da er das Debugging erschwert (kein direkter SSH-Zugang ohne zweite Netzwerkkarte oder USB-Gadget-Modus).



\*\*\*



\## Entwicklungsreihenfolge



1\. \*\*Flask-Skeleton + SocketIO\*\* – Server starten, WebSocket-Verbindung im Browser prüfen

2\. \*\*UART-Kommunikation\*\* – Marlin-Simulator anschließen, `G28` senden, `ok` empfangen

3\. \*\*G-Code-Generator\*\* – SVG einlesen, G-Code ausgeben, in ncviewer.com prüfen

4\. \*\*Relay-Sequenzer\*\* – Rezept laden, Ablauf mit GPIO-Mock simulieren

5\. \*\*ToF-Sensor\*\* – I2C-Verbindung prüfen, Messwert auslesen, Z-Offset übergeben

6\. \*\*LED + Servo + Button\*\* – Hardware einzeln testen, dann in Gesamtablauf integrieren

7\. \*\*Frontend-Integration\*\* – Alle Module per SocketIO ans UI anbinden

8\. \*\*Systemtest per LAN\*\* – Handy im gleichen WLAN, vollständiger Druckdurchlauf

9\. \*\*WLAN-Hotspot\*\* – Letzter Schritt, sobald alles stabil läuft



\*\*\*



\## Technologie-Übersicht



| Komponente | Technologie | Paket / Tool |

|---|---|---|

| Web-Backend | Python / Flask | `flask`, `flask-socketio`, `eventlet` |

| Echtzeit-Kommunikation | WebSocket | `flask-socketio` |

| Marlin-Steuerung | UART / Serial | `pyserial` |

| SVG-Verarbeitung | Python | `svgpathtools` |

| PNG → SVG | CLI | `potrace` |

| Relay-Steuerung | GPIO | `RPi.GPIO` |

| ToF-Sensor | I2C | `adafruit-circuitpython-vl53l0x` |

| Status-LED | GPIO / WS2812B | `rpi\_ws281x` |

| Servo | GPIO PWM | `RPi.GPIO` |

| Hotspot | Linux | `hostapd`, `dnsmasq` |

| Settings | JSON-Dateien | Filesystem |



\*\*\*



\## Installation (Kurzfassung)



```bash

sudo apt update \&\& sudo apt install -y python3-pip python3-venv i2c-tools potrace

python3 -m venv venv \&\& source venv/bin/activate



pip install flask flask-socketio eventlet pyserial svgpathtools \\

&#x20;   adafruit-circuitpython-vl53l0x rpi\_ws281x RPi.GPIO



\# Schnittstellen aktivieren

sudo raspi-config

\# → Interface Options → I2C aktivieren

\# → Interface Options → Serial: Login-Shell NEIN, Hardware-UART JA

```


## Neue GPIO-Eingaenge und Case LED

Die Maschine nutzt jetzt vier Raspberry-Pi-Header-Eingaenge, die alle gegen `GND` schalten. Deshalb werden sie softwareseitig als `pull-up` und `active-low` behandelt.

- `Pin 7` ist der System-Switch.
- `Pin 29`, `Pin 31` und `Pin 26` sind kurze Push-Buttons fuer spaetere Quick Actions.
- `Pin 23` schaltet das direkte Relay fuer die Vacuum Valve.
- Die Case LED / das LED Filament wird ueber Marlin `P27` geschaltet.

### System-Switch Verhalten

Wenn der System-Switch auf `off` steht, bleibt die Website weiter erreichbar, aber die Maschine geht in einen sicheren Ruhemodus:

- Druckjobs werden abgebrochen.
- Laufende Foam-Sequenzen werden gestoppt.
- Manuelle Hardware-Aktionen ueber die API werden blockiert.
- WS2812-Status-LED, Vacuum Relay und Case LED werden ausgeschaltet.

Beim Zurueckschalten auf `on` wird kein Job automatisch neu gestartet. Das System ist dann wieder bereit fuer neue Aktionen.

### Case LED

Die Case LED kann im Advanced Tab ueber zwei Buttons ein- und ausgeschaltet werden. Der Web-Zustand wird gespeichert und ueber Marlin `M42 P27` ausgegeben, aber der System-Switch hat immer Vorrang:

- `System on` + `Case LED on` => `P27` aktiv
- `System off` => `P27` immer aus

Nach dem erneuten Einschalten des System-Switches kehrt die Case LED auf den zuletzt im Web gesetzten Zustand zurueck.
=======
# Artista-Amara
Latte Art 3D Printer Software and Interface
>>>>>>> cca30468434a43e3eb4513386f3156768138fcbf
