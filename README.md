# oil-tank-level

Reads the float gauge on a residential heating-oil tank from an IP camera and publishes the level
to [Home Assistant](https://www.home-assistant.io/) over MQTT, with entities that appear
automatically through MQTT discovery, the way Zigbee2MQTT's do.

> **Status: early development (milestone M0).** The project skeleton, tooling and distroless
> container are in place. Gauge reading, calibration, the go2rtc source and MQTT publishing are
> next; see [Roadmap](#roadmap). Sections marked *planned* describe behaviour that isn't built yet.

## How it works

```
go2rtc ──HTTP JPEG──▶ align ──▶ rectify gauge ──▶ find disc ──▶ calibrate ──▶ filter ──▶ MQTT ──▶ Home Assistant
(4K IR frame)        (camera    (upright strip)   (position t)  (t → level)  (smooth,
                      bumps)                                                  refills,
                                                                              usage)
```

1. **Fetch.** A burst of frames is pulled from [go2rtc](https://github.com/AlexxIT/go2rtc)'s
   `frame.jpeg` API at the camera's native 4K resolution.
2. **Align.** Each frame is registered against static parts of the scene (the gauge fitting and
   pipework), so a small camera bump doesn't throw the reading off.
3. **Rectify and read.** The clear gauge dome is warped into an upright strip. The indicator disc
   inside it is located with a matched filter on row-intensity profiles, giving its position `t`
   from 0 (bottom) to 1 (top) with a confidence score.
4. **Calibrate.** `t` is mapped to a fill fraction through calibration points you check against
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
- **Enough resolution.** The gauge's travel should span at least ~50 px, ideally ≥100 px. A
  low-resolution snapshot or substream isn't enough:

  | Frame size | Gauge travel | Precision per pixel |
  |---|---|---|
  | 3840×2160 (4K main stream) | ~170 px | ~0.6% (~1.5 gal) |
  | 1280×720 | ~59 px | ~1.7% |
  | 640×360 (typical snapshot/substream) | ~29 px | ~3.4% (about ±8–10% in practice) |

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

For now you can build the image and run the CLI against an image file:

```sh
docker build -t oiltank:dev .
docker run --rm -v "$PWD/samples:/samples:ro" oiltank:dev read --image /samples/frame0.jpg
```

## Configuration

*Planned.* Configuration will be a YAML file mounted read-only at `/config/config.yaml`. Any key
can be overridden with an environment variable of the form `OILTANK__SECTION__KEY`, for example
`OILTANK__MQTT__PASSWORD`. The full reference will go here.

## Calibration

*Planned (milestone M2).* The `oiltank calibrate` commands will record the gauge axis, an alignment
anchor and the disc template. They will also let you add calibration points by reading the gauge
yourself. Running `oiltank capture` during an oil delivery records the disc across its whole range.

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
taken with the tank near full.

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
- [ ] **M1** Geometry, calibration model, classical reader, annotated output
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
