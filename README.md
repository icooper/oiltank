# oil-tank-level

Reads the float gauge on a residential heating-oil tank from an IP camera and publishes the level
to [Home Assistant](https://www.home-assistant.io/) over MQTT, with entities that appear
automatically through MQTT discovery, the way Zigbee2MQTT's do.

> **Status: early development (milestone M1).** Gauge reading works from image files: alignment,
> rectification, the classical reader, calibration mapping, and annotated and diagnostic images. The calibration
> CLI, the go2rtc source and MQTT publishing are next; see [Roadmap](#roadmap). Sections marked
> *planned* describe behaviour that isn't built yet.

## How it works

```mermaid
flowchart TB
    camera["IR camera"] -->|RTSP| go2rtc
    go2rtc -->|"HTTP JPEG, 4K"| oiltank
    subgraph oiltank ["oiltank container"]
        direction LR
        align["Align<br/>undo camera bumps"] --> rectify["Rectify<br/>upright gauge strip"]
        rectify --> find["Find disc<br/>position t"]
        find --> calibrate["Calibrate<br/>t → level"]
        calibrate --> filter["Filter<br/>smooth, refills, usage"]
    end
    oiltank -->|"discovery + state"| broker[("MQTT broker")]
    broker --> ha["Home Assistant"]
```

1. **Fetch.** A burst of frames is pulled from [go2rtc](https://github.com/AlexxIT/go2rtc)'s
   `frame.jpeg` API at the camera's native 4K resolution.
2. **Align.** Each frame is registered against static parts of the scene (the gauge fitting and
   pipework), so a small camera bump doesn't throw the reading off.
3. **Rectify and read.** The region around the gauge axis is warped into an upright 256×128 strip,
   so the rest of the pipeline is independent of camera resolution and tilt. In IR the indicator is
   a bright elliptical disc. An ellipse template of the calibrated disc size, inside a darker ring,
   slides along the strip using masked normalized cross-correlation. That makes it insensitive to
   IR brightness, and the template contains no wall texture from any one photo. The best-scoring
   row gives the disc's position `t`, from 0 (bottom of travel) to 1 (top). Confidence requires
   both a strong match and a clear margin over the next-best position.
4. **Combine and calibrate.** A burst of frames is combined by median. Confidence drops if the
   frames disagree. `t` is mapped to a fill fraction through calibration points you check against
   the gauge by eye, then to gallons using the tank's usable capacity.
5. **Filter and publish** *(planned)*. Low-confidence readings are rejected and accepted ones
   smoothed. Refills are detected and the usage rate estimated. The results go to MQTT with Home
   Assistant discovery.

Classical computer vision runs on the CPU. The reader sits behind an interface, so a learned ONNX
model, running on CPU or an NVIDIA GPU, can replace it through config later.

## Requirements

- A **fixed camera** with a clear view of the gauge dome. IR night vision is fine and expected.
- **go2rtc** re-serving the camera's main stream, so the service can fetch single frames over HTTP:
  ```yaml
  # go2rtc.yaml
  streams:
    oil_tank: rtsp://user:pass@camera-ip:554/main   # your camera's main-stream URL
  ```
  Frames are then available at `http://<go2rtc-host>:1984/api/frame.jpeg?src=oil_tank`.
- **Enough resolution.** The indicator disc's travel, from E to F, should span at least ~40 px,
  ideally ≥100 px. Readings below 40 px are flagged `low_resolution`. A low-resolution snapshot or
  substream isn't enough:

  | Frame size | Disc travel | Gauge per pixel |
  |---|---|---|
  | 3840×2160 (4K main stream) | ~125 px | ~0.8% (~2 gal of 260) |
  | 1920×1080 | ~62 px | ~1.6% |
  | 1280×720 | ~42 px | ~2.4% |
  | 640×360 (typical snapshot/substream) | ~21 px | ~4.8% |

- A **Docker** host, an **MQTT broker**, and Home Assistant's
  [MQTT integration](https://www.home-assistant.io/integrations/mqtt/).

## Quick start

`docker-compose.yml` already has the hardened deployment settings: non-root, read-only root
filesystem, no capabilities, `./config` mounted read-only and `./data` for state. The long-running
`run` command, its healthcheck and `config.example.yaml` arrive in milestone M5. Setup will be:

```sh
mkdir -p config data && sudo chown 65532:65532 data
cp config.example.yaml config/config.yaml        # then edit (M5)
echo 'OILTANK__MQTT__PASSWORD=...' > .env          # secrets stay out of config.yaml
docker compose up -d --build
```

For now you can build the image and read the sample frames (repeat `--image` for a burst):

```sh
docker build -t oiltank:dev .
docker run --rm -v "$PWD/samples:/samples:ro" oiltank:dev \
  read -c /samples/calibration.yaml --image /samples/frame0.jpg --image /samples/frame1.jpg
# level 98.0%  (t=0.820, confidence 1.00, indicator at x=2168.0 y=1047.3, 2 frame(s))
```

`read` also takes `--json` for a machine-readable result, `--annotate out.jpg` to write the
compact gauge image that Home Assistant will show, and `--diagnostic check.jpg` (or `.png`) to
write a larger image for [checking the calibration](#checking-a-calibration). It exits 0 for a
good reading, 1 if there is no trustworthy reading, and 2 for configuration errors such as a
missing calibration or a frame-size mismatch.

## Configuration

*Planned.* Configuration will be a YAML file mounted read-only at `/config/config.yaml`. Any key
can be overridden with an environment variable of the form `OILTANK__SECTION__KEY`, for example
`OILTANK__MQTT__PASSWORD`. The full reference will go here.

## Calibration

A calibration is a YAML file plus a **reference frame** that the alignment anchor is cut from. The
reference path is relative to the YAML file. `samples/calibration.yaml` is a complete example for
the sample frames:

```yaml
version: 1
reference_image: frame0.jpg      # relative to this file
frame_size: [3840, 2160]         # frames of any other size are rejected
axis:                            # the disc centre's path, in frame px
  top: [2166.0, 1020.0]          # top of travel (t = 1): disc against the dome cap
  bottom: [2166.0, 1172.0]       # bottom of travel (t = 0): disc on the base ring
  half_width: 50.0               # strip half-width across the axis
  margin: 0.25                   # strip extends this fraction of the axis past each end
disc: {width: 64.0, height: 38.0}   # apparent disc size, frame px
anchor:
  rect: [1950, 1185, 290, 165]   # x, y, w, h of static scene (the gauge fitting). Never the tube!
  search: 48                     # px the camera may shift before the anchor is lost
  min_score: 0.6
  recalibrate_shift: 20.0        # suggest recalibrating beyond this shift
points:                          # disc position -> gauge reading (0 = E, 1 = F)
  - {t: 0.0, fraction: 0.0, note: "default: bottom of travel = E"}
  - {t: 0.820, fraction: 0.98, note: "2026-10-13: owner reports ~98%, a week after a fill"}
```

The axis bounds where the disc can physically go, and so where the reader looks. `points` set the
scale. A new calibration starts with two defaults: the bottom of travel is E (`t: 0`) and the top
is F (`t: 1`). Replace or add to them with readings you know, from the gauge read by eye or from
a delivery. Points must increase in both position and `fraction`. Between points the mapping is
linear. Past the outermost points the end segments are extended, so one known reading near full
plus the default E is enough to place F.

A point's position can be given two ways:

- `t`, the position along the axis (`read` prints it). A `t` point moves if you later edit the
  axis endpoints.
- `at`, the disc centre in frame pixels, copied from `read`'s `indicator at x=… y=…`. For
  example, `{at: [2168.0, 1047.3], fraction: 0.98}`. An `at` point stays on the disc however the
  axis changes, so prefer it when you're adjusting the axis by hand.

The sample calibration shows why the defaults are only a starting point. The frames were taken a
week after a fill, with the tank at about 98%, yet the disc sat at `t ≈ 0.82`, well below the top
of the dome. With that reading in place of the "top = F" default, F lands at `t ≈ 0.84`, about
y = 1045. Assuming the dome cap was F would have read 82%.

### Checking a calibration

`--diagnostic` draws the calibration over a real frame:

- **cyan disc outlines** where the calibration puts 0% (E) and 100% (F), with 25/50/75% marks
  between;
- **white dots** at the calibration points;
- **the detected disc**, in green (amber or red when confidence is low);
- **the grey box** the reader searches, which must contain both outlines;
- **the magenta anchor** used to detect camera shifts, with its match score and the shift found.

```sh
uv run oiltank read -c samples/calibration.yaml --image samples/frame0.jpg --diagnostic check.jpg
```

Everything is drawn after correcting for camera shift, so it should line up with the gauge in
the frame. If an outline doesn't sit where the disc would rest when the tank is empty or full,
add a point you've checked.

*Planned (milestone M2):* `oiltank calibrate` commands to create and edit this file from a live
frame, including adding a point at the current reading. Later, `oiltank capture` will record the
disc across its whole range during an oil delivery.

## Home Assistant

*Planned (milestones M4–M5).* One device, **Oil Tank**, with these entities:

- level (%) and volume (gal)
- usage rate (gal/day) and days left
- last fill time and last fill amount
- an annotated gauge image
- a **Read now** button
- a problem flag
- diagnostic confidence and last-reading entities

The MQTT topic reference and example automations will go here.

## Accuracy and limitations

Expect about **±2–3%** at 4K. Accuracy is limited more by the camera's noise reduction, JPEG
compression and the textured wall visible through the clear dome than by resolution. The gauge
mechanism isn't perfectly linear, which is why calibration uses points you've checked by eye
rather than assuming the top of the dome is "full".

## Troubleshooting

*Planned.* This will cover the `problem` reasons (`camera_unreachable`, `camera_moved`,
`low_confidence`, resolution mismatch) and how to check discovery with `mosquitto_sub`.

## Development

The project uses [uv](https://docs.astral.sh/uv/) and Python 3.14.

```sh
uv sync                                       # create .venv from uv.lock
uv run pytest                                 # tests (integration tests excluded)
uv run pytest -m integration                  # integration tests (need Docker)
uv run ruff format . && uv run ruff check .   # format + lint
uv run mypy                                   # strict type checking
uv run oiltank --help
```

Sample frames for tests and development are in `samples/`. They are native 4K go2rtc captures,
taken a week after a fill with the tank at about 98%. The disc's centre is at y≈1048 in all six.
The tests cover:

- `tests/unit/`: calibration, geometry, peak finding, annotation and CLI;
- `tests/cv/test_golden.py`: the six real frames, one by one and as a burst;
- `tests/cv/test_synthetic.py`: the disc moved along its travel or removed; noise, JPEG, blur and
  gain/gamma; camera nudges and large moves; 1280×720 and 640×360 frames.

```sh
uv run oiltank read -c samples/calibration.yaml --image samples/frame0.jpg \
  --annotate gauge.jpg --diagnostic check.jpg
```

## Container and security

The image is built in two stages. A uv builder installs a uv-managed CPython 3.14 and a
non-editable virtualenv, which are copied into
[`gcr.io/distroless/cc-debian12:nonroot`](https://github.com/GoogleContainerTools/distroless):

- no shell or package manager in the runtime image;
- runs as uid/gid **65532**;
- works with a **read-only root filesystem** and **all capabilities dropped**:
  ```sh
  docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges oiltank:dev --version
  ```
- zlib is the only system library added on top of distroless, because the numpy and OpenCV wheels
  link against it.

## Roadmap

- [x] **M0** Project skeleton, tooling, distroless image
- [x] **M1** Geometry, calibration model, classical reader, annotated output
- [ ] **M2** Calibration CLI
- [ ] **M3** go2rtc HTTP frame source
- [ ] **M4** MQTT discovery, state and availability
- [ ] **M5** Service loop: filtering, history, refill detection, usage rate, Read now button,
      gauge image, healthcheck
- [ ] **M6** Frame archive and delivery `capture` mode
- [ ] **M7** Documentation pass
- [ ] *Later:* ONNX reader with GPU (CUDA) image, model trained on archived frames, tank volume
      chart

## License

[MIT](LICENSE)
