# Servo range and home position (degrees)
PAN_LIMITS   = (0, 180)
TILT_LIMITS  = (70, 120)
PAN_HOME     = 90
TILT_HOME    = 90

# Scan grid
PAN_STEPS    = 9     # 0°…180° in 9 equal steps → 22.5° spacing
TILT_STEPS   = 3     # e.g. 70°, 90°, 110°

# Camera sensor – Raspberry Pi Camera Module 3 Wide (Sony IMX708)
FRAME_WIDTH      = 4608
FRAME_HEIGHT     = 2592
SENSOR_WIDTH_MM  = 6.287
FOCAL_LENGTH_MM  = 2.75   # Wide lens

# Equivalent focal length on 35 mm sensor (for XMP CalibrationPrior)
# formula: f_35mm = f_actual * diag_35mm / sensor_diag
# diag_35mm ≈ 43.27 mm; sensor_diag = sqrt(6.287^2 + 3.537^2) ≈ 7.21 mm
FOCAL_LENGTH_35MM = round(FOCAL_LENGTH_MM * 43.27 / 7.21, 2)  # ≈ 16.5 mm

# Rig geometry
# ARM_LENGTH_M: distance from servo pan-axis to camera optical centre (metres)
ARM_LENGTH_M     = 0.04   # ~4 cm – measure on actual hardware and update

# STEREO_OFFSET_M: signed lateral offset of a second camera along its local +X.
# 0.0 for the test model (single camera).
# Set to ~+0.03 (right cam) / ~-0.03 (left cam) for the pilot model (2 × camera, 6 cm baseline).
STEREO_OFFSET_M  = 0.0
