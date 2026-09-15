"""Where a reactive press lands on Merciless Storm — the wiggle method, not a proxy.

    .venv/bin/python tools/measure_storm_landing.py recordings_video/merciless-storm/events.json 43 60 75

Storm draws an unfilled outline with no solid Great band, so `find_zone` returns None and
the tracker abstains; `autorun.py` then fires REACTIVELY on the classifier's cue. The
question this answers is where that press lands, and it is scored the only way that has
survived on this project: cue frame + LOCAL rate x round trip, against the zone actually
drawn. A recorded window's endpoint is not a landing — scoring one produced the false "33%
of wiggle presses fail" figure, 200-250 ms after the cue against a 43 ms trip.

Two traps, both of which silently produce plausible numbers:

  * **The box moves between revolutions and the ingested windows OVERLAP.** A median over
    a whole window blends two boxes and keeps neither: read that way, 6 of 17 checks show
    no zone at all and the rest read 3-38 deg for what is plainly one consistent box. The
    static is therefore taken from the eight frames before the cue — which is also what
    the live tracker would have had.
  * **`events.json`'s `size` is the INGEST TARGET, not the source resolution.**
    `merciless-storm.mp4` is 1280x720 and the file says 1920x1080 because `ingest_video.py`
    scales every clip to 1080p first (its own line 12). The geometry in that file is in
    scaled coordinates and is internally consistent — fps and frame count match the source
    exactly. Nothing here reads the crop from that field; the resolution comes from the
    decoder.

The zone is measured by EXTENT only — the longest angular run of lit radial thickness —
because Storm has no Great tier to find.
"""

import json, os, sys
import numpy as np, cv2
sys.path.insert(0, '/Users/nicojan/dev/dbd_autoSkillCheck')
from dbd.AI_model import AI_model
from dbd.utils.monitoring_window import TRAINING_REFERENCE_CROP, TRAINING_REFERENCE_HEIGHT
from dbd.utils import needle_tracker as nt
from tools.replay_centre_crop import centre_crop, CROP, RING_ABOVE_CENTRE_PX

ev = json.load(open(sys.argv[1]))
trips = [float(x) for x in sys.argv[2:]] or [nt.ROUND_TRIP_MS]
cap = cv2.VideoCapture(ev['video'])
h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)); fps = ev['fps']
scale = h / TRAINING_REFERENCE_HEIGHT
side = max(8, int(round(TRAINING_REFERENCE_CROP * scale)))
offset_y = int(round(RING_ABOVE_CENTRE_PX * scale))
model = AI_model('models/model.onnx', use_gpu=False)
print(f"{ev['video']} {h}p @{fps:.2f}  crop {side}->{CROP}  shift {offset_y:+d}\n")

def zone_extent(static, centre, ring_r):
    """The drawn outline's angular run — extent only; Storm draws no solid band."""
    radii = np.arange(ring_r + nt.WINDOW_IN, ring_r + nt.WINDOW_OUT, nt.RADIUS_STEP)
    angles, polar = nt.sample_rays(static, centre[0], centre[1], radii)
    resid = polar - np.median(polar, axis=0, keepdims=True)
    thick = (resid > nt.HOT).sum(axis=1) * nt.RADIUS_STEP
    run = nt._longest_run(nt._close_gaps(thick >= nt.ZONE_THICK_PX, 1.0), 1.0)
    if run is None:
        return None
    return float(angles[run[0]]), float(run[1])

rows = []
for check in ev['checks']:
    cap.set(cv2.CAP_PROP_POS_FRAMES, check['frame0'])
    crops, ts, descs, hits = [], [], [], []
    for i in range(check['frame0'], check['frame1'] + 1):
        ok, bgr = cap.read()
        if not ok: break
        crop = centre_crop(bgr, side, offset_y)
        pred, desc, _, should_hit = model.predict(crop[:, :, ::-1])
        crops.append(crop); ts.append((i - check['frame0']) / fps * 1000.0)
        descs.append(desc); hits.append(bool(should_hit))
    if not crops: continue
    cue = next((i for i, hp in enumerate(hits) if hp), None)
    # The box moves between revolutions and the windows OVERLAP, so a median over the
    # whole window blends two boxes and keeps neither. Read it off the frames the live
    # tracker would have had: the eight immediately before the cue.
    near = crops[max(0, (cue or 0) - 7):(cue or 0) + 1] or crops
    static = nt.static_image(near)
    cx, cy, ring_r, peak = nt.refine_centre(static, prior=nt.CENTRE_PRIOR)
    ext = zone_extent(static, (cx, cy), ring_r)
    angles = np.array([nt.needle_angle(c, (cx, cy))[0] for c in crops])
    un = np.rad2deg(np.unwrap(np.deg2rad(angles)))
    if cue is None or ext is None:
        print(f"{check['dir']}: cue={cue} zone={ext}"); continue
    j0, j1 = max(0, cue - 1), min(len(ts) - 1, cue + 1)
    rate = (un[j1] - un[j0]) / (ts[j1] - ts[j0])          # deg/ms, local
    zs, zw = ext
    out = []
    for trip in trips:
        land = (un[cue] + rate * trip) % 360.0
        into = (land - zs) % 360.0
        out.append((trip, land, into, into <= zw))
        rows.append((check['dir'], trip, into, zw, into <= zw))
    print(f"{check['dir']}  zone {zs:5.1f} +{zw:4.1f} deg  cue frame {cue:3d} ({descs[cue]}) "
          f"at {un[cue] % 360:6.1f} deg, rate {rate * 1000:+6.1f} deg/s")
    for trip, land, into, ok_ in out:
        print(f"     trip {trip:4.0f} ms -> lands {land:6.1f} deg, {into:6.1f} into the zone "
              f"({'IN' if ok_ else 'OUT'})")

print()
for trip in trips:
    sel = [r for r in rows if r[1] == trip]
    if not sel: continue
    hit = sum(1 for r in sel if r[4])
    print(f"trip {trip:4.0f} ms: {hit}/{len(sel)} land inside the drawn zone, "
          f"median {np.median([r[2] for r in sel]):+.1f} deg into a "
          f"{np.median([r[3] for r in sel]):.0f} deg zone")
cap.release()
