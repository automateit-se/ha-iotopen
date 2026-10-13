# ha-iotopen – development notes

Design decisions, status and roadmap. For user documentation, see the [README](../README.md). For an overview of
the components and data flows, see the [architecture diagram](architecture.png) (source: [architecture.svg](architecture.svg); re-export the PNG after editing it).

## Status

| Part | Status |
|---|---|
| Survey of existing integrations | Done. There is no Home Assistant integration for IoT Open (see below). |
| `pyiotopen` (REST + MQTT over WebSocket + structure events) | Implemented. Pure tests (topics, models, codec) pass. |
| Import: IoT Open → HA (sensor, binary_sensor, switch, light) | Implemented and running on a production Home Assistant OS install. |
| Mirroring: HA → IoT Open (label-based) | Implemented and verified live, including control from IoT Open. |
| Config subentries per installation | Implemented. Older entries are migrated automatically. |
| Mapping rules (`mapping.py`, `export_mapping.py`) | Pure tests pass. |
| Integration tests (pytest-homeassistant-custom-component) | Written, not yet run in CI. |
| aiohttp tests for `pyiotopen` (fake Lynx + MQTT broker) | Written, not yet run in CI. |

## Prior art

- **IoTOpen/py-lynx** (PyPI `iotopen-py-lynx`) is synchronous and REST-only, with no MQTT. Home Assistant requires
  async I/O, so a new async library was written.
- IoT Open's GitHub organisation (node-red-lynx, go-lynx, react-lynx, grafana-datasource-lynx, …) has nothing for
  Home Assistant.
- Nothing in HACS default (`hacs/default/integration`) matches "iotopen" or "lynx".
- `home-assistant.io/integrations/iotopen` does not exist.

## Code layout

```
pyiotopen/                      standalone library (to be published on PyPI; lives in this repository for now)
  client.py      IoTOpenClient  REST: auth (+2FA), installationinfo, devicex, functionx, status, log, meta, CRUD
  mqtt.py        IoTOpenMqtt    MQTT 3.1.1 over WebSocket, reconnect/back-off, keep-alive, subscription diff
  mqtt_codec.py                 pure packet encoding (testable without network)
  events.py                     parse_message → ValueUpdate | StructureEvent
  topics.py                     normalize_topic ("<cid>/obj/x" → "obj/x"), full_topic, structure_topics
  models.py                     Installation, Device, Function, Status, InstallationSnapshot

custom_components/iotopen/
  __init__.py                   thin shim that re-exports integration.py
  integration.py                setup/unload, update listener, device removal
  hub.py         IoTOpenHub     snapshot per installation, MQTT, debounce, diff → add/update/remove entities
  mapping.py                    pure: function → platform/device class/state class/unit (no HA imports)
  exporter.py    IoTOpenExporter mirrors labelled HA entities/devices into IoT Open
  export_mapping.py             pure: entity → IoT Open function spec, values, commands, ownership
  entity.py, sensor.py, binary_sensor.py, switch.py, light.py
  config_flow.py                config flow, reauth, options, installation subentries
  diagnostics.py, strings.json, translations/{en,sv}.json
  frontend/iotopen-icons.js     icon set (iotopen:logo, iotopen:badge) used by the label
  brand/                        icon and logo images shown by Home Assistant
  pyiotopen/                    bundled copy of pyiotopen/src/pyiotopen (see "Packaging")
```

## Import flow

This follows the same pattern as IoT Open's own web dashboards:

1. At start-up, a REST snapshot is read for every selected installation.
2. MQTT subscriptions are made to `<client_id>/<topic_read>` for every function, plus
   `<client_id>/evt/{functionx,devicex}/updated`.
3. When a structure event arrives, the installation is re-read after a 1 s debounce and the changes are applied.
   Values newer than the REST response are kept.
4. On reconnect, everything is re-read.
5. Once an hour `installationinfo` is read, because new installations are not announced through structure events.

## Mirroring HA → IoT Open

- One installation is chosen as the mirror target in the options. It is pinned in the options, so adding a second
  installation never disables mirroring silently.
- Selection: the HA label **IoT Open** is created automatically with an IoT Open icon. It can be put on entities or
  on devices. A labelled device mirrors all its entities except diagnostic and hidden ones.
- HA device → IoT Open device (type `HomeAssistant`, metadata `name`, `manufacturer`, `model`, `ha.device_id`,
  `ha.source`, `ha.source_id`).
- HA entity → function with `topic_read=obj/ha/<domain>/<object_id>`. Controllable entities also get
  `topic_write=set/obj/ha/<domain>/<object_id>`. Metadata includes `state_*`/`text_*` for the type, `unit`,
  `ha.entity_id`, `ha.device_class`, `ha.type`, `ha.source` and `ha.source_id`.
