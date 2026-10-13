# Contributing to ha-iotopen

Thanks for helping out. This file explains how to work on the code, test a change on a real Home Assistant, and
make a release. For design decisions and the roadmap, see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Repository layout

| Path | What it is |
|---|---|
| `custom_components/iotopen/` | The Home Assistant integration. This folder is what HACS installs. |
| `custom_components/iotopen/pyiotopen/` | Bundled copy of the library. **Do not edit it by hand.** |
| `pyiotopen/` | The library source (`src/pyiotopen`), its tests and packaging. Edit the library here. |
| `tests/` | Integration tests (pytest-homeassistant-custom-component). |
| `tools/sync_vendor.py` | Copies `pyiotopen/src/pyiotopen` into the integration. |
| `docs/` | Development notes and the architecture diagram. |
| `brands/` | Images prepared for a PR to `home-assistant/brands`. |

## Development setup

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest                  # integration tests
pytest pyiotopen/tests  # library tests
```

The tests never talk to the real IoT Open service: the integration tests mock it, and the library tests use an
in-process fake Lynx (`pyiotopen/tests/fake_lynx.py`) with a small MQTT-over-WebSocket broker.

## Rules that keep the integration installable

- **Edit the library in `pyiotopen/src/pyiotopen`, then run `python tools/sync_vendor.py`.**
  `tests/test_packaging.py` fails if the bundled copy differs from the source.
- **Import the library relatively inside the integration:** `from .pyiotopen import IoTOpenClient`, never
  `from pyiotopen import …`. Home Assistant does not install `pyiotopen` from PyPI (yet), so an absolute import
  crashes the integration at start-up. The packaging test guards this too.
- Keep `mapping.py` and `export_mapping.py` free of Home Assistant imports. They hold the mapping rules and are
  tested as plain Python.
- New user-facing text goes into `strings.json` and both `translations/en.json` and `translations/sv.json`.
- Never log or store passwords. Tokens and personal metadata must stay redacted in `diagnostics.py`.

## Testing on a real Home Assistant

1. Copy `custom_components/iotopen/` to `/config/custom_components/iotopen/` (for example with the Samba or
   *Advanced SSH & Web Terminal* add-on). Copy the whole folder, including `pyiotopen/`.
2. Restart Home Assistant and check *Settings → System → Logs* for `iotopen`.
3. Use a test installation in IoT Open, and a separate IoT Open user for the integration, so the IoT Open log shows
   which changes came from Home Assistant.

Debug logging:

```yaml
logger:
  logs:
    custom_components.iotopen: debug
```

## Pull requests

- One topic per pull request. Describe what changed and how you tested it.
- CI must be green: *Tests* (library + integration) and *Validate* (hassfest + HACS).
- Update `README.md` when behaviour or options change, and `docs/DEVELOPMENT.md` when a design decision changes.
- Add a line to `CHANGELOG.md` under *Unreleased*.

## Making a release

1. Move the *Unreleased* entries in `CHANGELOG.md` under a new version heading.
2. Set the same version in `custom_components/iotopen/manifest.json` (for example `0.3.1`).
3. Commit and merge to `main`. Then either run *Actions → Release → Run workflow* on `main`, which creates the
   tag `v0.3.1` from `manifest.json`, or tag the merged commit yourself and push the tag:
   `git tag v0.3.1 && git push origin v0.3.1`.
4. The *Release* workflow checks that the tag matches `manifest.json`, publishes the GitHub release with the
   changelog section as release notes and attaches `iotopen.zip`. HACS users see the new version as an update.

Versioning follows [semantic versioning](https://semver.org/): bump the minor version for new features and the
patch version for fixes. Until 1.0, options and metadata keys may still change; mention that in the release notes.
