# coding=utf-8
from __future__ import absolute_import

__author__ = "Fizcko"
__license__ = "GNU Affero General Public License http://www.gnu.org/licenses/agpl.html"
__copyright__ = "Released under terms of the AGPLv3 License"

import asyncio
import json
import ssl
import threading
import time

import flask
import octoprint.plugin
import requests
import websockets
from websockets.exceptions import ConnectionClosed
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

REQUEST_TIMEOUT = (5, 10)  # (connect, read) seconds
WS_BACKOFF_MAX = 60
WS_PING_INTERVAL = 30


class PSUControl_HomeAssistant(octoprint.plugin.SettingsPlugin,
                         octoprint.plugin.AssetPlugin,
                         octoprint.plugin.TemplatePlugin,
                         octoprint.plugin.SimpleApiPlugin,
                         octoprint.plugin.StartupPlugin,
                         octoprint.plugin.ShutdownPlugin,
                         octoprint.plugin.RestartNeedingPlugin):

    def __init__(self):
        self.config = dict()
        self._state = None
        self._state_lock = threading.Lock()

        self._ws_thread = None
        self._ws_stop = threading.Event()
        self._ws_connected = False
        self._ws_msg_id = 0
        self._ws_loop = None

        self._last_rest_poll_ts = 0.0

    def get_settings_defaults(self):
        return dict(
            address = '',
            api_key = '',
            entity_id = '',
            verify_certificate = True,
            use_websocket = True,
            fallback_poll_interval = 30,
            custom_headers = [],
            legacy_import_done = False,
        )

    def on_settings_initialized(self):
        self._import_legacy_settings_once()
        self.reload_settings()

    def _import_legacy_settings_once(self):
        try:
            if self._settings.get_boolean(['legacy_import_done']):
                return

            legacy_path = ['plugins', 'psucontrol_homeassistant']
            # Read-only peek at legacy plugin's config in config.yaml
            legacy_keys = {
                'address': str,
                'api_key': str,
                'entity_id': str,
                'verify_certificate': bool,
            }

            imported = []
            for key, _type in legacy_keys.items():
                legacy_value = self._settings.global_get(legacy_path + [key])
                if legacy_value is None:
                    continue
                if _type is str and not str(legacy_value).strip():
                    continue

                # Don't overwrite if user already filled the field in the new plugin
                current = self._settings.get([key])
                default = self.get_settings_defaults()[key]
                already_set = current is not None and current != default and current != ''
                if already_set:
                    continue

                self._settings.set([key], legacy_value)
                imported.append(key)

            self._settings.set_boolean(['legacy_import_done'], True)
            self._settings.save()

            if imported:
                shown = ', '.join(k if k != 'api_key' else 'api_key(***)' for k in imported)
                self._logger.info("Imported settings from legacy 'psucontrol_homeassistant' plugin: {}".format(shown))
            else:
                self._logger.debug("No legacy psucontrol_homeassistant settings to import.")
        except Exception:
            self._logger.exception("Failed to import legacy psucontrol_homeassistant settings")

    def reload_settings(self):
        defaults = self.get_settings_defaults()
        for k, v in defaults.items():
            if isinstance(v, bool):
                v = self._settings.get_boolean([k])
            elif isinstance(v, int):
                v = self._settings.get_int([k])
            elif isinstance(v, float):
                v = self._settings.get_float([k])
            elif isinstance(v, list) or isinstance(v, dict):
                v = self._settings.get([k])
            else:
                v = self._settings.get([k])

            self.config[k] = v
            if k == 'api_key':
                self._logger.debug("{}: {}".format(k, '***' if v else ''))
            else:
                self._logger.debug("{}: {}".format(k, v))

    def on_startup(self, host, port):
        psucontrol_helpers = self._plugin_manager.get_helpers("psucontrol")
        if not psucontrol_helpers or 'register_plugin' not in psucontrol_helpers.keys():
            self._logger.warning("The version of PSUControl that is installed does not support plugin registration.")
            return

        self._logger.debug("Registering plugin with PSUControl")
        psucontrol_helpers['register_plugin'](self)

    def on_after_startup(self):
        self._logger.debug(
            "on_after_startup: use_websocket={} address_set={} api_key_set={} entity={}".format(
                self.config.get('use_websocket'),
                bool(self.config.get('address')),
                bool(self.config.get('api_key')),
                self.config.get('entity_id'),
            )
        )
        self._start_ws()

    def on_shutdown(self):
        self._stop_ws()

    # ------------------------------------------------------------------ helpers

    def _build_headers(self):
        headers = {'Authorization': 'Bearer ' + (self.config.get('api_key') or '')}
        for entry in (self.config.get('custom_headers') or []):
            try:
                name = (entry.get('name') or '').strip()
                value = entry.get('value')
                if name:
                    headers[name] = '' if value is None else str(value)
            except AttributeError:
                continue
        return headers

    def _resolved_entity_id(self):
        _entity_id = self.config.get('entity_id') or ''
        if '.' not in _entity_id:
            _entity_id = 'switch.' + _entity_id
        return _entity_id

    def _set_state(self, value, source):
        with self._state_lock:
            if self._state != value:
                self._logger.debug("State update from {}: {}".format(source, value))
            self._state = value

    # ------------------------------------------------------------------ REST

    def send(self, cmd, data=None):
        url = (self.config.get('address') or '') + '/api' + cmd
        headers = self._build_headers()
        verify_certificate = self.config.get('verify_certificate', True)

        response = None
        try:
            if data is not None:
                response = requests.post(url, headers=headers, json=data, verify=verify_certificate, timeout=REQUEST_TIMEOUT)
            else:
                response = requests.get(url, headers=headers, verify=verify_certificate, timeout=REQUEST_TIMEOUT)
        except (
                requests.exceptions.InvalidURL,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout
        ):
            self._logger.error("Unable to communicate with server. Check settings.")
        except Exception:
            self._logger.exception("Exception while making API call")
        else:
            if data:
                self._logger.debug("cmd={}, data={}, status_code={}".format(cmd, data, response.status_code))
            else:
                self._logger.debug("cmd={}, status_code={}".format(cmd, response.status_code))

            if response.status_code == 401:
                self._logger.warning("Server returned 401 Unauthorized. Check access token.")
                response = None
            elif response.status_code == 404:
                self._logger.warning("Server returned 404 Not Found. Check Entity ID.")
                response = None

        return response

    def _rest_refresh_state(self):
        entity = self._resolved_entity_id()
        cmd = '/states/' + entity
        self._logger.debug("REST fallback: GET /api{} (entity={})".format(cmd, entity))
        response = self.send(cmd)
        self._last_rest_poll_ts = time.monotonic()
        if not response:
            self._logger.debug("REST fallback: no usable response; keeping last known state.")
            return None
        try:
            data = response.json()
            raw_state = data.get('state')
            status = (raw_state == 'on')
        except (ValueError, KeyError):
            self._logger.error("Unable to parse HA state response.")
            return None
        self._logger.debug("REST fallback: entity={} raw_state={} -> {}".format(entity, raw_state, status))
        self._set_state(status, source='rest')
        return status

    # ------------------------------------------------------------------ PSUControl API

    def change_psu_state(self, state):
        _entity_id = self.config.get('entity_id') or ''
        _domainsplit = _entity_id.find('.')
        if _domainsplit < 0:
            _domain = 'switch'
            _entity_id = _domain + '.' + _entity_id
        else:
            _domain = _entity_id[:_domainsplit]
            if _domain == 'group':
                _domain = 'homeassistant'

        if state:
            cmd = '/services/' + _domain + '/turn_' + state
        else:
            cmd = '/services/' + _domain + '/toggle'
        data = {"entity_id": _entity_id}
        response = self.send(cmd, data)
        # optimistic update; WS push will confirm shortly
        if response is not None and state in ('on', 'off'):
            self._set_state(state == 'on', source='command')

    def turn_psu_on(self):
        self._logger.debug("Switching PSU On")
        self.change_psu_state('on')

    def turn_psu_off(self):
        self._logger.debug("Switching PSU Off")
        self.change_psu_state('off')

    def get_psu_state(self):
        # WS connected → state is kept live, return it directly (no request)
        if self._ws_connected:
            with self._state_lock:
                if self._state is not None:
                    self._logger.debug("get_psu_state: ws-live cache hit -> {}".format(self._state))
                    return self._state
            self._logger.debug("get_psu_state: WS connected but state unknown; falling back to REST.")

        # Fallback: REST poll, throttled by fallback_poll_interval
        interval = self.config.get('fallback_poll_interval') or 0
        now = time.monotonic()
        since = now - self._last_rest_poll_ts
        if self._state is not None and since < interval:
            with self._state_lock:
                self._logger.debug(
                    "get_psu_state: REST throttled ({:.1f}s since last poll, interval={}s) -> cached {}"
                    .format(since, interval, self._state)
                )
                return self._state

        self._logger.debug(
            "get_psu_state: REST fallback poll (ws_connected={}, since_last={:.1f}s, interval={}s)"
            .format(self._ws_connected, since, interval)
        )
        status = self._rest_refresh_state()
        if status is None:
            with self._state_lock:
                fallback = self._state if self._state is not None else False
            self._logger.debug("get_psu_state: REST failed, returning last-known {}".format(fallback))
            return fallback
        return status

    # ------------------------------------------------------------------ WebSocket

    def _ws_url(self):
        address = (self.config.get('address') or '').rstrip('/')
        if address.startswith('https://'):
            return 'wss://' + address[len('https://'):] + '/api/websocket'
        if address.startswith('http://'):
            return 'ws://' + address[len('http://'):] + '/api/websocket'
        return None

    def _start_ws(self):
        if not self.config.get('use_websocket'):
            self._logger.info("WebSocket mode disabled; using REST polling fallback.")
            return
        if not self.config.get('address') or not self.config.get('api_key') or not self.config.get('entity_id'):
            self._logger.debug("WS not started: settings incomplete (address/api_key/entity_id).")
            return
        if self._ws_thread and self._ws_thread.is_alive():
            self._logger.debug("WS worker already running; start requested no-op.")
            return

        self._logger.debug("Starting WS worker thread (asyncio/websockets).")
        self._ws_stop.clear()
        self._ws_thread = threading.Thread(target=self._ws_thread_main, name="PSUControl-HA-WS", daemon=True)
        self._ws_thread.start()

    def _stop_ws(self):
        self._logger.debug("Stopping WS worker thread.")
        self._ws_stop.set()
        loop = self._ws_loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError as e:
                self._logger.debug("WS loop stop raised: {}".format(e))
        self._ws_connected = False

    def _ws_thread_main(self):
        loop = asyncio.new_event_loop()
        self._ws_loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_supervisor())
        except Exception:
            self._logger.exception("WS thread crashed unexpectedly")
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._ws_loop = None
            self._logger.debug("WS thread terminated.")

    async def _ws_supervisor(self):
        backoff = 1
        while not self._ws_stop.is_set():
            url = self._ws_url()
            if not url:
                self._logger.warning("Cannot derive WebSocket URL from address.")
                return
            try:
                await self._ws_session(url)
                backoff = 1  # session ended cleanly
            except asyncio.CancelledError:
                self._logger.debug("WS session cancelled.")
                break
            except Exception as e:
                self._logger.warning("WebSocket error: {}".format(e))
                self._logger.debug("WebSocket error detail", exc_info=True)
            finally:
                self._ws_connected = False

            if self._ws_stop.is_set():
                self._logger.debug("WS supervisor: stop flag set, exiting.")
                break
            self._logger.debug("WS reconnecting in {}s (backoff)".format(backoff))
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            backoff = min(backoff * 2, WS_BACKOFF_MAX)

    async def _ws_session(self, url):
        verify = self.config.get('verify_certificate', True)
        self._logger.debug("WS connect attempt: url={} verify={} timeout={}s".format(
            url, verify, REQUEST_TIMEOUT[0]
        ))

        ssl_ctx = None
        if url.startswith('wss://'):
            ssl_ctx = ssl.create_default_context()
            if not verify:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                self._logger.debug("WS TLS verification disabled.")

        extra_headers = {}
        for k, v in self._build_headers().items():
            if k.lower() == 'authorization':
                continue  # HA WS uses in-protocol auth
            extra_headers[k] = v
        if extra_headers:
            self._logger.debug("WS extra upgrade headers: {}".format(list(extra_headers.keys())))

        connect_kwargs = dict(
            ssl=ssl_ctx,
            open_timeout=REQUEST_TIMEOUT[0],
            ping_interval=WS_PING_INTERVAL,
            ping_timeout=WS_PING_INTERVAL / 2,
            close_timeout=5,
            max_size=None,
        )
        # websockets 13+ renamed extra_headers → additional_headers
        try:
            ws = await websockets.connect(url, additional_headers=extra_headers, **connect_kwargs)
        except TypeError:
            ws = await websockets.connect(url, extra_headers=extra_headers, **connect_kwargs)

        try:
            self._logger.debug("WS socket established; starting handshake.")
            if not await self._ws_authenticate(ws):
                raise RuntimeError("WS authentication failed")

            self._logger.debug("WS authenticated; performing REST state resync before subscribe.")
            self._rest_refresh_state()
            if not await self._ws_subscribe(ws):
                raise RuntimeError("WS subscription failed")

            self._ws_connected = True
            self._logger.info("WebSocket connected and subscribed for {}".format(self._resolved_entity_id()))
            await self._ws_read_loop(ws)
        finally:
            try:
                await ws.close()
                self._logger.debug("WS socket closed.")
            except Exception as e:
                self._logger.debug("WS close raised: {}".format(e))

    async def _ws_recv_msg(self, ws):
        raw = await ws.recv()
        msg = json.loads(raw)
        if isinstance(msg, dict):
            self._logger.debug("WS recv: id={} type={} success={}".format(
                msg.get('id'), msg.get('type'), msg.get('success')
            ))
        return msg

    async def _ws_send_msg(self, ws, payload):
        ptype = payload.get('type') if isinstance(payload, dict) else '?'
        pid = payload.get('id') if isinstance(payload, dict) else None
        if ptype == 'auth':
            self._logger.debug("WS send: type=auth (token hidden)")
        else:
            self._logger.debug("WS send: id={} type={}".format(pid, ptype))
        await ws.send(json.dumps(payload))

    def _ws_next_id(self):
        self._ws_msg_id += 1
        return self._ws_msg_id

    async def _ws_authenticate(self, ws):
        msg = await self._ws_recv_msg(ws)
        if not msg or msg.get('type') != 'auth_required':
            self._logger.error("Unexpected WS handshake: {}".format(msg))
            return False
        ha_version = msg.get('ha_version')
        if ha_version:
            self._logger.debug("WS handshake: HA version {}".format(ha_version))
        await self._ws_send_msg(ws, {"type": "auth", "access_token": self.config.get('api_key') or ''})
        msg = await self._ws_recv_msg(ws)
        if not msg or msg.get('type') != 'auth_ok':
            self._logger.error("WS auth failed: {}".format(msg))
            return False
        self._logger.debug("WS auth_ok received.")
        return True

    async def _ws_subscribe(self, ws):
        sub_id = self._ws_next_id()
        entity = self._resolved_entity_id()
        self._logger.debug("WS subscribe_entities: id={} entity={} (server-side filter)".format(sub_id, entity))
        await self._ws_send_msg(ws, {
            "id": sub_id,
            "type": "subscribe_entities",
            "entity_ids": [entity],
        })
        msg = await self._ws_recv_msg(ws)
        if not msg or not msg.get('success'):
            self._logger.error("WS subscribe failed: {}".format(msg))
            return False
        self._logger.debug("WS subscribe ack: id={}".format(sub_id))
        return True

    async def _ws_read_loop(self, ws):
        while not self._ws_stop.is_set():
            try:
                msg = await self._ws_recv_msg(ws)
            except ConnectionClosed as e:
                self._logger.debug("WS closed by peer: code={} reason={}".format(e.code, e.reason))
                raise
            mtype = msg.get('type')
            if mtype == 'event':
                self._handle_event(msg.get('event', {}))
            elif mtype == 'result' and not msg.get('success', True):
                self._logger.warning("WS error result: {}".format(msg))
            else:
                self._logger.debug("WS unhandled frame: type={} id={}".format(mtype, msg.get('id')))

    def _handle_event(self, event):
        # subscribe_entities compressed-state event shapes:
        #   initial: {"a": {entity_id: {"s": state, "a": attrs, "c": ctx, "lc": t, "lu": t}}}
        #   change:  {"c": {entity_id: {"+": {"s": state, ...}, "-": [...]}}}
        #   removal: {"r": [entity_id, ...]}
        target = self._resolved_entity_id()

        added = event.get('a')
        if isinstance(added, dict) and target in added:
            raw = (added[target] or {}).get('s')
            self._logger.debug("WS event (initial): entity={} raw_state={}".format(target, raw))
            self._set_state(raw == 'on', source='ws-init')
            return

        changed = event.get('c')
        if isinstance(changed, dict) and target in changed:
            entry = changed[target] or {}
            plus = entry.get('+') if isinstance(entry, dict) else None
            if isinstance(plus, dict) and 's' in plus:
                raw = plus.get('s')
                self._logger.debug("WS event (change): entity={} raw_state={}".format(target, raw))
                self._set_state(raw == 'on', source='ws')
            else:
                self._logger.debug("WS event (change) without state field: entity={} entry={}".format(target, entry))
            return

        removed = event.get('r')
        if isinstance(removed, list) and target in removed:
            self._logger.warning("WS event: target entity {} was removed from HA.".format(target))
            return

        self._logger.debug("WS event ignored (no matching keys for {}): keys={}".format(target, list(event.keys())))

    # ------------------------------------------------------------------ settings

    def on_settings_save(self, data):
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        self.reload_settings()
        # restart WS to apply new settings
        self._stop_ws()
        self._start_ws()

    def get_settings_version(self):
        return 3

    def on_settings_migrate(self, target, current=None):
        # Actual legacy import runs in on_settings_initialized via the
        # legacy_import_done flag; this hook is kept as a no-op marker.
        pass

    def get_template_configs(self):
        return [
            dict(type="settings", custom_bindings=False)
        ]

    def is_template_autoescaped(self):
        # Template contains only static HTML + Knockout data-bindings, no Jinja
        # expressions producing markup. Enabling autoescape satisfies OctoPrint
        # 1.13's global requirement without any visible change.
        return True

    # ------------------------------------------------------------------ API (test button)

    def get_api_commands(self):
        return {"test": []}

    def is_api_protected(self):
        return True

    def on_api_command(self, command, data):
        if command == "test":
            result = self._run_diagnostics()
            return flask.jsonify(result)

    def get_assets(self):
        return {"js": ["js/psucontrol_hass_ws.js"]}

    def _run_diagnostics(self):
        checks = []

        address = (self.config.get('address') or '').strip()
        api_key = (self.config.get('api_key') or '').strip()
        entity = (self.config.get('entity_id') or '').strip()

        if not address:
            checks.append({"name": "Configuration", "ok": False, "detail": "Address is empty"})
        if not api_key:
            checks.append({"name": "Configuration", "ok": False, "detail": "Access token is empty"})
        if not entity:
            checks.append({"name": "Configuration", "ok": False, "detail": "Entity ID is empty"})
        if any(not c["ok"] for c in checks):
            return {"ok": False, "checks": checks}

        # 1. REST reachability + auth
        try:
            url = address.rstrip('/') + '/api/'
            headers = self._build_headers()
            r = requests.get(url, headers=headers,
                             verify=self.config.get('verify_certificate', True),
                             timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                checks.append({"name": "REST /api/", "ok": True, "detail": "HTTP 200 (auth OK)"})
            elif r.status_code == 401:
                checks.append({"name": "REST /api/", "ok": False, "detail": "HTTP 401 — check access token"})
            else:
                checks.append({"name": "REST /api/", "ok": False, "detail": "HTTP {}".format(r.status_code)})
        except requests.exceptions.Timeout:
            checks.append({"name": "REST /api/", "ok": False, "detail": "Timeout"})
        except Exception as e:
            checks.append({"name": "REST /api/", "ok": False, "detail": "{}: {}".format(type(e).__name__, e)})

        # 2. Entity exists
        try:
            resolved = self._resolved_entity_id()
            r = self.send('/states/' + resolved)
            if r is not None and r.status_code == 200:
                current_state = r.json().get('state', '?')
                checks.append({"name": "Entity", "ok": True,
                               "detail": "{} — current state: {}".format(resolved, current_state)})
            else:
                checks.append({"name": "Entity", "ok": False,
                               "detail": "Not found or inaccessible: {}".format(resolved)})
        except Exception as e:
            checks.append({"name": "Entity", "ok": False, "detail": "{}: {}".format(type(e).__name__, e)})

        # 3. WebSocket connect + auth + subscribe
        try:
            ok, detail = asyncio.run(self._ws_diagnostic())
            checks.append({"name": "WebSocket", "ok": ok, "detail": detail})
        except Exception as e:
            checks.append({"name": "WebSocket", "ok": False, "detail": "{}: {}".format(type(e).__name__, e)})

        overall = all(c["ok"] for c in checks)
        return {"ok": overall, "checks": checks}

    async def _ws_diagnostic(self):
        url = self._ws_url()
        if not url:
            return False, "Cannot derive WS URL from address (must start with http:// or https://)"

        verify = self.config.get('verify_certificate', True)
        ssl_ctx = None
        if url.startswith('wss://'):
            ssl_ctx = ssl.create_default_context()
            if not verify:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE

        extra_headers = {k: v for k, v in self._build_headers().items() if k.lower() != 'authorization'}

        connect_kwargs = dict(
            ssl=ssl_ctx,
            open_timeout=REQUEST_TIMEOUT[0],
            close_timeout=5,
            max_size=None,
        )

        try:
            try:
                ws = await websockets.connect(url, additional_headers=extra_headers, **connect_kwargs)
            except TypeError:
                ws = await websockets.connect(url, extra_headers=extra_headers, **connect_kwargs)
        except Exception as e:
            return False, "Connect failed: {}: {}".format(type(e).__name__, e)

        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if msg.get('type') != 'auth_required':
                return False, "Unexpected handshake: {}".format(msg)
            ha_version = msg.get('ha_version', 'unknown')

            await ws.send(json.dumps({"type": "auth",
                                      "access_token": self.config.get('api_key') or ''}))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if msg.get('type') != 'auth_ok':
                return False, "Auth failed: {}".format(msg)

            sub_id = 999
            await ws.send(json.dumps({
                "id": sub_id,
                "type": "subscribe_entities",
                "entity_ids": [self._resolved_entity_id()],
            }))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if not msg.get('success'):
                return False, "Subscribe failed: {}".format(msg)

            return True, "Connected, authenticated, subscribed (HA {})".format(ha_version)
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    def get_update_information(self):
        return dict(
            psucontrol_hass_ws=dict(
                displayName="PSU Control - Home Assistant (WS)",
                displayVersion=self._plugin_version,

                type="github_release",
                user="fizcko",
                repo="OctoPrint-PSUControl-HomeAssistant-WS",
                current=self._plugin_version,

                pip="https://github.com/fizcko/OctoPrint-PSUControl-HomeAssistant-WS/archive/{target_version}.zip"
            )
        )


__plugin_name__ = "PSU Control - Home Assistant (WS)"
__plugin_pythoncompat__ = ">=3,<4"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = PSUControl_HomeAssistant()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
    }
