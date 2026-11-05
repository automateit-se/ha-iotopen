# Wall panel dashboard (Home Assistant)

A Home Assistant dashboard for a wall display, for example a Shelly Wall Display X2i: lights, a couple of
thermostats, doors and Sonos speakers on one screen. It is made the way Home Assistant dashboards usually are: a
*sections* view with Home Assistant's own cards (tile, thermostat, media control, heading, button). No custom cards,
no HACS, and it keeps working on the local network without internet, as long as Home Assistant and the devices are
local.

The same entities can also be shown in the [IoT Open Dashboard](https://github.com/automateit-se/iotopen-dashboard):
give them the label *IoT Open* and this integration mirrors them into IoT Open (see below).

## Install

1. *Settings → Dashboards → Add dashboard → New dashboard from scratch*. Name it e.g. *Väggpanel*, choose an icon
   and whether it is shown in the sidebar.
2. Open it, *⋮ → Edit dashboard → ⋮ → Raw configuration editor*, replace everything with
   [`dashboard.yaml`](dashboard.yaml) and save.
3. Replace the example entity ids (`light.vardagsrum`, `climate.badrum`, `media_player.vardagsrum` …) with your own.
   The ids are listed under *Settings → Devices & services → Entities*. A card whose entity does not exist shows a
   warning, so the ones left to change are easy to find. Remove the cards you do not need.

Requires Home Assistant 2025.1 or later (sections view, heading card, badge visibility, tile features below the
tile). The volume slider on the tile (`media-player-volume-slider`) needs a later version; on an older Home
Assistant, remove that feature or use a media control card instead.

## The display

Point the display at the dashboard's address, `http://<home assistant>:8123/vaggpanel/hem` (the dashboard's path
as chosen when it was created, then the view's `path`). How a page is opened differs between display firmware
versions; see the display's own documentation.

- Use a separate Home Assistant user for the display, without administrator rights. It can control the
  devices but not change settings.
- The sections view adapts to the width of the screen. Hiding Home Assistant's header and sidebar completely needs
  a custom card (kiosk mode), which this dashboard deliberately does not use.

## What is on it

| Section | Cards |
|---|---|
| Badges | Shown only when a door is open (`visibility`) |
| Belysning | Tiles for lights, with a brightness slider for dimmable ones; a tap toggles. *Släck allt* turns all of them off after a confirmation |
| Klimat | A thermostat card with mode buttons (`heat`, `off`), and a tile with − / + for a second thermostat |
| Dörrar | Door sensors (open / closed) and a lock |
| Musik | A media control card (track, play/pause, next) and a tile with a volume slider |

## The same entities in IoT Open

Give the entities the label *IoT Open* (*Settings → Areas, labels & zones → Labels*, or on the entity) and turn on
mirroring in this integration. They then appear in the IoT Open installation and can be shown in the IoT Open
Dashboard:

| Home Assistant | IoT Open | IoT Open Dashboard widget |
|---|---|---|
| `light` (dimmable) | `dimmer` with an on/off topic | *Börvärde / nivå* or *Brytare* |
| `light`, `switch` | `switch` | *Brytare* |
| `climate` | four functions: temperature, set point, mode, action (0.6.0) | *Termostat* (0.12.0) |
| `binary_sensor` door | `door` | *Status* |
| `lock` | `lock`, 1 = locked, controllable | *Status* or *Brytare* |
| `media_player` | up to five functions: playback, volume, mute, status, power | *Media* |

With *Send values and receive commands via → Local broker*, both the wall display and the local IoT Open
dashboard keep working without internet.
