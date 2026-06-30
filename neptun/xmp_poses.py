"""
XMP sidecar generation for RealityScan / RealityCapture pose priors.

Converts NEPTUN servo angles (pan/tilt) into a 3-D camera pose and writes
one XMP file per image.  RealityCapture picks these up automatically when
the XMP files sit in the same folder as the images (-addFolder).

Coordinate convention – servo world frame
------------------------------------------
Origin : servo pan-axis intersection with horizontal plane
+X     : right at pan = 0°
+Y     : up
+Z     : camera forward at home position (pan = PAN_HOME = 90°)

At home (pan = 90°, tilt = 90°) R = I₃, so the camera looks along +Z.

Pan  rotation: around world +Y, positive CCW when viewed from above.
               δpan  = pan_deg  - PAN_HOME
Tilt rotation: around camera local +X after pan, positive = nose up.
               δtilt = tilt_deg - TILT_HOME

Total rotation matrix: R = Ry(δpan) @ Rx(δtilt)

Camera position: ARM_LENGTH_M along the camera's local +Z after rotation
(the lever arm from the servo axis to the optical centre, swung out by pan).

For the pilot model (two cameras, fixed baseline) pass stereo_offset ≠ 0
to displace the camera along its local +X (right direction in world space).
Use xcr:PosePrior = "exact" once the rig geometry is measured precisely;
use "draft" while still characterising the rig.
"""

import math
from pathlib import Path

_DEFAULTS = dict(
    pan_home=90.0,
    tilt_home=90.0,
    arm_length=0.04,
    focal_35mm=16.5,
    stereo_offset=0.0,
    pose_prior="exact",
)


# ── rotation helpers ──────────────────────────────────────────────────────────

def _Ry(deg):
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return [[c, 0, s], [0, 1, 0], [-s, 0, c]]


def _Rx(deg):
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return [[1, 0, 0], [0, c, -s], [0, s, c]]


def _mm(A, B):
    return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)]


def _mv(M, v):
    return [sum(M[i][k] * v[k] for k in range(3)) for i in range(3)]


def _f(v, p=9):
    """Format float: fixed precision, strip trailing zeros."""
    return f"{v:.{p}f}".rstrip("0").rstrip(".")


# ── public API ────────────────────────────────────────────────────────────────

def compute_pose(pan_deg, tilt_deg, *, pan_home=90.0, tilt_home=90.0,
                 arm_length=0.04, stereo_offset=0.0):
    """
    Compute camera pose from servo angles.

    Returns
    -------
    R   : list[list[float]]  3×3 camera-to-world rotation matrix (row-major)
    pos : list[float]        [x, y, z] camera centre in world metres
    """
    R = _mm(_Ry(pan_deg - pan_home), _Rx(tilt_deg - tilt_home))

    # Camera centre: arm_length along camera's local +Z swept into world space
    pos = _mv(R, [0.0, 0.0, arm_length])

    # Lateral stereo offset along camera's local +X (right column of R)
    if stereo_offset:
        pos = [pos[i] + stereo_offset * R[i][0] for i in range(3)]

    return R, pos


def write_xmp_priors(frames, image_dir, *, overwrite=True, **kwargs):
    """
    Write one XMP sidecar per frame into image_dir.

    Parameters
    ----------
    frames : list[dict]
        Each dict must contain 'filename' (basename of the image),
        'pan_deg', and 'tilt_deg'.
    image_dir : str | Path
        Directory that holds the scan images.
        XMP files are written there as '<stem>.xmp'.
    overwrite : bool
        Replace existing XMP files (default True).
    **kwargs
        Override any default: pan_home, tilt_home, arm_length,
        focal_35mm, stereo_offset, pose_prior.

    Returns
    -------
    list[Path]  Paths of the written XMP files.
    """
    cfg = {**_DEFAULTS, **kwargs}
    image_dir = Path(image_dir)
    written = []

    for frame in frames:
        stem = Path(frame["filename"]).stem
        xmp_path = image_dir / f"{stem}.xmp"
        if xmp_path.exists() and not overwrite:
            written.append(xmp_path)
            continue

        R, pos = compute_pose(
            frame["pan_deg"], frame["tilt_deg"],
            pan_home=cfg["pan_home"],
            tilt_home=cfg["tilt_home"],
            arm_length=cfg["arm_length"],
            stereo_offset=cfg["stereo_offset"],
        )
        xmp_path.write_text(_render_xmp(R, pos, cfg["focal_35mm"], cfg["pose_prior"]),
                            encoding="utf-8")
        written.append(xmp_path)

    return written


def _render_xmp(R, pos, focal_35mm, pose_prior):
    r_str = " ".join(_f(R[row][col]) for row in range(3) for col in range(3))
    p_str = " ".join(_f(v) for v in pos)
    f35   = _f(focal_35mm, 4)
    return (
        '<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        '  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '    <rdf:Description rdf:about=""\n'
        '      xmlns:xcr="http://www.capturingreality.com/ns/xcr/1.1#"\n'
        '      xcr:Version="2"\n'
        f'      xcr:PosePrior="{pose_prior}"\n'
        '      xcr:Coordinates="absolute"\n'
        '      xcr:DistortionModel="brown3t2"\n'
        f'      xcr:FocalLength35mm="{f35}"\n'
        '      xcr:Skew="0"\n'
        '      xcr:AspectRatio="1"\n'
        '      xcr:PrincipalPointU="0"\n'
        '      xcr:PrincipalPointV="0"\n'
        '    >\n'
        f'      <xcr:Rotation>{r_str}</xcr:Rotation>\n'
        f'      <xcr:Position>{p_str}</xcr:Position>\n'
        '    </rdf:Description>\n'
        '  </rdf:RDF>\n'
        '</x:xmpmeta>\n'
        '<?xpacket end="w"?>'
    )
