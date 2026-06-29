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
    → Draco-Komprimierung (Geometrie)
    → Supabase → mesh_status = "complete".

    Hinweis zur Dateigröße: Nach Draco ist das GLB typischerweise 60-80 MB,
    was das Supabase Free-Plan-Limit (50 MB) überschreitet. Upload wird trotzdem
    versucht. Eine Lösung (Cloud-Storage, Textur-Resize, GPU-Komprimierung)
    wird separat umgesetzt.

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
    GLTF_TRANSFORM_CMD  Pfad zu gltf-transform CLI     (Standard: gltf-transform)
                        Installieren: npm install -g @gltf-transform/cli

Starten (aus ~/Depth-Anything-3):
    python watcher.py
"""

import json
import logging
import math
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

# gltf-transform CLI für Draco-Komprimierung (npm install -g @gltf-transform/cli)
_GLTF_TRANSFORM_CMD = os.environ.get("GLTF_TRANSFORM_CMD", "gltf-transform")

# Supabase Free-Plan: 50 MB pro Datei (nicht konfigurierbar ohne Pro-Upgrade)
_SUPABASE_MAX_MB = 50.0


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


def _quat_rotate_minus_z(qw: float, qx: float, qy: float, qz: float) -> tuple[float, float, float]:
    """Rotate (0,0,-1) by unit quaternion q → camera forward (OpenGL -Z convention)."""
    vx, vy, vz = 0.0, 0.0, -1.0
    tx = 2 * (qy * vz - qz * vy)
    ty = 2 * (qz * vx - qx * vz)
    tz = 2 * (qx * vy - qy * vx)
    return (
        vx + qw * tx + qy * tz - qz * ty,
        vy + qw * ty + qz * tx - qx * tz,
        vz + qw * tz + qx * ty - qy * tx,
    )


def _opk_to_fwd_rc(omega_deg: float, phi_deg: float, kappa_deg: float) -> tuple[float, float, float]:
    """OPK Euler angles → forward vector in RC's native Z-up world space.
    Uses R = Rz(K) @ Ry(P) @ Rx(O); forward = R @ (0, 0, 1) (photogrammetry +Z)."""
    o = math.radians(omega_deg)
    p = math.radians(phi_deg)
    k = math.radians(kappa_deg)
    co, so = math.cos(o), math.sin(o)
    cp, sp = math.cos(p), math.sin(p)
    ck, sk = math.cos(k), math.sin(k)
    return (ck * sp * co + sk * so,
            sk * sp * co - ck * so,
            cp * co)


def _parse_mesh_cameras_txt(cameras_txt: Path) -> list[dict] | None:
    """
    Parst den RealityCapture-Kamera-Export.

    Unterstützte Formate:

    COLMAP images.txt (bevorzugt – kein GPS nötig, in RC-GUI als Default wählen):
        IMAGE_ID  QW  QX  QY  QZ  TX  TY  TZ  CAMERA_ID  NAME
        Erkennung: Name steht am Ende, Quaternion an Pos 1-4.
        Konvention: q ist World→Camera, Kamera schaut +Z → forward = q⁻¹·(0,0,1)

    OPK CSV (nur bei geo-referenzierter Szene verfügbar):
        name, x, y, z, omega, phi, kappa

    Quaternion (name zuerst):
        name  x  y  z  qw  qx  qy  qz

    Rotationsmatrix (name zuerst):
        name  x  y  z  r11 r12 r13  r21 r22 r23  r31 r32 r33 …

    Rückgabe: [{filename, fwd_x, fwd_y, fwd_z}] in Three.js Y-up-Raum.
    """
    if not cameras_txt.exists():
        logger.warning(f"Kamera-Export nicht gefunden: {cameras_txt}")
        return None

    cameras = []
    with open(cameras_txt, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace(",", " ").split()
            if len(parts) < 7:
                continue

            # ── COLMAP format detection ─────────────────────────────────────
            # In COLMAP images.txt the filename is the LAST token.
            # All preceding tokens are numeric (IMAGE_ID + 7 floats + CAMERA_ID).
            colmap = False
            try:
                float(parts[-1])   # last token numeric → not COLMAP (no filename at end)
            except ValueError:
                # Last token is non-numeric → filename at end → likely COLMAP
                if len(parts) >= 10:
                    try:
                        _id = int(parts[0])   # IMAGE_ID must be integer
                        qw  = float(parts[1])
                        qx  = float(parts[2])
                        qy  = float(parts[3])
                        qz  = float(parts[4])
                        # Verify unit quaternion
                        if abs(qw**2 + qx**2 + qy**2 + qz**2 - 1.0) < 0.1:
                            colmap = True
                    except (ValueError, IndexError):
                        pass

            if colmap:
                name = parts[-1]
                # q is world→camera, camera looks along +Z in camera space.
                # forward_world = q⁻¹ · (0,0,1) = –(q⁻¹ · (0,0,–1))
                #               = –_quat_rotate_minus_z(qw, –qx, –qy, –qz)
                r = _quat_rotate_minus_z(qw, -qx, -qy, -qz)
                fx_rc, fy_rc, fz_rc = -r[0], -r[1], -r[2]
            else:
                # ── Name-first formats ───────────────────────────────────────
                name = parts[0]
                try:
                    nums = [float(p) for p in parts[1:]]
                except ValueError:
                    continue
                if len(nums) < 6:
                    continue

                if len(nums) >= 12:
                    # Rotation-matrix: name x y z r11 r12 r13 r21 r22 r23 r31 r32 r33 …
                    # R maps world→camera; forward_world = –R^T[:,2] = –(r31,r32,r33)
                    fx_rc, fy_rc, fz_rc = -nums[9], -nums[10], -nums[11]
                elif len(nums) >= 7:
                    qw2, qx2, qy2, qz2 = nums[3], nums[4], nums[5], nums[6]
                    if abs(qw2**2 + qx2**2 + qy2**2 + qz2**2 - 1.0) < 0.05:
                        # Quaternion (camera→world), camera looks –Z
                        fx_rc, fy_rc, fz_rc = _quat_rotate_minus_z(qw2, qx2, qy2, qz2)
                    else:
                        fx_rc, fy_rc, fz_rc = _opk_to_fwd_rc(nums[3], nums[4], nums[5])
                else:
                    # OPK: name x y z omega phi kappa
                    fx_rc, fy_rc, fz_rc = _opk_to_fwd_rc(nums[3], nums[4], nums[5])

            # RC Z-up → Three.js Y-up: (X, Y, Z)_rc → (X, Z, –Y)_yup
            cameras.append({
                "filename": name,
                "fwd_x":    round(fx_rc,  6),
                "fwd_y":    round(fz_rc,  6),
                "fwd_z":    round(-fy_rc, 6),
            })

    if not cameras:
        logger.warning("Keine gültigen Kamera-Einträge in der Exportdatei gefunden")
        return None

    logger.info(f"Kamera-Export: {len(cameras)} Kameras geparst aus {cameras_txt.name}")
    return cameras


def _write_mesh_cameras_json(cameras: list[dict], viewer: Path) -> Path:
    """Schreibt mesh_cameras.json in das viewer-Verzeichnis."""
    out = viewer / "mesh_cameras.json"
    out.write_text(
        json.dumps({"camera_count": len(cameras), "cameras": cameras},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info(f"mesh_cameras.json geschrieben ({len(cameras)} Kameras)")
    return out


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

    Qualität: calculateNormalModel (statt High) – erzeugt deutlich kleinere
    Dateien (~10-80 MB) die in Supabase Storage hochgeladen werden können.
    calculateHighModel erzeugte ~500 MB, was das Supabase-Limit (50 MB) bei
    weitem überschreitet.

    Hinweis: RealityCapture beendet sich häufig mit Exit-Code != 0, auch wenn
    der Export erfolgreich war. Der Exit-Code wird daher ignoriert; stattdessen
    wird geprüft ob scene_mesh.glb tatsächlich existiert und Inhalt hat.
    """
    viewer.mkdir(parents=True, exist_ok=True)
    output_glb   = viewer / "scene_mesh.glb"
    # RC exports registration in the format last selected in the GUI.
    # One-time setup: choose "COLMAP" in Export → Registration once.
    # Parser auto-detects COLMAP / OPK / quaternion / rotation-matrix.
    cameras_txt  = viewer / "mesh_cameras.txt"

    win_input    = _to_win_path(workspace)
    win_output   = _to_win_path(output_glb)

    # RC's -exportRegistration cannot write to \\wsl.localhost\... UNC paths
    # (err:5618), even though -exportModel can.  Route the export to a native
    # Windows temp path and copy back after RC completes.
    win_cameras_candidate = _to_win_path(cameras_txt)
    if win_cameras_candidate.startswith("\\\\wsl"):
        cameras_win_linux = Path("/mnt/c/Windows/Temp/neptun_mesh_cameras.txt")
        win_cameras = _to_win_path(cameras_win_linux)
        logger.info(
            f"Kamera-Export via Windows-Temp: {win_cameras} → {cameras_txt.name}"
        )
    else:
        cameras_win_linux = cameras_txt
        win_cameras = win_cameras_candidate

    cmd = [
        realityscan_exe,
        "-addFolder",                  win_input,
        "-align",
        # Do NOT use -selectMaximalComponent: it drops camera groups that cover
        # the extreme pan positions (10°/170°) if they form a separate component,
        # which causes the left/right sides of the room to vanish from the mesh.
        # RC will reconstruct everything that aligned.
        "-setReconstructionRegionAuto",
        "-calculateNormalModel",
        "-calculateTexture",
        "-renameSelectedModel",        "scene_mesh",
        "-exportModel",                "scene_mesh",  win_output,
        "-exportRegistration",         win_cameras,
        "-quit",
    ]
    logger.info(f"RealityScan-Befehl: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, timeout=3600)
        # RC exits non-zero even on success – trust the output file, not the exit code.
        if output_glb.exists() and output_glb.stat().st_size > 0:
            size_mb = output_glb.stat().st_size / 1024 / 1024
            if result.returncode != 0:
                logger.warning(
                    f"RealityScan exitcode={result.returncode} aber "
                    f"scene_mesh.glb ({size_mb:.1f} MB) vorhanden – OK"
                )
            else:
                logger.info(f"scene_mesh.glb erzeugt: {size_mb:.1f} MB")
            # Copy camera export from Windows temp back to viewer directory
            if cameras_win_linux != cameras_txt:
                if cameras_win_linux.exists():
                    shutil.copy2(cameras_win_linux, cameras_txt)
                    cameras_win_linux.unlink()
                    logger.info(f"Kamera-Export nach {cameras_txt.name} kopiert")
                else:
                    logger.warning(
                        "Kamera-Export nicht in Windows-Temp gefunden – "
                        "auto-alignment nicht möglich"
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


def _compress_draco(viewer: Path) -> bool:
    """
    Komprimiert scene_mesh.glb mit Draco-Geometrie-Komprimierung (verlustfrei).

    Draco kodiert Mesh-Geometrie effizienter ohne Qualitätsverlust.
    Typische Reduktion der Geometrie-Daten: 85-90%.
    Texturen bleiben unkomprimiert – nach Draco ist das GLB typischerweise
    60-80 MB (je nach Texturanzahl und -auflösung).

    Benötigt gltf-transform CLI:
        npm install -g @gltf-transform/cli
    """
    input_glb  = viewer / "scene_mesh.glb"
    output_glb = viewer / "scene_mesh_draco.glb"
    if not input_glb.exists():
        return False
    try:
        result = subprocess.run(
            [_GLTF_TRANSFORM_CMD, "draco", str(input_glb), str(output_glb)],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0 and output_glb.exists() and output_glb.stat().st_size > 0:
            mb_before = input_glb.stat().st_size  / 1024 / 1024
            mb_after  = output_glb.stat().st_size / 1024 / 1024
            reduction = (1 - mb_after / mb_before) * 100
            logger.info(f"Draco: {mb_before:.1f} MB → {mb_after:.1f} MB ({reduction:.0f}% kleiner)")
            input_glb.unlink()
            output_glb.rename(input_glb)
            return True
        logger.warning(
            f"Draco-Komprimierung fehlgeschlagen (exitcode={result.returncode}): "
            f"{result.stderr.strip()}"
        )
        return False
    except FileNotFoundError:
        logger.warning(
            "gltf-transform nicht gefunden – Draco-Komprimierung übersprungen. "
            "Installieren mit: npm install -g @gltf-transform/cli"
        )
        return False
    except subprocess.TimeoutExpired:
        logger.error("Draco-Komprimierung: Timeout (>5 min)")
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

    size_mb = glb_path.stat().st_size / 1024 / 1024
    if size_mb > _SUPABASE_MAX_MB:
        logger.warning(
            f"scene_mesh.glb ist {size_mb:.1f} MB – überschreitet Supabase-Limit "
            f"({_SUPABASE_MAX_MB:.0f} MB). Upload wird trotzdem versucht, kann scheitern."
        )

    try:
        with open(glb_path, "rb") as fh:
            storage.upload(
                path=f"{device_id}/scene_mesh.glb",
                file=fh.read(),
                file_options={"content-type": "model/gltf-binary", "upsert": "true"},
            )
        logger.info(f"  ↑  {device_id}/scene_mesh.glb ({size_mb:.1f} MB)")

        # Externe Textur-Sidecars mituploaden (scene_mesh_u0_v0_diffuse.png etc.)
        for png in sorted(viewer.glob("scene_mesh*.png")):
            with open(png, "rb") as fh:
                storage.upload(
                    path=f"{device_id}/{png.name}",
                    file=fh.read(),
                    file_options={"content-type": "image/png", "upsert": "true"},
                )
            logger.info(f"  ↑  {device_id}/{png.name}")

        # Kamera-Posen für automatische Cloud↔Mesh-Ausrichtung im Viewer
        cam_json = viewer / "mesh_cameras.json"
        if cam_json.exists():
            with open(cam_json, "rb") as fh:
                storage.upload(
                    path=f"{device_id}/mesh_cameras.json",
                    file=fh.read(),
                    file_options={"content-type": "application/json", "upsert": "true"},
                )
            logger.info(f"  ↑  {device_id}/mesh_cameras.json")

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

    # ── PFAD A: DA3 (schnell, 6 Frames aus Tilt=90°-Reihe) ──────────────────────────────────────
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

    # ── PFAD B: RealityScan (langsam, alle 27 Frames) ───────────────────
    logger.info("--- PFAD B: RealityScan ---")
    if _run_realityscan(workspace, viewer, realityscan_exe):
        # Kamera-Export parsen und als JSON schreiben (für Auto-Ausrichtung im Viewer)
        cameras = _parse_mesh_cameras_txt(viewer / "mesh_cameras.txt")
        if cameras:
            _write_mesh_cameras_json(cameras, viewer)
        _compress_draco(viewer)
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
    logger.info(f"Draco-Komprimierung: {_GLTF_TRANSFORM_CMD}")
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
