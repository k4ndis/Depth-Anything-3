#!/usr/bin/env python3
"""
pose_priors.py – RealityScan-Pose-Priors aus der NEPTUN-Rig-Kinematik
========================================================================
Berechnet für jeden Scan-Frame (pan_deg/tilt_deg aus scan_metadata.json) über
rig_kinematics.forward() die erwartete Kamerapose und schreibt sie als
"Flight Log"-CSV (RealityScans dokumentiertes Trajektorie-Importformat,
siehe https://dev.epicgames.com/documentation/realityscan/all-commands,
Befehl `-importFlightLog`, Default-Spalten laut Doku:

    Image, X, Y, Altitude, XAccuracy, YAccuracy, AltitudeAccuracy, Yaw, Pitch, Roll

Y ist dabei die Höhenachse ("Yaw – Rotation um Y-Achse"), X die zweite
horizontale Achse ("Pitch – Rotation um X-Achse"), Z ergibt sich daraus
rechtshändig ("Roll – Rotation um Z-Achse"). Omega/Phi/Kappa (die im GUI unter
"Prior pose" zusätzlich angezeigt werden) gelten laut Doku nur für
georeferenzierte Szenen – unser Projekt ist "local … Euclidean", also NICHT
georeferenziert, deshalb Yaw/Pitch/Roll statt Omega/Phi/Kappa.

Das deckt sich exakt mit rig_kinematics.py's Orientation: yaw_deg ist bereits
eine Rotation um die Pan-Achse (=Y), pitch_deg um die Tilt-Achse (=lokales X)
– keine Rotationsmatrix, keine Basiswechsel-Umrechnung nötig, nur direkte
Übernahme der beiden Werte. Roll ist beim Rig konstruktionsbedingt immer 0
(kein dritter Rotations-Freiheitsgrad).

Frühere Version schrieb XMP-Sidecars – verworfen, weil `-addFolder` diese
beim Import NICHT automatisch liest (bestätigt: "Absolute pose" blieb
"Unknown" trotz vorhandener .xmp-Dateien neben den Bildern). `-importFlightLog`
ist der dokumentierte, für diesen Zweck vorgesehene Mechanismus.

Absoluter Pose-Prior pro Aufnahme, verfeinerbar (nicht "Locked" – Servo-
Encoder haben Spiel/Ungenauigkeit, die Bündelausgleichung soll um den
berechneten Startwert herum verfeinern dürfen): über die Accuracy-Spalten
gesteuert (siehe POSITION_ACCURACY_M / ORIENTATION_ACCURACY_DEG unten),
nicht über einen diskreten Modus wie in der GUI.

Stereo-Paare (RIG_PILOTMODELL, relative Pose zwischen linker/rechter Kamera):
das Flight-Log-Format kennt keine Gruppierung für starre Rig-Baselines. Dafür
braucht es vermutlich `-editInputSelection` mit eigenen Relative-Pose-Keys –
noch nicht recherchiert, weil erst mit der Pilotmodell-Hardware überhaupt
testbar. TODO sobald das Pilotmodell da ist.

rig_kinematics.py existiert nur im yolo-Repo (eine Quelle der Wahrheit).
Import über NEPTUN_CALIBRATION_PATH (.env), siehe unten.

Verwendung (Bibliothek, aufgerufen aus watcher.py):
    from pose_priors import write_flight_log

    csv_path = write_flight_log(workspace_dir, metadata, config=RIG_TESTMODELL)
    # anschließend per CLI: -importFlightLog <csv_path>

Standalone (z. B. zum manuellen Nachrechnen eines vorhandenen Scans):
    python pose_priors.py <scan_dir_mit_scan_metadata.json> [--rig testmodell|pilotmodell]
"""

import argparse
import csv
import json
import logging
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# rig_kinematics.py lebt nur im yolo-Repo – Pfad über .env-Variable, gleicher
# Stil wie SUPABASE_URL (siehe watcher.py).
_calibration_path = os.environ.get("NEPTUN_CALIBRATION_PATH")
if _calibration_path and _calibration_path not in sys.path:
    sys.path.insert(0, _calibration_path)

