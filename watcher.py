#!/usr/bin/env python3
"""
watcher.py – PC-seitiger DA3- und RealityScan-Watcher
======================================================
Überwacht Supabase auf scan_status = "processing", lädt alle 27 Pi-Frames
herunter und verarbeitet sie auf zwei Pfaden:

  Pfad A (schnell):  6 Frames aus Tilt=90°-Reihe → DA3
                     → scene.glb → upload → scan_status = "complete"

  Pfad B (langsam):  alle 27 Frames → RealityCapture CLI
                     → scene_mesh.glb → upload → mesh_status = "complete"

Beide Pfade laufen sequenziell (erst DA3, dann RealityScan).

Konfiguration über .env (oder Umgebungsvariablen):
    SUPABASE_URL        Projekt-URL (https://xxx.supabase.co)
    SUPABASE_KEY        service_role Key
    NEPTUN_DEVICE_ID    UUID des Pi-Geräts in der devices-Tabelle
    DA3_CMD             DA3-Binary (Standard: da3)
    DA3_MODEL_DIR       Modellpfad für DA3
                        (Standard: depth-anything/DA3-LARGE)
    WORKSPACE_DIR       Lokales Eingabeverzeichnis
                        (Standard: workspace/scan_input)
    VIEWER_DIR          DA3-Ausgabeverzeichnis (Standard: viewer)
    REALITYSCAN_EXE     Pfad zu RealityCapture.exe auf dem Windows-Host,
                        erreichbar aus WSL über /mnt/c/...
                        Beispiel:
                        /mnt/c/Program Files/Epic Games/RealityCapture/RealityCapture.exe

HINWEIS (Supabase-Schema):
    ALTER TABLE devices ADD COLUMN mesh_status text DEFAULT 'idle';

Starten (aus ~/Depth-Anything-3):
    python watcher.py
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from supabase import create_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 10.0

# Tilt-Winkel der horizontalen Mittellinie für den DA3-Pfad
DA3_TILT_DEG = 90.0
# Pan-Positionen für DA3: Indizes 0,2,3,5,6,8 aus der 9er-Reihe
# [10,30,50,70,90,110,130,150,170] → [10,50,70,110,130,170] (6 Frames)
DA3_PAN_DEG = {10.0, 50.0, 70.0, 110.0, 130.0, 170.0}


def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        logger.error(f"Umgebungsvariable '{key}' fehlt. Bitte .env anpassen.")
        sys.exit(1)
    return val


def _set_scan_status(sb, device_id: str, status: str) -> None:
    try:
        sb.table("devices").update({"scan_status": status}).eq("id", device_id).execute()
        logger.info(f"scan_status → '{status}'")
    except Exception as exc:
        logger.warning(f"scan_status-Update fehlgeschlagen: {exc}")


def _set_mesh_status(sb, device_id: str, status: str) -> None:
    try:
        sb.table("devices").update({"mesh_status": status}).eq("id", device_id).execute()
        logger.info(f"mesh_status → '{status}'")
    except Exception as exc:
        logger.warning(f"mesh_status-Update fehlgeschlagen: {exc}")


def _download_frames(sb, device_id: str, workspace: Path, metadata: dict) -> bool:
    """Lädt alle Frames aus Supabase Storage in workspace/ herunter."""
    workspace.mkdir(parents=True, exist_ok=True)
    storage = sb.storage.from_("scans")
    for frame in metadata["frames"]:
        src  = f"{device_id}/frames/{frame['filename']}"
        dest = workspace / frame["filename"]
        try:
            data = storage.download(src)
            dest.write_bytes(data)
            logger.info(f"  ↓  {frame['filename']}")
        except Exception as exc:
            logger.error(f"Download fehlgeschlagen: {src} – {exc}")
            return False
    return True


def _select_da3_frames(workspace: Path, da3_workspace: Path, metadata: dict) -> bool:
    """
    Kopiert 6 Frames aus der Tilt=90°-Reihe in da3_workspace.
    Ausgewählt werden Pan-Positionen in DA3_PAN_DEG (6 gleichmäßige Winkel)
    – ausreichend für DA3 bei 8 GB VRAM.
    """
    da3_workspace.mkdir(parents=True, exist_ok=True)
    selected = [
        f for f in metadata["frames"]
        if abs(f["tilt_deg"] - DA3_TILT_DEG) < 0.5
        and round(f["pan_deg"], 1) in DA3_PAN_DEG
    ]

    if len(selected) != 6:
        logger.error(
            f"DA3-Frame-Auswahl fehlgeschlagen: {len(selected)} statt 6 Frames "
            f"(Tilt≈90°, Pan∈{sorted(DA3_PAN_DEG)})"
        )
        return False

    for frame in selected:
        shutil.copy2(workspace / frame["filename"], da3_workspace / frame["filename"])
        logger.info(
            f"  DA3 ← {frame['filename']}  "
            f"(Pan={frame['pan_deg']}°, Tilt={frame['tilt_deg']}°)"
        )

    da3_meta = dict(metadata, frames=selected, frame_count=len(selected))
    (da3_workspace / "scan_metadata.json").write_text(
        json.dumps(da3_meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return True


def _run_da3(workspace: Path, viewer: Path, da3_cmd: str, model_dir: str) -> bool:
    """Führt DA3 aus. Gibt True bei Erfolg zurück."""
    viewer.mkdir(parents=True, exist_ok=True)
    cmd = [
        da3_cmd, "auto", str(workspace),
        "--export-format", "glb",
        "--export-dir", str(viewer),
        "--model-dir", model_dir,
    ]
    logger.info(f"DA3-Befehl: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, check=True)
        return result.returncode == 0
    except subprocess.CalledProcessError as exc:
        logger.error(f"DA3 fehlgeschlagen (returncode={exc.returncode})")
        return False
    except FileNotFoundError:
        logger.error(f"DA3-Binary nicht gefunden: {da3_cmd}")
        return False


def _run_realityscan(workspace: Path, viewer: Path, realityscan_exe: str) -> bool:
    """
    Führt RealityCapture 2.0 CLI mit allen 27 Frames aus und exportiert
    scene_mesh.glb. RealityCapture.exe liegt auf dem Windows-Host;
    Aufruf aus WSL via /mnt/c/... Pfad (REALITYSCAN_EXE).

    CLI-Sequenz (RealityCapture piped commands):
      -addFolder  <workspace>          Bilder einlesen
      -align                           Kamera-Registrierung
      -setReconstructionRegionAuto     Rekonstruktionsbereich automatisch
      -calculateHighModel              Hochauflösendes Mesh berechnen
      -exportModel <name> <output>     Als GLB exportieren
      -quit                            Prozess beenden
    """
    output_path = viewer / "scene_mesh.glb"
    viewer.mkdir(parents=True, exist_ok=True)

    cmd = [
        realityscan_exe,
        "-addFolder",                str(workspace),
        "-align",
        "-setReconstructionRegionAuto",
        "-calculateHighModel",
        "-exportModel",              "scene_mesh", str(output_path),
        "-quit",
    ]
    logger.info(f"RealityCapture-Befehl: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, check=True, timeout=3600)
        return result.returncode == 0
    except subprocess.CalledProcessError as exc:
        logger.error(f"RealityCapture fehlgeschlagen (returncode={exc.returncode})")
        return False
    except subprocess.TimeoutExpired:
        logger.error("RealityCapture: Timeout nach 60 Minuten")
        return False
    except FileNotFoundError:
        logger.error(f"RealityCapture-Binary nicht gefunden: {realityscan_exe}")
        return False


def _upload_glb(sb, device_id: str, glb_path: Path, dest_name: str) -> bool:
    """Lädt eine .glb-Datei in Supabase Storage hoch."""
    if not glb_path.exists():
        logger.error(f"{glb_path.name} nicht gefunden: {glb_path}")
        return False
    try:
        with open(glb_path, "rb") as fh:
            sb.storage.from_("scans").upload(
                path=f"{device_id}/{dest_name}",
                file=fh.read(),
                file_options={"content-type": "model/gltf-binary", "upsert": "true"},
            )
        logger.info(f"  ↑  {device_id}/{dest_name}")
        return True
    except Exception as exc:
        logger.error(f"Upload {dest_name} fehlgeschlagen: {exc}", exc_info=True)
        return False


def _upload_metadata(sb, device_id: str, workspace: Path) -> None:
    """Lädt scan_metadata.json in Supabase Storage hoch (optional)."""
    meta_path = workspace / "scan_metadata.json"
    if not meta_path.exists():
        return
    try:
        with open(meta_path, "rb") as fh:
            sb.storage.from_("scans").upload(
                path=f"{device_id}/scan_metadata.json",
                file=fh.read(),
                file_options={"content-type": "application/json", "upsert": "true"},
            )
        logger.info(f"  ↑  {device_id}/scan_metadata.json")
    except Exception as exc:
        logger.warning(f"Metadata-Upload fehlgeschlagen: {exc}")


def process_scan(
    sb,
    device_id: str,
    workspace: Path,
    viewer: Path,
    da3_cmd: str,
    model_dir: str,
    realityscan_exe: str,
) -> None:
    """
    Kompletter Verarbeitungs-Workflow:
      Pfad A (DA3, schnell)            → scene.glb      → scan_status = "complete"
      Pfad B (RealityCapture, langsam) → scene_mesh.glb → mesh_status = "complete"
    """
    logger.info("=== Scan-Verarbeitung startet ===")

    # scan_metadata.json aus Storage laden
    try:
        meta_bytes = sb.storage.from_("scans").download(f"{device_id}/scan_metadata.json")
        metadata   = json.loads(meta_bytes.decode("utf-8"))
    except Exception as exc:
        logger.error(f"scan_metadata.json konnte nicht geladen werden: {exc}")
        _set_scan_status(sb, device_id, "error")
        _set_mesh_status(sb, device_id, "error")
        return

    # Workspace leeren und alle 27 Frames herunterladen
    if workspace.exists():
        shutil.rmtree(workspace)
    if not _download_frames(sb, device_id, workspace, metadata):
        _set_scan_status(sb, device_id, "error")
        _set_mesh_status(sb, device_id, "error")
        return

    (workspace / "scan_metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # ── Pfad A: DA3 (schnell, 6 Frames aus Tilt=90°-Reihe) ─────────────────────
    logger.info("--- Pfad A: DA3 (6 Frames, Tilt=90°-Reihe) ---")
    da3_workspace = workspace.parent / (workspace.name + "_da3")
    if da3_workspace.exists():
        shutil.rmtree(da3_workspace)

    if not _select_da3_frames(workspace, da3_workspace, metadata):
        _set_scan_status(sb, device_id, "error")
    elif not _run_da3(da3_workspace, viewer, da3_cmd, model_dir):
        _set_scan_status(sb, device_id, "error")
    elif not _upload_glb(sb, device_id, viewer / "scene.glb", "scene.glb"):
        _set_scan_status(sb, device_id, "error")
    else:
        _upload_metadata(sb, device_id, workspace)
        _set_scan_status(sb, device_id, "complete")
        logger.info("--- Pfad A abgeschlossen – Dashboard zeigt DA3-Punktwolke ---")

    # ── Pfad B: RealityCapture (langsam, alle 27 Frames) ─────────────────
    if not realityscan_exe:
        logger.warning("REALITYSCAN_EXE nicht gesetzt – Pfad B übersprungen.")
        _set_mesh_status(sb, device_id, "error")
        logger.info("=== Scan-Verarbeitung abgeschlossen ===")
        return

    logger.info("--- Pfad B: RealityCapture 2.0 (27 Frames) ---")
    if not _run_realityscan(workspace, viewer, realityscan_exe):
        _set_mesh_status(sb, device_id, "error")
    elif not _upload_glb(sb, device_id, viewer / "scene_mesh.glb", "scene_mesh.glb"):
        _set_mesh_status(sb, device_id, "error")
    else:
        _set_mesh_status(sb, device_id, "complete")
        logger.info("--- Pfad B abgeschlossen – Dashboard zeigt RealityScan-Mesh ---")

    logger.info("=== Scan-Verarbeitung abgeschlossen ===")


def main() -> None:
    supabase_url    = _require_env("SUPABASE_URL")
    supabase_key    = _require_env("SUPABASE_KEY")
    device_id       = _require_env("NEPTUN_DEVICE_ID")
    da3_cmd         = os.environ.get("DA3_CMD",           "da3")
    model_dir       = os.environ.get("DA3_MODEL_DIR",     "depth-anything/DA3-LARGE")
    workspace       = Path(os.environ.get("WORKSPACE_DIR", "workspace/scan_input"))
    viewer          = Path(os.environ.get("VIEWER_DIR",    "viewer"))
    realityscan_exe = os.environ.get("REALITYSCAN_EXE",   "")

    if not realityscan_exe:
        logger.warning(
            "REALITYSCAN_EXE nicht gesetzt – Pfad B (RealityCapture) ist deaktiviert. "
            "Beispiel für .env: "
            "REALITYSCAN_EXE=/mnt/c/Program Files/Epic Games/RealityCapture/RealityCapture.exe"
        )

    sb = create_client(supabase_url, supabase_key)
    logger.info(f"BirdGuard Watcher gestartet – Device {device_id}")
    logger.info(f"Polling alle {POLL_INTERVAL_S:.0f}s …")

    last_status: str | None = None

    while True:
        try:
            res = (
                sb.table("devices")
                .select("scan_status")
                .eq("id", device_id)
                .maybe_single()
                .execute()
            )
            if not res.data:
                logger.warning("Device nicht gefunden – nächster Versuch in 30s")
                time.sleep(30.0)
                continue

            status = res.data.get("scan_status", "idle")
            if status != last_status:
                logger.info(f"scan_status: {last_status!r} → {status!r}")
                last_status = status

            if status == "processing":
                process_scan(
                    sb, device_id, workspace, viewer,
                    da3_cmd, model_dir, realityscan_exe,
                )
                last_status = None

        except KeyboardInterrupt:
            logger.info("Watcher beendet.")
            break
        except Exception as exc:
            logger.warning(f"Poll fehlgeschlagen: {exc}")

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
