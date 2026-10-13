# oiltank <!-- omit from toc -->

Reads the float gauge on a residential heating-oil tank from an IP camera and publishes the level
to [Home Assistant](https://www.home-assistant.io/) over MQTT, with entities that appear
automatically through MQTT discovery, the way Zigbee2MQTT's do.

<img src="docs/images/gauge.jpg" alt="The annotated gauge image the service publishes: 98%, 245 gal" align="right" width="230">

It runs as a small, locked-down container alongside go2rtc. Every 15 minutes it takes a burst of
4K frames, finds the indicator disc in the gauge's clear dome, and publishes the level. It also
publishes the volume, the usage rate and days left, refills, an annotated gauge image, a
**Read now** button, and a problem flag. It keeps a history and an archive of what it saw, which
is enough to train a model later, and it can record an oil delivery frame by frame.

Images are published for amd64 and arm64, so it also runs on a Raspberry Pi. To build it yourself,
run the tests or make changes, see [CONTRIBUTING.md](CONTRIBUTING.md).

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [The service](#the-service)
  - [Frame archive](#frame-archive)
  - [Recording a delivery](#recording-a-delivery)
- [Web UI](#web-ui)
  - [In Home Assistant](#in-home-assistant)
- [Configuration](#configuration)
- [Calibration](#calibration)
  - [Setting up](#setting-up)
  - [The report](#the-report)
  - [Checking a calibration](#checking-a-calibration)
  - [Points (the scale)](#points-the-scale)
  - [Disc size](#disc-size)
  - [After the camera moves](#after-the-camera-moves)
  - [The file](#the-file)
- [Home Assistant](#home-assistant)
  - [Publishing by hand](#publishing-by-hand)
  - [Topics](#topics)
  - [Automations](#automations)
  - [Dashboard card](#dashboard-card)
- [Accuracy and limitations](#accuracy-and-limitations)
- [Troubleshooting](#troubleshooting)
- [Updating](#updating)
- [Container and security](#container-and-security)
- [License](#license)

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
    oiltank -->|"discovery, state, image"| broker[("MQTT broker")]
    broker -->|"Read now"| oiltank
    broker --> ha["Home Assistant"]
    oiltank --> data[("./data<br/>calibration, history,<br/>archive")]
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
5. **Filter and publish.** Low-confidence readings are rejected and accepted ones smoothed.
   Refills are detected and the usage rate estimated. The results go to MQTT with Home Assistant
   discovery, together with the annotated gauge image shown above.
6. **Remember.** Every reading goes into a SQLite history, and what it saw into a size-capped
   archive. On restart, or after a calibration change, the service rebuilds its state from the
   history.

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

- A **Docker** host on amd64 or arm64, such as a PC or a Raspberry Pi 4 or 5 running a 64-bit OS;
  an **MQTT broker**; and Home Assistant's
  [MQTT integration](https://www.home-assistant.io/integrations/mqtt/).

## Quick start

`docker-compose.yml` already has the hardened deployment settings: non-root, read-only root
filesystem, no capabilities, `./data` for state, and a healthcheck. All configuration comes from
`.env`. You need those two files from this repository, `docker-compose.yml` and `.env.example`.
Clone it, or fetch just them:

```sh
mkdir oiltank && cd oiltank
curl -fsSLO https://raw.githubusercontent.com/icooper/oiltank/main/docker-compose.yml
curl -fsSLO https://raw.githubusercontent.com/icooper/oiltank/main/.env.example
```

Then create `.env`, pull the image (`ghcr.io/icooper/oiltank`, for amd64 or arm64) and check the
camera:

```sh
cp .env.example .env                       # then fill in the camera URL, MQTT broker, ...
mkdir -p data && sudo chown 65532:65532 data
docker compose pull
alias oiltank='docker compose run --rm oiltank'
oiltank grab --out /data/frame.jpg         # check the camera: a full-size frame in ./data
```

Then [calibrate](#calibration), check a reading, and start the service:

```sh
oiltank read
# fetching 5 frame(s) from go2rtc.lan:1984, 2 s apart
# level 97.8%  (t=0.817, confidence 1.00, indicator at x=2169.0 y=1047.9, 5 frame(s))
docker compose up -d
docker compose ps                          # STATUS shows (healthy) once it's running
docker compose logs -f                     # a line per reading
```

The **Oil Tank** device then appears in Home Assistant (see [Home Assistant](#home-assistant)).
To try it on sample frames first, without a camera, see
[CONTRIBUTING.md](CONTRIBUTING.md#building-the-image).

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

## The service

`oiltank run`, the container's default command, reads the gauge every
`OILTANK_SERVICE_INTERVAL_MIN` minutes (15 by default), straight away when **Read now** is pressed,
and once at startup:

```
INFO oiltank.service: starting: a reading every 15 min, publishing to homeassistant.lan:1883
INFO oiltank.publish.mqtt: connected to homeassistant.lan:1883
INFO oiltank.service: level 97.8% (245 gal), confidence 1.00; using 1.4 gal/day, 168 days left
```

Each reading is filtered before it's published:

- **Rejected readings** have a problem, or a confidence below `OILTANK_SERVICE_MIN_CONFIDENCE`.
  They leave the last good level published. After `OILTANK_SERVICE_FAILURES_FOR_PROBLEM` (3) in a
  row, **Problem** turns on, with the reason. It turns off again at the next good reading.
- **Smoothing.** The level is the median of the last `OILTANK_SERVICE_SMOOTHING` (3) good
  readings, so one misread can't move it.
- **Refills.** A rise of more than `OILTANK_SERVICE_REFILL_JUMP` (15% of the gauge) is held back
  until the next reading confirms it. Then **Last fill** and **Last fill amount** update, and the
  usage history restarts. A jump the next reading doesn't confirm is ignored.
- **Usage rate** is a robust trend of the volume over the last
  `OILTANK_SERVICE_USAGE_WINDOW_DAYS` (7) since the last refill. It appears once there are 12
  readings spanning at least 24 hours. **Days left** is the volume above `OILTANK_TANK_RESERVE`
  divided by the usage rate. It's unknown while the usage is near zero, such as in summer.

Every reading attempt is stored in `data/oiltank.db` (SQLite), along with the gauge position it
saw. On startup, and whenever `calibration.yaml` changes, the service rebuilds its state by
replaying recent readings with the current calibration. So adding a calibration point with
`oiltank calibrate point` while the service runs also corrects the usage history.

The stored positions also survive the other calibration changes:
- **Axis edits** (`oiltank calibrate axis`): each stored position remembers the axis it was
  measured along and is mapped onto the new one.
- **Rebases** (`oiltank calibrate rebase`): stored positions are kept as they are, because the
  disc is still at the same place on the gauge.
- **Anything else,** such as a new reference frame whose axis doesn't simply follow the camera
  (`calibrate init --force`): positions can't be mapped. The service logs a warning and keeps
  them as they are.

`oiltank history [--days N]` lists recent readings and refills, and what they add up to, worked
out with the current calibration.

`oiltank healthcheck` (the container's healthcheck) checks that the service's loop has written its
heartbeat file in the last two minutes. The service stops cleanly on SIGTERM (`docker compose
stop`): it finishes a reading in progress and marks the device unavailable. Starting before the
broker or camera is reachable is fine; it keeps retrying.

### Frame archive

The service also keeps what each reading saw in `data/archive/<UTC date>/`. Each record has three
parts:

- **`<time>Z.json`**: the reading, the per-frame alignment and results, and the calibration's
  geometry.
- **`<time>Z-strip.png`**: the rectified gauge strip, lossless. The JSON's `strip_row`,
  `strip_t_first_row` and `strip_t_last_row` say where the disc is in it, so every strip is a
  labelled example for training a model later.
- **`<time>Z-frame.jpg`**: the full camera frame, exactly as received. It's kept every
  `OILTANK_ARCHIVE_FULL_FRAME_HOURS` (6) and for every problem reading.

That's about 3.3 MB a day at the default settings: a 21 KB strip per reading and four full frames.
Beyond `OILTANK_ARCHIVE_MAX_MB` (2000), whole records are deleted, oldest first, until the
archive is back under 95% of the limit. That's about 20 months. Archived frames can be read again with any calibration, which
makes them useful for checking a calibration change against past readings:
`oiltank read --image data/archive/2026-10-13/035920Z-frame.jpg`.

### Recording a delivery

A delivery takes the disc from near E to near F in a few minutes. It's the best data you'll get:
it checks the reader across the whole gauge, and with the delivery ticket it gives you calibration
points. Start a capture when the truck arrives:

```sh
oiltank capture --every 10s --duration 30m
# capturing 181 time(s), every 10 s for 1800 s, into /data/captures/20261020-141500Z (Ctrl-C stops early)
# 14:15:02   24.6%  t=0.205  confidence 1.00
# 14:15:12   26.1%  t=0.218  confidence 0.99
# ...
```

Each capture keeps the full frame, the strip and the reading in the archive's format, under
`data/captures/<start time>/`. Captures don't count towards the archive's size limit, and they're
never deleted automatically. The service keeps running alongside.

To turn the ticket into a calibration point, take the level from just before the delivery (the
service's last reading, or the capture's first) and add the delivered amount:
`after = before + delivered / capacity`. Then record that reading against the capture's last
frame:

```sh
oiltank calibrate point --fraction 0.93 --note "delivery: 172 gal on the ticket" \
  --image /data/captures/20261020-141500Z/2026-10-20/143010Z-frame.jpg
```

## Web UI

The service can also serve a web page. It shows the level as Home Assistant sees it, the latest
gauge image and the service's state, and it has a **Read now** button. It's off until you give it
a port in `.env`:

```sh
OILTANK_WEB_PORT=8780
```

Then publish the port by uncommenting `ports:` in `docker-compose.yml`, and open
`http://<docker host>:8780/`.

**It has no login.** Anyone who can reach the port can use it. Keep it on your home network or a
private one such as Tailscale, and never forward it from the internet. To narrow it further:

- **Names:** it only answers to the names a browser uses on a private network. Those are IP
  addresses, `localhost`, single-label names, and names ending in `.local`, `.lan`, `.home.arpa`
  or `.internal`. If you reach it by any other name, such as through a reverse proxy, set
  `OILTANK_WEB_URL` to that address.
- **Changes:** it only accepts them from its own pages.
- **Browse only:** `OILTANK_WEB_READ_ONLY=true` makes it refuse calibration changes.

`oiltank web` serves the same page without the service, for looking at a data directory from
elsewhere. Read now isn't available there.

```sh
docker compose run --rm -p 8780:8780 oiltank web --host 0.0.0.0
uv run oiltank web    # from a checkout: http://127.0.0.1:8780/
```

### In Home Assistant

Home Assistant's **Webpage** card shows the web UI on a dashboard. A **Webpage** dashboard puts
it in the sidebar.

Your browser loads the page itself, not Home Assistant, so the UI has to be reachable from
wherever you use Home Assistant. If Home Assistant is served over HTTPS, the UI must be too:
browsers won't show an `http://` page inside an `https://` one. A reverse proxy that already
serves Home Assistant can serve the UI as well.

With Caddy, give the UI a name of its own:

```caddyfile
oiltank.example.com {
    reverse_proxy docker-host:8780
}
```

```sh
# .env
OILTANK_WEB_PORT=8780
OILTANK_WEB_URL=https://oiltank.example.com/
OILTANK_WEB_EMBED_ORIGINS=https://ha.example.com
```

- `OILTANK_WEB_EMBED_ORIGINS` lets Home Assistant's pages show the UI in a frame.
- `OILTANK_WEB_URL` makes the name trusted, and adds a **Visit** link to the device's page in
  Home Assistant.

Then add the card:

```yaml
type: iframe
url: https://oiltank.example.com/
aspect_ratio: 75%
```

<details>
<summary>Under Home Assistant's own name instead</summary>

A path on Home Assistant's name works too:

```caddyfile
ha.example.com {
    redir /oiltank-ui /oiltank-ui/
    handle_path /oiltank-ui/* {
        reverse_proxy docker-host:8780
    }
    reverse_proxy homeassistant:8123
}
```

Set `OILTANK_WEB_URL=https://ha.example.com/oiltank-ui/` and leave out
`OILTANK_WEB_EMBED_ORIGINS`. Choose a path Home Assistant doesn't use.

This shares Home Assistant's origin, which is why a name of its own is better. Home Assistant
keeps your login in the browser's storage for its origin, so script on the UI's pages could read
it. A bug in the UI would then expose all of Home Assistant.

</details>

## Configuration

All settings come from environment variables named `OILTANK_<SECTION>_<KEY>`, normally kept in
`.env`. A test checks this table against the code. Copy `.env.example`, which lists every setting with its default. `.env` is git-ignored, and
Docker Compose passes it to the container whole.

Variables set in your shell win over `.env`, and `.env` wins over the defaults. An empty value
means "not set". An `OILTANK_` name that matches no setting is an error, which catches typos.

| Setting | Default | Meaning |
|---|---|---|
| `OILTANK_CAMERA_URL` | none | A URL that returns one full-size JPEG per request. For go2rtc: `http://<host>:1984/api/frame.jpeg?src=<stream>`. |
| `OILTANK_CAMERA_USERNAME`, `_PASSWORD` | none | Basic auth, if go2rtc's `api:` section sets it. Never put these in the URL. |
| `OILTANK_CAMERA_BURST_FRAMES` | 5 | Frames per reading, combined by median. |
| `OILTANK_CAMERA_BURST_SPACING_S` | 2.0 | Seconds between frames. Use at least the camera's keyframe interval: duplicate frames are dropped and refetched. |
| `OILTANK_CAMERA_TIMEOUT_S` | 15 | Per request. |
| `OILTANK_CAMERA_RETRIES`, `_RETRY_BACKOFF_S` | 3, 2.0 | Retries for timeouts, connection errors and HTTP 5xx. The wait doubles each time. |
| `OILTANK_CAMERA_VERIFY_TLS` | true | For `https://` URLs. |
| `OILTANK_MQTT_HOST`, `_PORT` | none, 1883 | Home Assistant's MQTT broker. |
| `OILTANK_MQTT_USERNAME`, `_PASSWORD`, `_TLS` | none, none, false | |
| `OILTANK_MQTT_CLIENT_ID`, `_TOPIC_PREFIX`, `_DISCOVERY_PREFIX` | oiltank, oiltank, homeassistant | |
| `OILTANK_DEVICE_ID`, `_NAME` | oil_tank, Oil Tank | How the tank appears in Home Assistant, and the entity ids it gets. |
| `OILTANK_TANK_CAPACITY`, `_UNIT` | 260, gal | Usable capacity when the gauge reads F. |
| `OILTANK_TANK_RESERVE` | 0 | "Days left" counts down to this volume. |
| `OILTANK_SERVICE_INTERVAL_MIN` | 15 | Minutes between readings. |
| `OILTANK_SERVICE_MIN_CONFIDENCE`, `_FAILURES_FOR_PROBLEM` | 0.5, 3 | Rejecting readings, and when Problem turns on. See [The service](#the-service). |
| `OILTANK_SERVICE_SMOOTHING`, `_REFILL_JUMP` | 3, 0.15 | Median window; the rise (fraction of the gauge) that counts as a refill. |
| `OILTANK_SERVICE_USAGE_WINDOW_DAYS`, `_USAGE_MIN_HOURS`, `_USAGE_MIN_POINTS` | 7, 24, 12 | Usage-rate trend window, and how much data it needs. |
| `OILTANK_ARCHIVE_ENABLED`, `_MAX_MB` | true, 2000 | The [frame archive](#frame-archive), and its size limit. |
| `OILTANK_ARCHIVE_FULL_FRAME_HOURS`, `_PROBLEM_FRAMES` | 6, true | How often to keep a full frame (0: only for problems), and whether to keep problem readings' frames. |
| `OILTANK_WEB_PORT`, `_HOST` | 0, 0.0.0.0 | The [web UI](#web-ui) (0: off), and the address it listens on. |
| `OILTANK_WEB_URL`, `_READ_ONLY` | none, false | Where browsers reach the web UI, if not by IP address or a local name; and whether it refuses calibration changes. |
| `OILTANK_WEB_EMBED_ORIGINS` | | Sites allowed to show the web UI in a frame (space-separated origins, e.g. `https://ha.example.com`). |
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

<img src="docs/images/diagnostic.jpg" alt="The diagnostic view: disc outlines at 0% and 100%, the detected disc, the search area and the anchor" width="420">


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

The best way to collect points along the whole scale is to
[record a delivery](#recording-a-delivery).

## Home Assistant

The tank appears as one device, **Oil Tank**, through
[MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery). No custom
integration is needed: HA's MQTT integration and a broker are enough.

| Entity | Type | Notes |
|---|---|---|
| Level | sensor, % | |
| Volume | sensor, `volume_storage` | Level × `OILTANK_TANK_CAPACITY`, in `OILTANK_TANK_UNIT` |
| Usage rate | sensor, gal/d | Unknown until there's a day of readings |
| Days left | sensor, `duration` | Until `OILTANK_TANK_RESERVE`; unknown while usage is near zero |
| Last fill, Last fill amount | sensors, `timestamp` / `volume` | From refill detection |
| Gauge | image | The annotated gauge, updated with each reading |
| Read now | button | Takes and publishes a reading straight away |
| Problem | binary sensor, `problem` | On after repeated rejected readings; the `reason` attribute says why (see [Troubleshooting](#troubleshooting)) |
| Confidence, Last reading | diagnostic sensors | |
| Gauge position | diagnostic sensor, disabled by default | The raw position `t` along the axis |

All entities are unavailable when the service isn't connected: it announces `online` when it
connects and sets a last will of `offline`.

### Publishing by hand

`oiltank mqtt publish` takes one reading and publishes it without the service's filtering or
history. It's useful for setting up the Home Assistant side. `--listen 300` keeps it connected
for five minutes to answer **Read now**. Don't run it while the service is running: they share the
availability topic.

```sh
oiltank mqtt publish --listen 300
# connected to homeassistant.lan:1883; discovery on homeassistant/device/oil_tank/config
# level 97.9%, 245 gal (confidence 1.00, 5 frame(s))
# published to oiltank/oil_tank/state and oiltank/oil_tank/image
```

`oiltank mqtt clear` removes the device from Home Assistant, and its retained messages from the
broker.

### Topics

The base topic is `<OILTANK_MQTT_TOPIC_PREFIX>/<OILTANK_DEVICE_ID>`, `oiltank/oil_tank` by
default. Every message is published with QoS 1.

| Topic | Retained | Payload |
|---|---|---|
| `homeassistant/device/oil_tank/config` | yes | Discovery: the device and all its entities |
| `oiltank/oil_tank/availability` | yes | `online` / `offline` (also the last will) |
| `oiltank/oil_tank/state` | yes | JSON, below |
| `oiltank/oil_tank/image` | yes | The annotated gauge as a JPEG of at most 50 KB |
| `oiltank/oil_tank/cmd/read` | no | `PRESS` from the Read now button |

```json
{"level": 97.9, "volume": 244.6, "confidence": 1.0, "t": 0.8179, "usage_rate": null,
 "days_left": null, "last_fill": null, "last_fill_volume": null, "problem": false,
 "reason": null, "ts": "2026-10-13T03:29:34+00:00"}
```

`null` means "not known yet", and Home Assistant shows it as unknown. The service republishes
discovery when it reconnects, and a few seconds after Home Assistant announces `online` on
`homeassistant/status`. So a restart of either side recovers by itself.

### Automations

A low-oil alert and a delivery notice, using the default entity ids:

```yaml
automation:
  - alias: Heating oil low
    triggers:
      - trigger: numeric_state
        entity_id: sensor.oil_tank_days_left
        below: 21
    actions:
      - action: notify.notify
        data:
          message: >-
            Heating oil: {{ states('sensor.oil_tank_level') }}% left, about
            {{ states('sensor.oil_tank_days_left') }} days at the current rate. Time to order.
  - alias: Heating oil delivered
    triggers:
      - trigger: state
        entity_id: sensor.oil_tank_last_fill
    conditions: "{{ trigger.from_state.state not in ['unknown', 'unavailable'] }}"
    actions:
      - action: notify.notify
        data:
          message: "Oil delivered: {{ states('sensor.oil_tank_last_fill_amount') }} gal."
```

### Dashboard card

A gauge, the details, and the latest gauge image (YAML for a manual card):

```yaml
type: vertical-stack
cards:
  - type: gauge
    entity: sensor.oil_tank_level
    name: Heating oil
    min: 0
    max: 100
    needle: true
    severity:
      green: 40
      yellow: 20
      red: 0
  - type: entities
    entities:
      - sensor.oil_tank_volume
      - sensor.oil_tank_usage_rate
      - sensor.oil_tank_days_left
      - sensor.oil_tank_last_fill
      - sensor.oil_tank_last_fill_amount
      - binary_sensor.oil_tank_problem
      - button.oil_tank_read_now
  - type: picture-entity
    entity: image.oil_tank_gauge
    show_name: false
    show_state: false
```

The entity ids come from the device name. With `OILTANK_DEVICE_NAME` set to something else, they
follow it.

## Accuracy and limitations

- **About ±2–3% at 4K.** The disc moves about 125 px from E to F, so 1 px is 0.8% of the gauge,
  and readings of the same scene agree to within about 1 px. The limits are the camera's noise
  reduction, JPEG compression, and the textured wall seen through the clear dome, more than
  resolution.
- **The scale is only as good as its points.** The gauge mechanism isn't perfectly linear, and
  where the dome's ends fall on the scale varies between gauges. On the sample tank, assuming the
  dome cap was F read 82% instead of 98%. Add points from readings you trust, and from deliveries.
- **Volume is level × capacity.** That assumes the gauge's marks are fractions of the tank's
  volume, which is how gauges for a given tank size are usually marked. If yours aren't, points at
  known volumes (delivery tickets) correct it. A proper tank chart is on the
  [roadmap](CONTRIBUTING.md#roadmap).
- **The disc size matters.** A template much smaller than the disc can lock onto a highlight with
  full confidence; see [Disc size](#disc-size). `calibrate show` reports how much margin you have.
- **Tested so far near full.** The sample frames, day and night, all show the tank at about 98%.
  Synthetic tests move the disc across the whole gauge, and the next delivery capture will show
  how real readings hold up at other heights.
- **Usage rate needs the heating to be using oil.** In summer, with only hot water or nothing,
  usage can be close to zero and days left unknown, which is correct but not very useful.

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

MQTT problems, from `oiltank mqtt`:

| Message | Cause |
|---|---|
| `refused the connection: Not authorized` | Check `OILTANK_MQTT_USERNAME` and `OILTANK_MQTT_PASSWORD`. |
| `no answer from host:1883` | Wrong host or port, or the broker is down. TLS brokers usually listen on 8883 with `OILTANK_MQTT_TLS=true`. |
| Device doesn't appear in HA | Check that HA's MQTT integration uses the same broker and discovery prefix (`homeassistant`). `mosquitto_sub -v -t 'homeassistant/device/#'` should show the config message. |

**Problem** reasons, as the `reason` attribute in Home Assistant and in the service's log:

| Reason | Meaning | What to do |
|---|---|---|
| `camera_unreachable` | No frames: go2rtc or the camera is down, or it keeps sending the same frame | Check go2rtc; the service retries every interval |
| `camera_moved` | The alignment anchor can't be found | `oiltank calibrate rebase --camera` (or `init` if the view changed) |
| `low_confidence` | Frames arrive, but the disc can't be found reliably | `oiltank calibrate show --camera --diagnostic /data/check.jpg` |
| `wrong_resolution` | Frames don't match the calibration's size | Point `OILTANK_CAMERA_URL` at the main stream, or recalibrate |

`docker compose ps` shows `unhealthy` if the service's loop stops making progress. Docker
doesn't restart unhealthy containers by itself, but `docker compose logs` shows why.

## Updating

Each release is published to `ghcr.io/icooper/oiltank` for amd64 and arm64, with
[release notes](https://github.com/icooper/oiltank/releases). To update:

```sh
docker compose pull && docker compose up -d
```

Versions follow [semantic versioning](https://semver.org/), and each release is tagged four ways,
for example `1.4.2`, `1.4`, `1` and `latest`. `docker-compose.yml` uses `latest`. To take only
compatible updates, pin the major version instead (`image: ghcr.io/icooper/oiltank:1`), or pin a
full version to stay on one release. `./data` (the calibration, history and archive) is kept
across updates.

## Container and security

The image is built in two stages. A uv builder installs a uv-managed CPython 3.14 and a
non-editable virtualenv, which are copied into
[`gcr.io/distroless/cc-debian12:nonroot`](https://github.com/GoogleContainerTools/distroless).
It's built natively for amd64 and arm64, and each one is smoke-tested on its own architecture,
running locked down on the sample frames, before it's published.

- no shell or package manager in the runtime image;
- runs as uid/gid **65532**, never root;
- works with a **read-only root filesystem**, **all capabilities dropped** and
  `no-new-privileges`, which is how `docker-compose.yml` runs it. The only writable place is the
  `./data` mount:
  ```sh
  docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges ghcr.io/icooper/oiltank --version
  ```
- zlib is the only system library added on top of distroless, because the numpy and OpenCV wheels
  link against it;
- nothing from the checkout gets into the image except the code and its lock file
  (`.dockerignore` is an allowlist). Settings and credentials come from `.env` at run time, and
  credentials are never logged;
- it only makes outbound connections, to go2rtc and the MQTT broker. It listens on no ports
  unless you turn on the [web UI](#web-ui), which has no login, so keep that port private.

## License

[MIT](LICENSE.md)
