# TempoWarnerUpdate

Offline-Straßendaten mit Tempolimits für die TempoWarner-App, je Bundesland eine SQLite-Datei.
Gebaut monatlich von GitHub Actions (`.github/workflows/build.yml`) aus den
[Geofabrik](https://download.geofabrik.de/europe/germany.html)-Auszügen mit `build_region.py`
und als Release veröffentlicht. Die App lädt `releases/latest/download/index.json` und daraus die Regionen.

Selbst bauen: `pip install -r requirements.txt && python build_region.py --region europe/germany/bayern`

**Kartendaten © OpenStreetMap-Mitwirkende**, verfügbar unter der
[Open Database License (ODbL)](https://www.openstreetmap.org/copyright).
Die Datenbanken sind abgeleitete Datenbanken im Sinne der ODbL und stehen ebenfalls unter der ODbL.
Skripte: GPL-3.0.
