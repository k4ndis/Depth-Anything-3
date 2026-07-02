#!/usr/bin/env python3
"""
pose_priors.py – RealityScan-Pose-Priors aus der NEPTUN-Rig-Kinematik
========================================================================
Berechnet für jeden Scan-Frame (pan_deg/tilt_deg aus scan_metadata.json) über
rig_kinematics.forward() die erwartete Kamerapose und schreibt sie als
XMP-Sidecar neben das jeweilige Bild. RealityScan liest solche Sidecars beim
Import (-addFolder) automatisch als Pose-Prior für die Bündelausgleichung.

Zwei Prior-Arten:

  Absolut, pro Aufnahme ("Position and orientation", inpPose=2):
      Servo-Encoder haben Spiel/Ungenauigkeit – die Bündelausgleichung soll
      um den berechneten Startwert herum verfeinern dürfen, NICHT "Locked"
      (inpPose=3). Schon heute mit dem Testmodell (1 Kamera, echte Fotos)
      testbar.

  Relativ, für ein Stereo-Paar ("Exact", inpPosePriorRelative=2, gemeinsame
      inpPosePriorRelativeGroup): Baseline ist mechanisch starr und exakt
      bekannt. Nur relevant für RIG_PILOTMODELL (2 Kameras) – Code ist
      fertig, aber erst testbar sobald das Pilotmodell physisch da ist.

WICHTIG – unverifizierte Annahme: Die Rotationsmatrix wird im Rig-
Referenzsystem ausgegeben, wie in rig_kinematics.py definiert (Y-up, +Z
= vorne bei Pan/Tilt=Home, +X = rechts). Ob RealityScans eigenes lokales
Koordinatensystem beim Import dieselbe Konvention erwartet (z. B. Y-up vs.
Z-up), ist NICHT verifiziert (siehe Handoff Abschnitt 4, "RealityScan-
Export-Settings prüfen") – beim ersten echten Alignment-Test gegenprüfen,
ggf. eine Basiswechsel-Matrix ergänzen.

rig_kinematics.py existiert nur im yolo-Repo (eine Quelle der Wahrheit).
Import über NEPTUN_CALIBRATION_PATH (.env), siehe unten.

Verwendung (Bibliothek, aufgerufen aus watcher.py):
    from pose_priors import write_pose_priors

    write_pose_priors(workspace_dir, metadata, config=RIG_TESTMODELL)

Standalone (z. B. zum manuellen Nachrechnen eines vorhandenen Scans):
    python pose_priors.py <scan_dir_mit_scan_metadata.json> [--rig testmodell|pilotmodell]
"""

import argparse
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
    Orientation,
    Position,
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

# RealityScan-Pose-Prior-Modi (siehe Handoff, Abschnitt 2).
INP_POSE_POSITION_AND_ORIENTATION = 2   # verfeinerbarer Startwert (Servo-Ungenauigkeit)
INP_POSE_LOCKED = 3                     # fix, keine Verfeinerung (hier NICHT verwendet)
INP_POSE_PRIOR_RELATIVE_EXACT = 2       # Stereo-Baseline, mechanisch starr


def _rotation_matrix_row_major(orientation: Orientation) -> tuple[float, ...]:
    """3x3-Rotationsmatrix (Kamera→Rig-Welt, row-major, roll=0) aus yaw/pitch.

    Spalten = Kamera-Achsen (rechts, oben, vorne) im Rig-Referenzsystem –
    siehe rig_kinematics.py Modul-Docstring für die Achsenkonvention.
    """
    from math import cos, radians, sin

    theta = radians(orientation.yaw_deg)
    phi = radians(orientation.pitch_deg)

    right_world = (cos(theta), 0.0, -sin(theta))
    up_world = (-sin(phi) * sin(theta), cos(phi), -sin(phi) * cos(theta))
    forward_world = (sin(theta) * cos(phi), sin(phi), cos(theta) * cos(phi))

    r00, r10, r20 = right_world
    r01, r11, r21 = up_world
    r02, r12, r22 = forward_world
    return (r00, r01, r02, r10, r11, r12, r20, r21, r22)


