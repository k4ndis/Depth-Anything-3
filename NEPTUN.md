# NEPTUN: this fork is the DA3 *inference engine* only

NEPTUN uses this repository for exactly one thing: the `da3` CLI + model turning
a handful of scan frames into a `scene.glb`. Everything else — scan status,
downloads, RealityScan, registration, uploads, the dashboard — lives in the
`yolo` repo's stereo pipeline.

## `watcher.py` was removed

It used to poll Supabase, download the Pi's frames, run DA3 *and* RealityScan,
Draco-compress the result and upload it. All of that is now
`neptun/stereo/watch_pc.py` in the `yolo` repo, which already did the same
polling, downloading and uploading for the stereo pipeline — one watcher instead
of two competing ones writing different status columns.

Two things went away with it, on purpose:

- **Its coordinate handling.** DA3 returns a photogrammetry reconstruction in an
  arbitrary frame with no metric tie to the rig, so the old viewer's home-grown
  heading/frustum logic was guessing — which is why that map came up rotated and
  clicked to the wrong place. DA3 is now registered onto the metric stereo cloud
  (`register_mesh`, multi-start ICP) and then rotated by the one
  `RIG_TO_THREEJS` rotation, exactly like the RealityScan map. Orientation and
  click-to-aim are inherited rather than re-derived.
- **Draco/WebP compression and the `gltf-transform` CLI.** They only existed to
  squeeze a 60–80 MB `scene.glb` under Supabase's 50 MB limit. The `.glb` now
  stays on the PC; only a small `da3_webview.ply` (a few MB) is uploaded.

## Running DA3 for NEPTUN

Nothing here needs to be started. Install the `da3` CLI + model on the PC, then
enable the step in the stereo watcher:

```bash
DA3_MAP=1 python -m neptun.stereo.watch_pc
```

`neptun/stereo/da3.py` (in the `yolo` repo) invokes this CLI. It always passes
`--auto-cleanup` (otherwise `da3 auto` prompts when the export directory exists
and would hang an unattended watcher) and `--no-show-cameras` (otherwise the
camera wireframes land in the `.glb` and drag the ICP off).

See `docs/STEREO_PIPELINE.md` in the `yolo` repo for the whole picture.

## Formatierung

`.pre-commit-config.yaml` kommt aus dem Upstream und lief bisher nur bei dem,
der `pre-commit install` ausgefuehrt hatte. `.github/workflows/pre-commit.yml`
fuehrt die Hooks jetzt bei jedem Push aus - allerdings nur auf den Dateien, die
dieser Branch gegenueber `main` aendert. Der geerbte Upstream-Stand ist nicht
formatiert, und ein dauerhaft roter Job prueft am Ende gar nichts mehr.

Lokal derselbe Lauf:

```bash
pip install pre-commit
pre-commit run --from-ref origin/main --to-ref HEAD
```

Nebenwirkung, die man einmal kennen sollte: wer eine Datei anfasst, die vom
Upstream Leerzeichen am Zeilenende mitbringt (`README.md` zum Beispiel), bekommt
von `trim trailing whitespace` die ganze Datei mitkorrigiert. Das ist richtig so,
sieht im Diff aber nach mehr aus, als man geaendert hat - am besten in einen
eigenen Commit legen.