- IoT Open standard types are used: `switch`, `dimmer` (level on the main topic, on/off on `…/onoff`), `door`,
  `movement` and `boolean_text`. All functions get `visible`.
- Ownership: objects whose `ha.source_id` matches this instance are owned by HA. The import side skips them; the
  export side creates, updates and deletes them. Objects without `ha.source_id` fall back to matching `ha.source`
  (legacy). The user's own metadata keys are never touched.
- **IoT Open is master for presentation.** HA only seeds `name`, `visible`, `unit` and all `text_*` keys when they
  are missing. Earlier versions wrote HA's values back a few seconds after every structure event, which reverted
  manual edits made in IoT Open. Delete a key in IoT Open to get HA's value back. Structural keys (`topic_*`, `ha.*`,
  `device_id`, `state_*`, `min`/`max`) still follow HA. A device keeps a name set in IoT Open. See `desired_update`
  and `desired_device_meta` in `export_mapping.py`.
- **IoT Open is also master for the function type.** `ha.type` remembers the type HA suggested:
  - same type as HA suggests: structural keys follow HA, seed-only keys are filled in when missing;
  - type changed in IoT Open (e.g. `boolean_text` → `door`) while HA still suggests the same type: the type and all
    `state_*`/`text_*` keys are kept;
  - HA suggests a new type (e.g. the entity got another device class): HA's type and keys are written, and the old
    texts are replaced since they no longer fit;
  - objects from before `ha.type` existed count as changed in IoT Open when the types differ.
- State changes are published as `{"value","timestamp","msg"}`. Everything is republished on reconnect.
- `set/obj/ha/...` calls the HA service (`turn_on`/`turn_off`, `set_value`, `open`/`close`, `lock`/`unlock`,
  `press`). Command handling never raises; failures are logged.
- The initial mirror sync runs as a background task, so it does not delay Home Assistant start-up.
- Excluded domains: `event`, `update`, `media_player` and others whose state is not a meaningful value (see
  `EXCLUDED_DOMAINS`).
- Use a separate IoT Open user for the integration, so the IoT Open log shows who wrote what.
- The API key's user needs write access to DeviceX and FunctionX in the mirror installation. Without it the
  response is HTTP 403.

## Selecting what to import (`ha.destination`)

- Each HA instance has an instance name (default: slugified location name) and a stable instance id.
- `ha.destination` on an IoT Open device or function names the instances it should appear in: a name, a
  comma-separated list, the instance id, or `*`. A function value overrides its device's value.
- Import mode *Only tagged* (default) imports only matching objects. *All* imports every function.
- An installation hub device is only created when there are functions without a device. Device manufacturer and
  model come from the IoT Open device metadata, and `configuration_url` links to the device in Lynx.

## Packaging

- The library is bundled in `custom_components/iotopen/pyiotopen/` and imported relatively
  (`from .pyiotopen import …`). The canonical source is `pyiotopen/src/pyiotopen`. Run
  `python tools/sync_vendor.py` after changing it.
- `tests/test_packaging.py` fails if any integration file imports `pyiotopen` absolutely, or if the bundled copy
  differs from the source. This guards against a real outage: files copied from a source tree that used absolute
  imports crashed the integration with `ModuleNotFoundError: No module named 'pyiotopen'`.
- `manifest.json` has empty `requirements` until `pyiotopen` is on PyPI. The imports switch back then.
- The MQTT client id is unique per run (`ha-iotopen-<entry>-<random>`). A fixed id let a connection leaked by a
  failed setup kick out the new one, causing a reconnect storm.
- The mirror exporter is started from the hub in a way that never raises, so a mirroring problem cannot take down
  the import side.

## Deployment notes

- When deploying by copying files (for example over the Samba add-on), copy the whole `custom_components/iotopen`
  folder including the bundled `pyiotopen/`, then restart Home Assistant.
- Some remote file tools cache uploads per path. Use unique file names per deploy and check the file size on the
  target afterwards.

## Decisions

- **Topic convention (IoT Open standard):** status is read from `topic_read` = `obj/...` (logged), control is sent
  to `topic_write` = `set/obj/...`. The metadata key is `topic_write`; `topic_set` is only read as a fallback for
  older functions.
- `unique_id = iotopen_<installation_id>_<function_id>`.
- Device identifiers: `installation_<iid>`, `device_<iid>_<device_id>`.
- Devices are only created for IoT Open devices with at least one entity, which avoids empty devices.
- A device belongs to exactly one config entry and subentry (Home Assistant 2026.10+ device model). Lookups use
  `async_get_device_by_identifier` when available.
