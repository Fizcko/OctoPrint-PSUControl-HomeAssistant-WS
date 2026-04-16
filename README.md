> [!NOTE]
> This is a fork of the [original (unmaintained) plugin](https://github.com/edekeijzer/OctoPrint-PSUControl-HomeAssistant). It introduces a WebSocket subscription to Home Assistant so the PSU state is pushed live instead of polled, with a throttled REST fallback and support for custom HTTP/WS headers. The plugin identifier has been renamed to `psucontrol_hass_ws`.

# OctoPrint PSU Control - Home Assistant (WS)
Adds Home Assistant support to OctoPrint-PSUControl as a sub-plugin, using a persistent WebSocket subscription for near-zero-latency state updates.

## Setup
- Install the plugin using Plugin Manager from Settings (or via the release zip)
- Configure this plugin (see below)
- In [PSU Control](https://github.com/kantlivelong/OctoPrint-PSUControl), select this plugin as both **Switching** and **Sensing** method (plugin id: `psucontrol_hass_ws`)
- :warning: **Turn off** the *Automatically turn PSU ON* option in the PSU Control settings, leaving this on will ruin your prints when Home Assistant becomes unavailable :warning: (some explanation in tickets
[#4](https://github.com/edekeijzer/OctoPrint-PSUControl-HomeAssistant/issues/4),
[#11](https://github.com/edekeijzer/OctoPrint-PSUControl-HomeAssistant/issues/11),
[#16](https://github.com/edekeijzer/OctoPrint-PSUControl-HomeAssistant/issues/16))

## Configuration
* **Address** — URL of your Home Assistant installation (e.g. `https://ha.local:8123`). No trailing slash.
* **Access token** — Go to your Home Assistant profile → *Long-Lived Access Tokens* → *Create Token*, then paste it here.
* **Entity ID** — The entity you want to control (e.g. `switch.my_smart_outlet`). Any entity supporting `turn_on`/`turn_off` works; `group` entities are handled via the `homeassistant` domain.
* **Use WebSocket** — When enabled (default), the plugin opens a persistent WebSocket to HA and subscribes to state changes. `get_psu_state()` then returns the last pushed state with no HTTP traffic.
* **Fallback poll interval** — If the WebSocket is down or disabled, REST calls to `/api/states/<entity>` are throttled to this interval (seconds). Between polls, the last known state is returned.
* **Custom headers** — Extra headers injected into every REST request **and** the WebSocket upgrade. Useful for reverse-proxy auth (e.g. Cloudflare Access `CF-Access-Client-Id`/`Secret`). The `Authorization: Bearer …` header is managed by the plugin.
* **Verify certificate** — Uncheck if HA uses a self-signed TLS certificate (applies to both REST and WS).

## How it works
1. On startup the plugin does **one** REST `GET /api/states/<entity>` to seed state.
2. It opens `wss://<ha>/api/websocket` (via the [`websockets`](https://pypi.org/project/websockets/) library — supports `permessage-deflate` natively), authenticates with the long-lived token, and sends `subscribe_entities` for the configured entity (server-side filtering).
3. HA pushes `event` messages on every change; the plugin updates its in-memory state and `get_psu_state()` returns it without any network I/O.
4. Commands (`turn_on`/`turn_off`) still go out over REST `/api/services/...`; the confirmation arrives through the WS push.
5. On WS disconnect, an exponential backoff reconnect loop runs (1 s → 60 s). While disconnected, `get_psu_state()` falls back to REST at most once per *Fallback poll interval*.

## Testing the configuration
A **Test connection** button is provided in the settings dialog. It runs four checks against your **saved** settings and reports each one individually:

1. **Configuration** — non-empty address, token and entity id.
2. **REST /api/** — HTTP reachability and access-token validity (detects `401`).
3. **Entity** — that the configured entity exists in HA; reports its current state.
4. **WebSocket** — full handshake: TLS connect, `auth`, and `subscribe_entities`. Reports the HA version it negotiated with.

Save the form before clicking the button — the test uses the persisted values, not the in-form ones.

## Build
A helper script is provided to produce an installable zip:

```bash
./build.sh
```

It auto-increments the patch digit in `VERSION`, stages the plugin tree, strips caches, and writes `dist/OctoPrint-PSUControl-HomeAssistant-WS-<version>.zip`. Install it via OctoPrint's Plugin Manager → *Get More...* → *...from URL* or *...from an uploaded file*.

## Support
Please check your logs first. If they do not explain your issue, open an issue on GitHub. Set `octoprint.plugins.psucontrol` and `octoprint.plugins.psucontrol_hass_ws` to **DEBUG** and include the relevant logs.

## Credits
Originally written by [Erik de Keijzer](https://github.com/edekeijzer/OctoPrint-PSUControl-HomeAssistant). Forked and extended (WebSocket subscription, custom headers, legacy-settings import, Test-connection button, build script).