_XMP_TEMPLATE = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
        xmlns:xcr="http://www.capturingreality.com/ns/xcr/1.1#"
        xcr:Version="3"
        xcr:Coordinates="local"
        xcr:inpPose="{inp_pose}"{relative_attrs}>
      <xcr:Rotation>{rotation}</xcr:Rotation>
      <xcr:Position>{position}</xcr:Position>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def write_pose_prior_xmp(
    image_path: Path,
    position: Position,
    orientation: Orientation,
    *,
    inp_pose: int = INP_POSE_POSITION_AND_ORIENTATION,
    relative_group: str | None = None,
    relative_prior: int | None = None,
) -> Path:
    """Schreibt ein XMP-Sidecar (<image>.xmp) mit Pose-Prior für RealityScan."""
    rot = _rotation_matrix_row_major(orientation)
    relative_attrs = ""
    if relative_group is not None:
        prior = INP_POSE_PRIOR_RELATIVE_EXACT if relative_prior is None else relative_prior
        relative_attrs = (
            f'\n        xcr:inpPosePriorRelative="{prior}"'
            f'\n        xcr:inpPosePriorRelativeGroup="{relative_group}"'
        )

    xmp = _XMP_TEMPLATE.format(
        inp_pose=inp_pose,
        relative_attrs=relative_attrs,
        rotation=" ".join(f"{v:.9f}" for v in rot),
        position=f"{position.x:.6f} {position.y:.6f} {position.z:.6f}",
    )

    xmp_path = image_path.with_suffix(image_path.suffix + ".xmp")
    xmp_path.write_text(xmp, encoding="utf-8")
    return xmp_path


def write_pose_priors(
    workspace: Path,
    metadata: dict,
    config: RigConfig,
) -> list[Path]:
    """Schreibt Pose-Prior-XMPs für alle Frames aus scan_metadata.json.

    Frames ohne 'camera'-Feld gelten als Single-Kamera-Rig (RIG_TESTMODELL).
    Frames mit 'camera': 'left'/'right' (künftiges RIG_PILOTMODELL-Format,
    siehe scan3d.py-Erweiterung) werden zusätzlich als relative Stereo-Priors
    ("Exact") mit gemeinsamer Gruppe pro Scan-Position markiert.
    """
    written: list[Path] = []
    stereo_groups: dict[tuple[float, float], list[dict]] = {}

    for frame in metadata["frames"]:
        camera = frame.get("camera", "single")
        pan_deg, tilt_deg = frame["pan_deg"], frame["tilt_deg"]
        pos, orient = forward(pan_deg, tilt_deg, config, camera=camera)

        image_path = workspace / frame["filename"]
        if not image_path.exists():
            logger.warning(f"Bild fehlt, Pose-Prior übersprungen: {image_path}")
            continue

        if camera == "single":
            xmp_path = write_pose_prior_xmp(image_path, pos, orient)
            written.append(xmp_path)
            logger.info(f"  ✓ {xmp_path.name}  (pan={pan_deg}° tilt={tilt_deg}°)")
        else:
            stereo_groups.setdefault((pan_deg, tilt_deg), []).append(
                {"frame": frame, "camera": camera, "pos": pos, "orient": orient}
            )

    for group_idx, ((pan_deg, tilt_deg), entries) in enumerate(sorted(stereo_groups.items())):
        group_name = f"stereo_pos_{group_idx:02d}"
        for entry in entries:
            image_path = workspace / entry["frame"]["filename"]
            if not image_path.exists():
                logger.warning(f"Bild fehlt, Pose-Prior übersprungen: {image_path}")
                continue
            xmp_path = write_pose_prior_xmp(
                image_path,
                entry["pos"],
                entry["orient"],
                relative_group=group_name,
            )
            written.append(xmp_path)
            logger.info(
                f"  ✓ {xmp_path.name}  ({entry['camera']}, Gruppe={group_name}, "
                f"pan={pan_deg}° tilt={tilt_deg}°)"
            )

    logger.info(f"Pose-Priors geschrieben: {len(written)} XMP-Sidecar(s) in {workspace}")
    return written


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
    write_pose_priors(args.scan_dir, metadata, config)


if __name__ == "__main__":
    main()
