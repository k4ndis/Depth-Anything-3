#!/usr/bin/env python3
"""
watcher.py – PC-seitiger DA3-Scan-Watcher
==========================================
Ueberwacht Supabase auf scan_status = "processing", laedt dann die
Pi-Frames herunter, fuehrt Depth-Anything-3 aus und laedt scene.glb hoch.
Das Dashboard erkennt scan_status = "complete" und zeigt die 3D-Karte.

Konfiguration ueber .env (oder Umgebungsvariablen):
    SUPABASE_URL      Projekt-URL (https://xxx.supabase.co)
    SUPABASE_KEY      service_role Key
    NEPTUN_DEVICE_ID  UUID des Pi-Geraets in der devices-Tabelle
    DA3_CMD           DA3-Binary (Standard: da3)
    DA3_MODEL_DIR     Modellpfad fuer DA3
                      (Standard: depth-anything/DA3-LARGE)
    WORKSPACE_DIR     Lokales Eingabeverzeichnis fuer Frames
                      (Standard: workspace/scan_input)
    VIEWER_DIR        DA3-Ausgabeverzeichnis fuer scene.glb
                      (Standard: viewer)

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
    pass  # python-dotenv optional; Umgebungsvariablen koennen direkt gesetzt werden

from supabase import create_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 10.0


def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        logger.error(f"Umgebungsvariable '{key}' fehlt. Bitte .env anpassen.")
        sys.exit(1)
    return val


def _set_status(sb, device_id: str, status: str) -> None:
    try:
        sb.table("devices").update({"scan_status": status}).eq("id", device_id).execute()
        logger.info(f"scan_status → '{status}'")
    except Exception as exc:
        logger.warning(f"scan_status-Update fehlgeschlagen: {exc}")


def _download_frames(sb, device_id: str, workspace: Path, metadata: dict) -> bool:
    """Laedt alle Frames aus Supabase Storage in workspace/ herunter."""
    workspace.mkdir(parents=True, exist_ok=True)
    storage = sb.storage.from_("scans")
    for frame in metadata["frames"]:
        src = f"{device_id}/frames/{frame['filename']}"
        dest = workspace / frame["filename"]
        try:
            data = storage.download(src)
            dest.write_bytes(data)
            logger.info(f"  ↓  {frame['filename']}")
        except Exception as exc:
            logger.error(f"Download fehlgeschlagen: {src} – {exc}")
            return False
    return True


def _run_da3(workspace: Path, viewer: Path, da3_cmd: str, model_dir: str) -> bool:
    """Fuehrt DA3 aus. Gibt True bei Erfolg zurueck."""
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


def _upload_results(sb, device_id: str, viewer: Path, workspace: Path) -> bool:
    """Laedt scene.glb + scan_metadata.json in Supabase Storage hoch."""
    storage = sb.storage.from_("scans")
    glb_path  = viewer / "scene.glb"
    meta_path = workspace / "scan_metadata.json"

    if not glb_path.exists():
        logger.error(f"scene.glb nicht gefunden: {glb_path}")
        return False

    try:
        with open(glb_path, "rb") as fh:
            storage.upload(
                path=f"{device_id}/scene.glb",
                file=fh.read(),
                file_options={"content-type": "model/gltf-binary", "upsert": "true"},
            )
        logger.info(f"  ↑  {device_id}/scene.glb")

        if meta_path.exists():
            with open(meta_path, "rb") as fh:
                storage.upload(
                    path=f"{device_id}/scan_metadata.json",
                    file=fh.read(),
                    file_options={"content-type": "application/json", "upsert": "true"},
                )
            logger.info(f"  ↑  {device_id}/scan_metadata.json")
        else:
            logger.warning("scan_metadata.json nicht gefunden – nur scene.glb hochgeladen")

        return True
    except Exception as exc:
        logger.error(f"Upload fehlgeschlagen: {exc}", exc_info=True)
        return False


def process_scan(
    sb,
    device_id: str,
    workspace: Path,
    viewer: Path,
    da3_cmd: str,
    model_dir: str,
) -> None:
    """Kompletter Verarbeitungs-Workflow fuer einen Scan."""
    logger.info("=== Scan-Verarbeitung startet ===")

    # scan_metadata.json aus Storage laden
    try:
        meta_bytes = sb.storage.from_("scans").download(f"{device_id}/scan_metadata.json")
        metadata = json.loads(meta_bytes.decode("utf-8"))
    except Exception as exc:
        logger.error(f"scan_metadata.json konnte nicht geladen werden: {exc}")
        _set_status(sb, device_id, "error")
        return

    # Altes Workspace-Verzeichnis leeren
    if workspace.exists():
        shutil.rmtree(workspace)

    # Frames herunterladen
    if not _download_frames(sb, device_id, workspace, metadata):
        _set_status(sb, device_id, "error")
        return

    # Metadata lokal ablegen (DA3 erwartet sie im Workspace)
    (workspace / "scan_metadata.json").write_bytes(
        json.dumps(metadata, indent=2, ensure_ascii=False).encode()
    )

    # DA3 ausfuehren
    if not _run_da3(workspace, viewer, da3_cmd, model_dir):
        _set_status(sb, device_id, "error")
        return

    # Ergebnisse hochladen
    if not _upload_results(sb, device_id, viewer, workspace):
        _set_status(sb, device_id, "error")
        return

    _set_status(sb, device_id, "complete")
    logger.info("=== Scan-Verarbeitung abgeschlossen ===")


def main() -> None:
    supabase_url = _require_env("SUPABASE_URL")
    supabase_key = _require_env("SUPABASE_KEY")
    device_id    = _require_env("NEPTUN_DEVICE_ID")
    da3_cmd      = os.environ.get("DA3_CMD",      "da3")
    model_dir    = os.environ.get("DA3_MODEL_DIR", "depth-anything/DA3-LARGE")
    workspace    = Path(os.environ.get("WORKSPACE_DIR", "workspace/scan_input"))
    viewer       = Path(os.environ.get("VIEWER_DIR",    "viewer"))

    sb = create_client(supabase_url, supabase_key)
    logger.info(f"BirdGuard DA3-Watcher gestartet – Device {device_id}")
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
                logger.warning("Device nicht gefunden – naechster Versuch in 30s")
                time.sleep(30.0)
                continue

            status = res.data.get("scan_status", "idle")
            if status != last_status:
                logger.info(f"scan_status: {last_status!r} → {status!r}")
                last_status = status

            if status == "processing":
                process_scan(sb, device_id, workspace, viewer, da3_cmd, model_dir)
                last_status = None  # Neu lesen beim naechsten Poll

        except KeyboardInterrupt:
            logger.info("Watcher beendet.")
            break
        except Exception as exc:
            logger.warning(f"Poll fehlgeschlagen: {exc}")

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
