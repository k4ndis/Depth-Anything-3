// rig-kinematics.js – NEPTUN Rig-Kinematik (Browser-Port von
// neptun/calibration/rig_kinematics.py, Repo yolo).
//
// Reine Mathematik, kein Three.js/DOM. Muss mit der Python-Quelle
// (Source of Truth) in Formeln UND Zahlen synchron gehalten werden – bei
// Änderungen dort auch hier nachziehen. Siehe rig_kinematics.py für die
// vollständige Herleitung (Achsenkonvention, Annahmen zur Rig-Geometrie).
//
// Referenzsystem (Rig-Frame, Meter, Y-up): +Y vertikal (Pan-Achse),
// +Z "vorne" bei Pan=Home/Tilt=Home, +X "rechts". Pan/Tilt=90° = Home.

export const PAN_HOME_DEG = 90.0;
export const TILT_HOME_DEG = 90.0;

const MM_PER_M = 1000.0;

export const RIG_TESTMODELL = {
  panRangeDeg: [0, 180],
  tiltRangeDeg: [70, 120],
  axisOffsetMm: 50,
  cameraHeightMm: 130,
  numCameras: 1,
  stereoBaselineMm: null,
};

export const RIG_PILOTMODELL = {
  panRangeDeg: [0, 360],
  tiltRangeDeg: [45, 135],
  axisOffsetMm: 85,
  cameraHeightMm: 160,
  numCameras: 2,
  stereoBaselineMm: 90,
};

function lateralOffsetM(config, camera) {
  if (camera === 'single') return 0.0;
  if (camera !== 'left' && camera !== 'right') {
    throw new Error(`Unbekannte Kamera: ${camera} (erwartet 'single', 'left' oder 'right')`);
  }
  if (config.stereoBaselineMm == null) {
    throw new Error(`camera=${camera} verlangt Stereo-Baseline, aber config.stereoBaselineMm ist null`);
  }
  const halfM = (config.stereoBaselineMm / MM_PER_M) / 2.0;
  return camera === 'left' ? -halfM : halfM;
}

function normalizePanDeg(panDeg, config) {
  const [lo, hi] = config.panRangeDeg;
  const span = hi - lo;
  if (span < 360.0 - 1e-9) return panDeg;
  return lo + (((panDeg - lo) % 360.0) + 360.0) % 360.0;
}

// direction: Blickrichtung als Einheitsvektor {dx,dy,dz} im Rig-Frame.
function orientationDirection(yawDeg, pitchDeg) {
  const yaw = yawDeg * Math.PI / 180;
  const pitch = pitchDeg * Math.PI / 180;
  return {
    dx: Math.sin(yaw) * Math.cos(pitch),
    dy: Math.sin(pitch),
    dz: Math.cos(yaw) * Math.cos(pitch),
  };
}

/**
 * Position + Blickrichtung einer Kamera im Rig-Referenzsystem.
 * camera: 'single' (RIG_TESTMODELL), 'left' oder 'right' (RIG_PILOTMODELL).
 * Rückgabe: { position: {x,y,z}, orientation: {yawDeg, pitchDeg, dx, dy, dz} }
 */
export function forward(panDeg, tiltDeg, config, camera = 'single') {
  const yawRad = (panDeg - PAN_HOME_DEG) * Math.PI / 180;
  const pitchRad = (tiltDeg - TILT_HOME_DEG) * Math.PI / 180;

  const lateralM = lateralOffsetM(config, camera);
  const offsetM = config.axisOffsetMm / MM_PER_M;
  const heightM = config.cameraHeightMm / MM_PER_M;

  const lx = lateralM, ly = heightM, lz = offsetM;

  const x = lx * Math.cos(yawRad) + lz * Math.sin(yawRad);
  const y = ly;
  const z = -lx * Math.sin(yawRad) + lz * Math.cos(yawRad);

  const yawDeg = yawRad * 180 / Math.PI;
  const pitchDeg = pitchRad * 180 / Math.PI;
  const dir = orientationDirection(yawDeg, pitchDeg);

  return {
    position: { x, y, z },
    orientation: { yawDeg, pitchDeg, dx: dir.dx, dy: dir.dy, dz: dir.dz },
  };
}

/**
 * (panDeg, tiltDeg), um die Referenzkamera auf targetXyz {x,y,z} (Rig-Frame,
 * Meter) auszurichten. Siehe rig_kinematics.py: Fixpunkt-Iteration, da die
 * Kameraposition selbst von panDeg abhängt (axisOffsetMm != 0).
 */
export function inverse(targetXyz, config, { maxIterations = 20, toleranceDeg = 1e-6 } = {}) {
  const { x: tx, y: ty, z: tz } = targetXyz;
  const offsetM = config.axisOffsetMm / MM_PER_M;
  const heightM = config.cameraHeightMm / MM_PER_M;

  let yawRad = Math.atan2(tx, tz);
  for (let i = 0; i < maxIterations; i++) {
    const cx = offsetM * Math.sin(yawRad);
    const cz = offsetM * Math.cos(yawRad);
    const newYawRad = Math.atan2(tx - cx, tz - cz);
    const deltaDeg = Math.abs((newYawRad - yawRad) * 180 / Math.PI);
    yawRad = newYawRad;
    if (deltaDeg < toleranceDeg) break;
  }

  const cx = offsetM * Math.sin(yawRad);
  const cy = heightM;
  const cz = offsetM * Math.cos(yawRad);

  const dx = tx - cx, dy = ty - cy, dz = tz - cz;
  const horizontalDist = Math.hypot(dx, dz);
  if (horizontalDist < 1e-9 && Math.abs(dy) < 1e-9) {
    throw new Error('targetXyz fällt mit der Kameraposition zusammen');
  }
  const pitchRad = Math.atan2(dy, horizontalDist);

  const panDeg = normalizePanDeg(PAN_HOME_DEG + yawRad * 180 / Math.PI, config);
  const tiltDeg = TILT_HOME_DEG + pitchRad * 180 / Math.PI;

  return { panDeg, tiltDeg };
}