- A switch's state follows `topic_read` (the confirmation), not the command. Without `topic_read` the switch is
  optimistic.
- Commands are sent as `{"value": <state_on|state_off>}` to `<client_id>/<topic_write>`. Numeric strings are sent as
  numbers.
- Entities become unavailable only after MQTT has been down for more than 60 s. This avoids flapping on short
  outages.
- `ha.hidden` sets `hidden_by=integration` but never overrides the user's own choice.
- If the platform changes (e.g. sensor → binary_sensor), the entity is removed and recreated. If only the device or
  metadata changes, the entity is updated in place, so the user's name and area are kept.
- Passwords are never stored. On login only the token and username (for reauth) are stored.
- Config entry unique id: `<host>_<user.id>` from `/api/v2/user/me`. Fallback if `me` cannot be read:
  `<host>_<installation ids>`.
- A numeric value without a unit, e.g. `level`, becomes a text sensor. Set `ha.state_class=measurement` to get
  statistics.

## Open questions to verify against Lynx

- [ ] Does `/api/v2/user/me` work with an API key (not only a session token)?
- [ ] Payload format for `topic_write` (`set/obj/...`): is `{"value": 1}` enough, or does the edge side expect
      `timestamp`/`msg`?
- [ ] Does `/api/v2/status/{iid}?topics=...` filter on the bare topic?
- [ ] Does Lynx answer 401 or 403 on an invalid/expired token? The code treats only 401 as an auth error.
- [ ] What does the broker return on a bad token: CONNACK 4/5, or a closed WebSocket (HTTP 401 in the handshake)?
      Both cases are handled.
- [ ] Is there an API for creating API keys? Then the login flow could create a key instead of storing an expiring
      token.
- [ ] Lowest Home Assistant version that works (config subentries). `hacs.json` says 2025.2.0; only 2026.9 and
      later have been tested on real hardware.
- [ ] Does Lynx send `expires_at` in the auth response, and in which format?

## Next steps

1. **GitHub** (repository `ha-iotopen`):
   - Repository: https://github.com/automateit-se/ha-iotopen
   - Push as a private repository, review, then make it public.
   - `pyiotopen/` stays in this repository for now. Move it to its own repository when it is published on PyPI.
2. **Run the tests** in CI (GitHub Actions) or locally:
   `pip install -r requirements_test.txt && pytest && pytest pyiotopen/tests`.
3. **First release**: tag `v0.3.1` and publish a GitHub release (0.3.0 was never published as a release) (see [CONTRIBUTING.md](../CONTRIBUTING.md)).
4. **PyPI**:
   - Check that the name `pyiotopen` is free.
   - Set up trusted publishing and publish 0.1.0. `pyiotopen/.github/workflows/publish.yml` is a template: GitHub
     only runs workflows in the root `.github/`, so move it there (or to the new repository) first.
   - Then add it to `requirements` in `manifest.json` and switch to absolute imports.
5. **Brands**:
   - The integration ships its own `custom_components/iotopen/brand/` images (icon, logo, dark logo), which newer
     Home Assistant versions show directly.
   - For older versions and HACS listings, open a PR to `home-assistant/brands` with `custom_integrations/iotopen/icon.png` (256×256) and `icon@2x.png`
     (512×512), preferably also `logo.png`.
   - Use IoT Open's official logo, supplied by IoT Open.
   - Then remove `ignore: brands` in `validate.yml`.
6. **HACS default**:
   - Public repository with description and topics (`home-assistant`, `hacs`, `iot-open`).
   - At least one GitHub release.
   - hassfest and the HACS action green.
   - Brands merged.
   - Then a PR to `hacs/default` adding the repository to the `integration` file (alphabetically).
7. **Features**:
   - `number` (topic_write without on/off) and `select` on the import side.
   - History import via `v3beta/log` into long-term statistics.
   - A service for setting metadata.
   - Reconfigure flow (change server URL).
   - HA tests for the exporter.
8. **Towards HA core**:
   - Reach bronze on the quality scale.
   - 100 % test coverage for the config flow.
   - First PR with one platform (sensor); the others in later PRs.
   - Library on PyPI with source and releases on GitHub.

## Logging

Add this to `configuration.yaml` for troubleshooting:

```yaml
logger:
  logs:
    custom_components.iotopen: debug
```

This also covers the bundled library (`custom_components.iotopen.pyiotopen.*`). Once the integration uses
`pyiotopen` from PyPI, add `pyiotopen: debug` as well.
