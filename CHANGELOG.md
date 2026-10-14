# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [semantic versioning](https://semver.org/).

## Unreleased

## 0.4.0

### Mirroring (Home Assistant → IoT Open)

- Media players are mirrored. A player (for example a Sonos speaker) becomes up to five functions on the same IoT
  Open device: playback, volume, mute, status and power, on `obj/ha/media_player/<object_id>/<part>`. Only the
  functions the player supports are created, and they are removed if the player loses the feature.
- The playback function sends the current track in `msg` (`Artist - Title`), so the IoT Open log shows what was
  played. Media values are only sent when they change, and volume is debounced by about 1 s.
- New option *Mirror diagnostic and configuration entities of labelled devices* (off by default). Without it, a
  labelled device still leaves out entities such as a phone's battery and network sensors.
- Function names of media players are in Swedish when Home Assistant's language is Swedish.

### Tests

- Home Assistant tests for the exporter (media players, commands, debounce, categorized entities).

### Releases

- Running the *Release* workflow manually now creates a draft release (notes and `iotopen.zip` included) that is
  published with one click on GitHub.

## 0.3.2

### Brand images

- Home Assistant 2026.3 and later, and HACS, use the integration's own `brand/` images (icon, logo, dark logo).
  No entry in `home-assistant/brands` is needed. Older Home Assistant versions show a placeholder icon.

### CI

- The HACS action now runs its brands check, which passes with the integration's own `brand/` images.
  The `brands/` folder is removed; the SVG sources moved to `docs/brand/`.

## 0.3.1

First GitHub release. It also contains everything listed under 0.3.0, which was never published as a release.

### Documentation

- README: add the HACS custom repository step, needed until the integration is in the HACS default list.

### Releases

- Pushing a `v*` tag now publishes the GitHub release, with notes from this changelog and `iotopen.zip` attached.
  The workflow can also be run manually on `main`; it then creates the tag from the version in `manifest.json`.
- Dependabot keeps the GitHub Actions in the workflows up to date.

## 0.3.0 – first public release

### Import (IoT Open → Home Assistant)

- Every IoT Open function becomes an entity: `sensor`, `binary_sensor`, `switch` or `light`.
- Live values over MQTT (WebSocket). Devices and functions added, changed or removed in IoT Open show up within
  about a second.
- IoT Open standard function types: `switch`, `dimmer`, `door`, `movement` and `boolean_text`.
- One config subentry per installation. Installations can be added later with *Add installation*.
- `ha.destination` metadata selects what each Home Assistant instance imports. The default import mode is
  *Only tagged*.
- Overrides through function metadata: `ha.platform`, `ha.device_class`, `ha.state_class`, `ha.unit`, `ha.hidden`.

### Mirroring (Home Assistant → IoT Open)

- Entities and devices with the label **IoT Open** are created as devices and functions in a chosen installation.
  The label is created automatically and has an IoT Open icon.
- States are published to `obj/ha/…`. IoT Open can control Home Assistant through `set/obj/ha/…` (can be turned
  off).
- Objects created by Home Assistant are identified by `ha.source_id`. IoT Open stays master for the type, name,
  visibility, unit and texts once they are seeded.

### Other

- Sign in with an API key or with username, password and two-factor code. Re-authentication flow.
- Diagnostics with tokens and personal data redacted.
- English and Swedish translations.