from neptun.calibration.rig_kinematics import (  # noqa: E402
    RIG_PILOTMODELL,
    RIG_TESTMODELL,
    RigConfig,
    forward,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

RIGS_BY_NAME: dict[str, RigConfig] = {
    "testmodell": RIG_TESTMODELL,
    "pilotmodell": RIG_PILOTMODELL,
}

# Locker genug, damit die Bündelausgleichung frei verfeinern kann (Servo-
# Encoder-Ungenauigkeit + axis_offset-Modellfehler), aber eng genug um bei
# schwacher Bildüberlappung als Startwert zu helfen. Unkalibriert – bei
# Bedarf nach echten Tests nachjustieren.
POSITION_ACCURACY_M = 0.05
ORIENTATION_ACCURACY_DEG = 5.0

_FLIGHT_LOG_COLUMNS = (
    "Image", "X", "Y", "Altitude",
    "XAccuracy", "YAccuracy", "AltitudeAccuracy",
    "Yaw", "Pitch", "Roll",
)


def write_flight_log(
    workspace: Path,
    metadata: dict,
    config: RigConfig,
    csv_filename: str = "pose_priors.csv",
) -> Path | None:
    """Schreibt eine RealityScan-Flight-Log-CSV für alle Single-Kamera-Frames.

    Frames mit 'camera': 'left'/'right' (künftiges Stereo-Format, siehe
    scan3d.py-Erweiterung) werden übersprungen – siehe Modul-Docstring.
    Gibt None zurück (statt eines Pfads), wenn keine Zeile geschrieben wurde.
    """
    rows = []
    skipped_stereo = 0

    for frame in metadata["frames"]:
        camera = frame.get("camera", "single")
        if camera != "single":
            skipped_stereo += 1
            continue

        pan_deg, tilt_deg = frame["pan_deg"], frame["tilt_deg"]
        image_path = workspace / frame["filename"]
        if not image_path.exists():
            logger.warning(f"Bild fehlt, Pose-Prior übersprungen: {image_path}")
            continue

        pos, orient = forward(pan_deg, tilt_deg, config, camera=camera)
        rows.append([
            frame["filename"],
            f"{pos.x:.6f}", f"{pos.y:.6f}", f"{pos.z:.6f}",
            f"{POSITION_ACCURACY_M}", f"{POSITION_ACCURACY_M}", f"{POSITION_ACCURACY_M}",
            f"{orient.yaw_deg:.6f}", f"{orient.pitch_deg:.6f}", "0.0",
        ])

    if skipped_stereo:
        logger.warning(
            f"{skipped_stereo} Stereo-Frame(s) übersprungen "
            "(relative Pose-Priors für RIG_PILOTMODELL noch nicht implementiert)"
        )

    if not rows:
        logger.warning("Keine Pose-Priors zu schreiben (keine Single-Kamera-Frames gefunden)")
        return None

    csv_path = workspace / csv_filename
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(f"# {c}" if i == 0 else c for i, c in enumerate(_FLIGHT_LOG_COLUMNS))
        writer.writerows(rows)

    logger.info(f"Pose-Priors geschrieben: {len(rows)} Zeile(n) in {csv_path}")
    return csv_path


def _parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scan_dir", type=Path, help="Verzeichnis mit scan_metadata.json + Bildern")
    ap.add_argument("--rig", choices=sorted(RIGS_BY_NAME), default="testmodell")
    return ap.parse_args()


def main():
    args = _parse_args()
    meta_path = args.scan_dir / "scan_metadata.json"
    if not meta_path.exists():
        logger.error(f"scan_metadata.json nicht gefunden: {meta_path}")
        sys.exit(1)

    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    config = RIGS_BY_NAME[args.rig]
    write_flight_log(args.scan_dir, metadata, config)


if __name__ == "__main__":
    main()
