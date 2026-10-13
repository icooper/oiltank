# oil-tank-level

Reads the float gauge on a residential heating-oil tank from an IP camera and publishes the level
to [Home Assistant](https://www.home-assistant.io/) over MQTT, with entities that appear
automatically through MQTT discovery, the way Zigbee2MQTT's do.

> **Status: early development (milestone M3).** `oiltank read` reads the gauge from go2rtc or
> from image files, and the `oiltank calibrate` commands create, check and adjust a calibration.
> MQTT publishing is next; see [Roadmap](#roadmap). Sections marked *planned* describe behaviour
> that isn't built yet.

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
filesystem, no capabilities, and `./data` for state. All configuration comes from `.env`. The
long-running `run` command and its healthcheck arrive in milestone M5. For now:

```sh
mkdir -p data && sudo chown 65532:65532 data
cp .env.example .env                       # then fill in the camera URL, MQTT broker, ...
docker compose build
alias oiltank='docker compose run --rm oiltank'
oiltank grab --out /data/frame.jpg         # check the camera: a full-size frame in ./data
```

Then [calibrate](#calibration), and read the gauge:

```sh
oiltank read
# fetching 5 frame(s) from go2rtc.lan:1984, 2 s apart
# level 97.8%  (t=0.817, confidence 1.00, indicator at x=2169.0 y=1047.9, 5 frame(s))
```

You can also try the sample frames without a camera or `.env`:

```sh
docker build -t oiltank:dev .
docker run --rm -v "$PWD/samples:/samples:ro" oiltank:dev \
  read -c /samples/calibration.yaml --image /samples/frame0.jpg --image /samples/frame1.jpg
# level 98.1%  (t=0.820, confidence 1.00, indicator at x=2168.0 y=1047.3, 2 frame(s))
```

`read` fetches a burst of frames from the camera, or reads image files given with `--image`
(repeat it for a burst). It exits 0 for a good reading, 1 if there is no trustworthy reading or
the camera can't be reached, and 2 for configuration errors such as a missing calibration, a
frame-size mismatch or a wrong camera URL. Its other options:

| Option | Meaning |
|---|---|
| `--url URL` | Fetch frames from this URL instead of `OILTANK_CAMERA_URL`. |
| `--frames N` | Frames per burst. |
| `--spacing S` | Seconds between frames. |
| `--json` | Print a machine-readable result. |
| `--annotate out.jpg` | Write the compact gauge image that Home Assistant will show. |
| `--diagnostic check.jpg` | Write a larger image for [checking the calibration](#checking-a-calibration). |

`oiltank grab --out PATH [--frames N]` saves frames exactly as the camera sent them. That's useful
for calibration, for test samples, or for checking what go2rtc returns.

## Configuration

All settings come from environment variables named `OILTANK_<SECTION>_<KEY>`, normally kept in
`.env`. Copy `.env.example`, which lists every setting with its default. `.env` is git-ignored, and
Docker Compose passes it to the container whole.

Variables set in your shell win over `.env`, and `.env` wins over the defaults. An empty value
means "not set". An `OILTANK_` name that matches no setting is an error, which catches typos.

| Setting | Default | Meaning |
|---|---|---|
| `OILTANK_CAMERA_URL` | none | An URL that returns one full-size JPEG per request. For go2rtc: `http://<host>:1984/api/frame.jpeg?src=<stream>`. |
| `OILTANK_CAMERA_USERNAME`, `_PASSWORD` | none | Basic auth, if go2rtc's `api:` section sets it. Never put these in the URL. |
| `OILTANK_CAMERA_BURST_FRAMES` | 5 | Frames per reading, combined by median. |
| `OILTANK_CAMERA_BURST_SPACING_S` | 2.0 | Seconds between frames. Use at least the camera's keyframe interval: duplicate frames are dropped and refetched. |
| `OILTANK_CAMERA_TIMEOUT_S` | 15 | Per request. |
| `OILTANK_CAMERA_RETRIES`, `_RETRY_BACKOFF_S` | 3, 2.0 | Retries for timeouts, connection errors and HTTP 5xx. The wait doubles each time. |
| `OILTANK_CAMERA_VERIFY_TLS` | true | For `https://` URLs. |
| `OILTANK_MQTT_HOST`, `_PORT` | none, 1883 | Home Assistant's MQTT broker (M4). |
| `OILTANK_MQTT_USERNAME`, `_PASSWORD`, `_TLS` | none, none, false | |
| `OILTANK_MQTT_CLIENT_ID`, `_TOPIC_PREFIX`, `_DISCOVERY_PREFIX` | oiltank, oiltank, homeassistant | |
| `OILTANK_DEVICE_ID`, `_NAME` | oil_tank, Oil Tank | How the tank appears in Home Assistant (M4). |
| `OILTANK_TANK_CAPACITY`, `_UNIT` | 260, gal | Usable capacity when the gauge reads F (M5). |
| `OILTANK_TANK_RESERVE` | 0 | "Days left" counts down to this volume (M5). |
| `OILTANK_SERVICE_INTERVAL_MIN`, `_MIN_CONFIDENCE` | 15, 0.5 | Reading interval and acceptance threshold (M5). |
| `OILTANK_DATA_DIR` | data | Calibration, history and archive. Relative to the working directory, which is `/` in the container, so `data` means `./data` from a checkout and `/data` (the same directory, mounted) in the container. |
| `OILTANK_LOG_LEVEL` | INFO | DEBUG, INFO, WARNING or ERROR. `oiltank -v` logs at DEBUG. |
| `TZ` | | The container's timezone, used for timestamps on the gauge image. |

Paths you pass to commands inside the container are relative to `/`, whose only writable directory
is `/data`. So write outputs as `/data/...`.

## Calibration

A calibration tells the reader where the gauge is in the frame and how the disc's position maps to
a level. It is a YAML file plus a **reference frame**, the image the alignment anchor is cut from.
In the container it lives at `/data/calibration.yaml`, which is also the default for `-c`. The
`oiltank calibrate` commands create and adjust it:

| Command | What it does |
|---|---|
| `calibrate grid` | Draws labelled coordinate gridlines on a frame, for reading off pixel positions. |
| `calibrate init` | Creates a calibration from one frame and the two ends of the disc's travel. |
| `calibrate show` | Prints a report, checks the calibration, reads the gauge, and can write the diagnostic image. |
| `calibrate point` | Records what the gauge reads now, from a reading you trust. |
| `calibrate axis` / `disc` / `anchor` | Adjusts one part of the calibration. |
| `calibrate rebase` | Follows the camera after it has been bumped. |

Commands that change the calibration print the same report as `show` for the result, then save it.
The previous file is kept as `calibration.yaml.bak`, and `--dry-run` shows the result without
saving. The file is safe to edit by hand. It is validated on load, and the commands keep its header
comment when they rewrite it.

### Setting up

The commands below run in the container, where `./data` is mounted at `/data` and
`/data/calibration.yaml` is the default calibration. An alias saves typing:

```sh
alias oiltank='docker compose run --rm oiltank'
```

From a checkout, `uv run oiltank ...` uses the same `./data`, with local paths such as
`data/frame.jpg`.

1. **Save a full-size frame** from the camera into `./data`:

   ```sh
   oiltank grab --out /data/frame.jpg
   ```

2. **Find the ends of the disc's travel.** Draw a coordinate grid over the frame, then zoom in on
   the gauge. The images land in `./data`:

   ```sh
   oiltank calibrate grid --image /data/frame.jpg --out /data/grid.jpg
   oiltank calibrate grid --image /data/frame.jpg --out /data/zoom.jpg --region 2050 950 250 300
   ```

   The **top** is the disc's centre where it would touch the dome cap. The **bottom** is the
   disc's centre where it would rest on the base ring. These ends bound where the reader looks
   for the disc. They don't need to be E and F.

3. **Create the calibration.**

   ```sh
   oiltank calibrate init --image /data/frame.jpg --top 2166 1020 --bottom 2166 1172
   ```

   `init` then:
   - scans for the disc size (see [Disc size](#disc-size));
   - picks an alignment anchor just below the gauge and self-tests it;
   - copies the frame next to the calibration as the reference;
   - prints the report.

   `--disc W H`, `--anchor X Y W H` and `--half-width` override the defaults. `--camera`
   takes the frame straight from the camera instead of `--image`.

4. **Check it.** Run `oiltank calibrate show --camera --diagnostic /data/check.jpg` and look at
   the image (see [Checking a calibration](#checking-a-calibration)).

5. **Tell it what the gauge reads.** Do this right away if you know the level, or later when you
   read the gauge by eye or get a delivery ticket:

   ```sh
   oiltank calibrate point --camera --fraction 0.98 --note "a week after a fill"
   ```

   This measures where the disc is now and stores that position with the reading. See
   [Points](#points-the-scale).

The commands that measure (`show`, `point`, `axis`, `disc` and `anchor`) use the reference frame
by default, `--image` files if given, or a burst from the camera with `--camera`. `init`, `rebase`
and `grid` take one frame, from `--image` or `--camera`.

### The report

`calibrate show` on the sample calibration and all ten sample frames:

```
calibration  samples/calibration.yaml
reference    frame0.jpg  (3840x2160)
axis         top (2166.0, 1020.0) to bottom (2166.0, 1172.0): 152.0 px, half-width 50, margin 0.25
disc         64 x 38 px; the reading holds for widths 45-72 and heights 33.8-54.1
anchor       rect [1950, 1185, 290, 165], search 48 px; contrast 67, self-test error 0.04 px
scale        E (0%) at (2166.0, 1172.0) t=0.000; F (100%) at (2166.0, 1045.0) t=0.836
             127 px from E to F, 0.79% per px
points       98.0% at (2168.0, 1047.5)  2026-10-13: owner reports ~98%, a week after a fill
             E is the bottom of travel (default until there's a second point)
reading      98.0% (t=0.819, confidence 1.00), disc at (2168.0, 1047.5), 10 frame(s)
note: other disc sizes find something at y=1035 (16 sizes) or y=1040 (4 sizes); ...
```

Problems appear as `warning:` lines. They include:
- E or F lying outside the area the reader searches;
- too few pixels between E and F;
- a strip barely wider than the disc;
- an anchor that is featureless, fails its self-test, or overlaps the disc's travel;
- a disc size on the edge of the range that reads consistently.

### Checking a calibration

`calibrate show --diagnostic check.jpg` (or `read --diagnostic`) draws the calibration over a
real frame:

- **cyan disc outlines** where the calibration puts 0% (E) and 100% (F), with 25/50/75% marks
  between;
- **white dots** at the calibration points;
- **the detected disc**, in green (amber or red when confidence is low);
- **the grey box** the reader searches, which must contain both outlines;
- **the magenta anchor** used to detect camera shifts, with its match score and the shift found.

Everything is drawn after correcting for camera shift, so it should line up with the gauge in the
frame. Check two things:
- The green outline should sit on the disc. If it doesn't, see [Disc size](#disc-size).
- The cyan outlines should sit where the disc would be at empty and full. If they don't, record
  a point.

### Points (the scale)

The axis bounds where the disc can go. `points` set the scale: each pairs a disc position with a
gauge reading (`fraction`: 0 = E, 1 = F). Defaults fill in until there are two points:

- with no points, E is the bottom of travel and F the top;
- with one point, it's paired with whichever of those is further from it in reading. One reading
  near full plus E at the bottom of travel is enough to place F.

Between points the mapping is linear, and past the outermost points the end segments are
extended. Points must increase in both position and reading. `calibrate point` replaces a point
with the same reading, and `calibrate point --fraction F --remove` deletes one.

The sample calibration shows why the defaults are only a starting point. The frames were taken a
week after a fill, with the tank at about 98%, yet the disc sat well below the top of the dome.
With that reading, F lands at y≈1045. Assuming the dome cap was F would have read 82%.

A point's position is stored one of two ways:

- `at`: the disc centre in reference-frame pixels. `calibrate point` measures and writes this. An
  `at` point stays on the disc however you later move the axis.
- `t`: the position along the axis, from 0 at the bottom of travel to 1 at the top. Write it by
  hand, or with `calibrate point --t`. A `t` point moves when you edit the axis endpoints.

### Disc size

The reader slides an ellipse of the calibrated disc size along the gauge, and **the size
matters**. On the sample frames, a size 12–25% too small locks onto a highlight about 12 px above
the disc, still with full confidence, and reads about 10% high.

`calibrate disc --scan` reads the frames with sizes from 0.6× to 1.6× the current one. It follows
the sizes that agree with the current size about where the disc is, and picks the size deepest
inside that range. It prints the table it used: `*` marks agreement, `#` the chosen size, and `?`
low confidence. `init` runs the same scan from a starting guess. `calibrate disc --size W H` sets
the size directly. The report shows how far the size can be off before the reading moves, and
notes any other position that several sizes agree on. That position is something you don't want
the reader to find, so check in the diagnostic image which one the green outline is on.

### After the camera moves

Readings tolerate small bumps: the anchor is found within `anchor.search` px, 48 for the sample.
Past `recalibrate_shift` (20 px), readings carry a `recalibrate_suggested` notice. If the camera
moves too far, readings fail with `camera_moved`.

```sh
oiltank calibrate rebase --camera
```

`rebase` finds the anchor in a new frame (from the camera, or `--image`), searching 10% of the
frame width by default. It needs a
strong match (0.9 or better), then moves the axis, anchor and `at` points by the same shift, and
makes the new frame the reference. It handles shifts only. If the zoom or angle has changed, start
again with `init`.

### The file

```yaml
version: 1
reference_image: frame0.jpg      # relative to this file
frame_size: [3840, 2160]         # frames of any other size are rejected
axis:                            # the disc centre's travel, in frame px
  top: [2166.0, 1020.0]          # top of travel (t = 1): disc against the dome cap
  bottom: [2166.0, 1172.0]       # bottom of travel (t = 0): disc on the base ring
  half_width: 50.0               # strip half-width across the axis
  margin: 0.25                   # strip extends this fraction of the axis past each end
disc:                            # apparent disc size, frame px
  width: 64.0
  height: 38.0
anchor:
  rect: [1950, 1185, 290, 165]   # x, y, w, h of static scene (the gauge fitting). Never the tube!
  search: 48                     # px the camera may shift before the anchor is lost
  min_score: 0.6                 # weakest acceptable anchor match
  recalibrate_shift: 20.0        # suggest `calibrate rebase` beyond this shift
points:                          # disc position -> gauge reading (0 = E, 1 = F)
  - {at: [2168.0, 1047.5], fraction: 0.98, note: '2026-10-13: owner reports ~98%, a week after a fill'}
```

*Later (milestone M6):* `oiltank capture` will record the disc across its whole range during an
oil delivery, which is the best way to collect points along the whole scale.

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

Camera problems, from `read`, `grab` or `--camera`:

| Message | Cause |
|---|---|
| `refused the request (HTTP 401)` | go2rtc's API wants a login. Set `OILTANK_CAMERA_USERNAME` and `OILTANK_CAMERA_PASSWORD`. |
| `not found (HTTP 404) (is there a go2rtc stream called ...?)` | The `src=` name in the URL doesn't match a stream in go2rtc's config. |
| `expected an image ..., got text/html` | The URL isn't go2rtc's `/api/frame.jpeg` endpoint. |
| `camera unreachable: cannot reach ...` / `didn't answer within ...` | go2rtc is down or the host name doesn't resolve, even after retries. |
| `answered HTTP 500: ...` | go2rtc couldn't get a frame from the camera. go2rtc's message follows. |
| `frame is 640x360 but the calibration was made at 3840x2160` | The URL serves a substream or a snapshot rather than the main stream. |
| `unknown setting(s) ...` | A misspelt `OILTANK_` name in `.env` or the shell. |

*Planned:* the `problem` reasons Home Assistant will show (`camera_unreachable`, `camera_moved`,
`low_confidence`), and how to check discovery with `mosquitto_sub`.

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
taken a week after a fill with the tank at about 98%. `frame0`–`frame5` are from the late
afternoon, with daylight through the basement window. `frame6`–`frame9` are from late evening,
with IR only. The disc's centre is at y≈1048 in all of them. The tests cover:

- `tests/unit/`: calibration, geometry, peak finding, annotation, and the `read` and `calibrate`
  commands;
- `tests/cv/test_golden.py`: the real frames one by one, and day, night and mixed bursts;
- `tests/cv/test_synthetic.py`: the disc moved along its travel or removed; noise, JPEG, blur and
  gain/gamma; camera nudges and large moves; 1280×720 and 640×360 frames;
- `tests/cv/test_calibrate.py`: the calibration tools (disc-size scan, anchor check, rebase) on
  the real frames;
- `tests/unit/test_http_source.py` and `test_cli_camera.py`: the go2rtc source against a mocked
  transport, and the camera commands against a fake go2rtc on localhost (auth, retries, timeouts,
  wrong stream, duplicates);
- `tests/unit/test_config.py`: settings, `.env` and unknown-name detection.

Tests never read your `.env` or `OILTANK_*` variables: they run in an empty working directory with
those variables removed.

New sample frames have to be listed as day or night in `tests/conftest.py`; a test fails until they
are.

```sh
uv run oiltank read -c samples/calibration.yaml --image samples/frame0.jpg \
  --annotate gauge.jpg --diagnostic check.jpg
uv run oiltank calibrate show -c samples/calibration.yaml --diagnostic check.jpg
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
- [x] **M2** Calibration CLI
- [x] **M3** go2rtc HTTP frame source
- [ ] **M4** MQTT discovery, state and availability
- [ ] **M5** Service loop: filtering, history, refill detection, usage rate, Read now button,
      gauge image, healthcheck
- [ ] **M6** Frame archive and delivery `capture` mode
- [ ] **M7** Documentation pass
- [ ] *Later:* ONNX reader with GPU (CUDA) image, model trained on archived frames, tank volume
      chart

## License

[MIT](LICENSE.md)
