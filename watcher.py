#!/usr/bin/env python3
"""
watcher.py – PC-seitiger DA3- und RealityScan-Watcher
======================================================
Überwacht Supabase auf scan_status = "processing", lädt dann die
27 Pi-Frames herunter und führt zwei Verarbeitungspfade sequenziell aus:

  PFAD A – DA3 (schnell, 6 Frames):
    Wählt 6 Frames aus der Tilt=90°-Reihe aus
    (Pan-Positionen: 10, 50, 70, 110, 130, 170°).
    DA3 erzeugt scene.glb → Supabase → scan_status = "complete".

  PFAD B – RealityScan 2.0 (langsam, 27 Frames):
    Alle 27 Frames → RealityScan CLI → scene_mesh.glb
    → Supabase → mesh_status = "complete".

Konfiguration über .env (oder Umgebungsvariablen):
    SUPABASE_URL        Projekt-URL (https://xxx.supabase.co)
    SUPABASE_KEY        service_role Key
    NEPTUN_DEVICE_ID    UUID des Pi-Geräts in der devices-Tabelle
    DA3_CMD             DA3-Binary                    (Standard: da3)
    DA3_MODEL_DIR       Modellpfad für DA3
                        (Standard: depth-anything/DA3-LARGE)
    WORKSPACE_DIR       Lokales Eingabeverzeichnis     (Standard: workspace/scan_input)
    VIEWER_DIR          DA3-Ausgabe für scene.glb       (Standard: viewer)
    REALITYSCAN_EXE     Pfad zur RealityScan.exe
                        (Standard: /mnt/c/Program Files/Epic Games/
                         RealityScan_2.1/RealityScan.exe)

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

# 6 Pan-Positionen aus der Tilt=90°-Reihe, die DA3 erhält (Indices 0,2,3,5,6,8)
_DA3_PAN_SUBSET = {10.0, 50.0, 70.0, 110.0, 130.0, 170.0}


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
    """Lädt alle Frames aus Supabase Storage nach workspace/ herunter."""
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


def _select_da3_frames(metadata: dict) -> list[dict]:
    """Wählt 6 Frames aus der Tilt≈90°-Reihe für DA3 aus."""
    tilt90   = [f for f in metadata["frames"] if abs(f["tilt_deg"] - 90.0) < 1.0]
    selected = [f for f in tilt90 if f["pan_deg"] in _DA3_PAN_SUBSET]
    selected.sort(key=lambda f: f["pan_deg"])
    return selected


def _prepare_da3_input(workspace: Path, da3_frames: list[dict]) -> Path:
    """Kopiert die 6 DA3-Frames in workspace/da3_input/ und schreibt Sub-Metadata."""
    da3_dir = workspace.parent / "da3_input"
    if da3_dir.exists():
        shutil.rmtree(da3_dir)
    da3_dir.mkdir(parents=True)

    for frame in da3_frames:
        shutil.copy2(workspace / frame["filename"], da3_dir / frame["filename"])

    sub_meta = {"frames": da3_frames, "frame_count": len(da3_frames)}
    (da3_dir / "scan_metadata.json").write_text(
        json.dumps(sub_meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info(f"DA3-Eingabe: {len(da3_frames)} Frames in {da3_dir}")
    return da3_dir


def _run_da3(workspace: Path, viewer: Path, da3_cmd: str, model_dir: str) -> bool:
    """Führt DA3 aus. Gibt True bei Erfolg zurück."""
    viewer.mkdir(parents=True, exist_ok=True)
    cmd = [
        da3_cmd, "auto", str(workspace),
        "--export-format", "glb",
        "--export-dir",    str(viewer),
        "--model-dir",     model_dir,
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


def _to_win_path(path: Path) -> str:
    """
    Konvertiert einen Linux/WSL-Pfad in einen Windows-Pfad fuer Windows-Exe-Aufrufe.
    Benutzt wslpath -w; Fallback fuer /mnt/<drive>/... Pfade.
    """
    try:
        result = subprocess.run(
            ["wslpath", "-w", str(path.resolve())],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    # Fallback: /mnt/c/foo/bar -> C:\foo\bar
    p = str(path.resolve())
    if p.startswith("/mnt/") and len(p) > 6:
        parts = p[5:].split("/", 1)
        drive = parts[0].upper() + ":\\"
        rest  = parts[1].replace("/", "\\") if len(parts) > 1 else ""
        return drive + rest
    # WSL Linux filesystem: /home/... -> \\wsl.localhost\Ubuntu\home\...
    return "\\\\wsl.localhost\\Ubuntu" + p.replace("/", "\\")


def _run_realityscan(workspace: Path, viewer: Path, realityscan_exe: str) -> bool:
    """
    Fuehrt RealityScan 2.x (RealityCapture-Engine) auf allen 27 Frames aus.
    Ausgabe: viewer/scene_mesh.glb (texturiertes Mesh)

    Hinweis: RealityCapture beendet sich haeufig mit Exit-Code != 0, auch wenn
    der Export erfolgreich war. Der Exit-Code wird daher ignoriert; stattdessen
    wird geprueft ob scene_mesh.glb tatsaechlich existiert und Inhalt hat.
    """
    viewer.mkdir(parents=True, exist_ok=True)
    output_glb = viewer / "scene_mesh.glb"

    win_input  = _to_win_path(workspace)
    win_output = _to_win_path(output_glb)

    cmd = [
        realityscan_exe,
        "-addFolder",                  win_input,
        "-align",
        "-selectMaximalComponent",
        "-setReconstructionRegionAuto",
        "-calculateHighModel",
        "-calculateTexture",
        "-renameSelectedModel",        "scene_mesh",
        "-exportModel",                "scene_mesh",  win_output,
        "-quit",
    ]
    logger.info(f"RealityScan-Befehl: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, timeout=3600)
        # RC exits non-zero even on success – trust the output file, not the exit code.
        if output_glb.exists() and output_glb.stat().st_size > 0:
            if result.returncode != 0:
                logger.warning(
                    f"RealityScan exitcode={result.returncode} aber "
                    f"scene_mesh.glb ({output_glb.stat().st_size // 1024} KB) vorhanden – OK"
                )
            return True
        logger.error(
            f"RealityScan: scene_mesh.glb nicht erzeugt (exitcode={result.returncode})"
        )
        return False
    except subprocess.TimeoutExpired:
        logger.error("RealityScan-Timeout (>60 min)")
        return False
    except FileNotFoundError:
        logger.error(f"RealityScan.exe nicht gefunden: {realityscan_exe}")
        return False


def _upload_scene_glb(sb, device_id: str, viewer: Path, workspace: Path) -> bool:
    """Lädt scene.glb + scan_metadata.json nach Supabase hoch."""
    storage  = sb.storage.from_("scans")
    glb_path = viewer / "scene.glb"

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

        meta_path = workspace / "scan_metadata.json"
        if meta_path.exists():
            with open(meta_path, "rb") as fh:
                storage.upload(
                    path=f"{device_id}/scan_metadata.json",
                    file=fh.read(),
                    file_options={"content-type": "application/json", "upsert": "true"},
                )
            logger.info(f"  ↑  {device_id}/scan_metadata.json")

        return True
    except Exception as exc:
        logger.error(f"Upload scene.glb fehlgeschlagen: {exc}", exc_info=True)
        return False


def _upload_mesh_glb(sb, device_id: str, viewer: Path) -> bool:
    """
    Laedt scene_mesh.glb + alle scene_mesh*.png Textur-Sidecars nach Supabase hoch.

    RealityCapture exportiert Texturen als externe PNG-Dateien neben dem GLB
    (scene_mesh_u0_v0_diffuse.png etc.). Der GLB referenziert sie per relativem
    Pfad, der im Browser gegen die Supabase-URL aufgeloest wird – deshalb muessen
    die PNGs im gleichen Supabase-Ordner liegen wie die GLB.
    """
    storage  = sb.storage.from_("scans")
    glb_path = viewer / "scene_mesh.glb"

    if not glb_path.exists():
        logger.error(f"scene_mesh.glb nicht gefunden: {glb_path}")
        return False

    try:
        with open(glb_path, "rb") as fh:
            storage.upload(
                path=f"{device_id}/scene_mesh.glb",
                file=fh.read(),
                file_options={"content-type": "model/gltf-binary", "upsert": "true"},
            )
        logger.info(f"  ↑  {device_id}/scene_mesh.glb")

        # Externe Textur-Sidecars mituploaden (scene_mesh_u0_v0_diffuse.png etc.)
        for png in sorted(viewer.glob("scene_mesh*.png")):
            with open(png, "rb") as fh:
                storage.upload(
                    path=f"{device_id}/{png.name}",
                    file=fh.read(),
                    file_options={"content-type": "image/png", "upsert": "true"},
                )
            logger.info(f"  ↑  {device_id}/{png.name}")

        return True
    except Exception as exc:
        logger.error(f"Upload scene_mesh.glb fehlgeschlagen: {exc}", exc_info=True)
        return False


def process_scan(
    sb,
    device_id: str,
    workspace: Path,
    viewer: Path,
    da3_cmd: str,
    model_dir: str,
    realityscan_exe: str,
) -> None:
    """Kompletter Verarbeitungs-Workflow: DA3 (Pfad A) dann RealityScan (Pfad B)."""
    logger.info("=== Scan-Verarbeitung startet ===")

    # scan_metadata.json aus Supabase laden
    try:
        meta_bytes = sb.storage.from_("scans").download(f"{device_id}/scan_metadata.json")
        metadata   = json.loads(meta_bytes.decode("utf-8"))
    except Exception as exc:
        logger.error(f"scan_metadata.json konnte nicht geladen werden: {exc}")
        _set_scan_status(sb, device_id, "error")
        return

    # Altes Workspace leeren und alle 27 Frames herunterladen
    if workspace.exists():
        shutil.rmtree(workspace)
    if not _download_frames(sb, device_id, workspace, metadata):
        _set_scan_status(sb, device_id, "error")
        return

    (workspace / "scan_metadata.json").write_bytes(
        json.dumps(metadata, indent=2, ensure_ascii=False).encode()
    )

    # ── PFAD A: DA3 (schnell, 6 Frames aus Tilt=90°-Reihe) ──────────────────────
    logger.info("--- PFAD A: DA3 ---")
    da3_frames = _select_da3_frames(metadata)
    if len(da3_frames) < 4:
        logger.warning(f"Nur {len(da3_frames)} DA3-Frames gefunden – DA3 übersprungen.")
        _set_scan_status(sb, device_id, "error")
    else:
        da3_input = _prepare_da3_input(workspace, da3_frames)
        if _run_da3(da3_input, viewer, da3_cmd, model_dir):
            if _upload_scene_glb(sb, device_id, viewer, workspace):
                _set_scan_status(sb, device_id, "complete")
            else:
                _set_scan_status(sb, device_id, "error")
        else:
            _set_scan_status(sb, device_id, "error")

    # ── PFAD B: RealityScan (langsam, alle 27 Frames) ──────────────────────
    logger.info("--- PFAD B: RealityScan ---")
    if _run_realityscan(workspace, viewer, realityscan_exe):
        if _upload_mesh_glb(sb, device_id, viewer):
            _set_mesh_status(sb, device_id, "complete")
        else:
            _set_mesh_status(sb, device_id, "error")
    else:
        _set_mesh_status(sb, device_id, "error")

    logger.info("=== Scan-Verarbeitung abgeschlossen ===")


def main() -> None:
    supabase_url    = _require_env("SUPABASE_URL")
    supabase_key    = _require_env("SUPABASE_KEY")
    device_id       = _require_env("NEPTUN_DEVICE_ID")
    da3_cmd         = os.environ.get("DA3_CMD",       "da3")
    model_dir       = os.environ.get("DA3_MODEL_DIR",  "depth-anything/DA3-LARGE")
    workspace       = Path(os.environ.get("WORKSPACE_DIR", "workspace/scan_input"))
    viewer          = Path(os.environ.get("VIEWER_DIR",    "viewer"))
    realityscan_exe = os.environ.get(
        "REALITYSCAN_EXE",
        "/mnt/c/Program Files/Epic Games/RealityScan_2.1/RealityScan.exe",
    )

    sb = create_client(supabase_url, supabase_key)
    logger.info(f"BirdGuard Watcher gestartet – Device {device_id}")
    logger.info(f"RealityScan: {realityscan_exe}")
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
