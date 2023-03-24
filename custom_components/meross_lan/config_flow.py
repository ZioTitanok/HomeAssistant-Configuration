"""Config flow for Meross LAN integration."""
from __future__ import annotations
import typing
from time import time
from logging import DEBUG
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowHandler, AbortFlow
from homeassistant.const import (
    CONF_PASSWORD, CONF_USERNAME, CONF_ERROR,
)
from homeassistant.helpers.typing import DiscoveryInfoType
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .merossclient import (
    const as mc,
    KeyType,
    MerossDeviceDescriptor,
    MerossKeyError,
    get_productnametype,
)
from .merossclient.httpclient import MerossHttpClient
from .merossclient.cloudapi import (
    MerossApiError,
    async_get_cloud_key,
)

from . import MerossApi
from .helpers import LOGGER
from .const import (
    DOMAIN,
    CONF_HOST, CONF_DEVICE_ID, CONF_KEY, CONF_CLOUD_KEY,
    CONF_PAYLOAD, CONF_TIMESTAMP,
    CONF_PROTOCOL, CONF_PROTOCOL_OPTIONS,
    CONF_POLLING_PERIOD, CONF_POLLING_PERIOD_DEFAULT,
    CONF_TRACE, CONF_TRACE_TIMEOUT, CONF_TRACE_TIMEOUT_DEFAULT,
)

# helper conf keys not persisted to config
CONF_DEVICE_TYPE = 'device_type'
DESCR = 'suggested_value'
ERR_BASE = 'base'
ERR_CANNOT_CONNECT = 'cannot_connect'
ERR_INVALID_KEY = 'invalid_key'
ERR_INVALID_NULL_KEY = 'invalid_nullkey'
ERR_DEVICE_ID_MISMATCH = 'device_id_mismatch'
ERR_ALREADY_CONFIGURED_DEVICE = 'already_configured_device'
ERR_INVALID_AUTH = 'invalid_auth'


async def _http_discovery(hass, host: str, key: KeyType) -> dict[str, object]:
    # passing key=None would allow key-hack and we don't want it aymore
    c = MerossHttpClient(host, key or '', async_get_clientsession(hass), LOGGER)
    payload = (await c.async_request_strict_get(mc.NS_APPLIANCE_SYSTEM_ALL))[mc.KEY_PAYLOAD]
    payload.update((await c.async_request_strict_get(mc.NS_APPLIANCE_SYSTEM_ABILITY))[mc.KEY_PAYLOAD])
    return {
        CONF_HOST: host,
        CONF_PAYLOAD: payload,
        CONF_KEY: key
    }


class ConfigError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class MerossFlowHandlerMixin(FlowHandler if typing.TYPE_CHECKING else object):
    """ Mixin providing cloud key retrieval for both Config and Option flows"""
    _device_id: str | None = None
    _host: str | None = None
    _key: str | None = None
    _cloud_key: str | None = None
    _placeholders = {
        CONF_DEVICE_TYPE: '',
        CONF_DEVICE_ID: '',
    }

    def show_keyerror(self):
        return self.async_show_menu(
            step_id="keyerror",
            menu_options=["cloudkey", "device"]
        )

    async def async_step_cloudkey(self, user_input=None):
        """ manage the cloud login form to retrieve the device key"""
        errors = {}

        if user_input:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            try:
                self._cloud_key = await async_get_cloud_key(
                    username, password, async_get_clientsession(self.hass))
                self._key = self._cloud_key
                return await self.async_step_device() # type: ignore
            except MerossApiError as error:
                errors[CONF_ERROR] = ERR_INVALID_AUTH
                _err = error.reason
            except Exception as error:
                errors[CONF_ERROR] = ERR_CANNOT_CONNECT
                _err = str(error) or type(error).__name__

            return self.async_show_form(
                step_id="cloudkey",
                data_schema=vol.Schema({
                        vol.Required(CONF_USERNAME, description={ DESCR: username }): str,
                        vol.Required(CONF_PASSWORD, description={ DESCR: password }): str,
                        vol.Optional(CONF_ERROR, description={ DESCR: _err }): str
                        }),
                errors=errors
            )

        return self.async_show_form(
            step_id="cloudkey",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str
            })
        )


class ConfigFlow(MerossFlowHandlerMixin, config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Meross IoT local LAN."""
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    _discovery_info: dict[str, object] | None = None

    @staticmethod
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None):
        if (api := MerossApi.peek(self.hass)) is not None:
            if api.cloud_key is not None:
                self._key = api.cloud_key
                self._cloud_key = api.cloud_key
        return await self.async_step_device()

    async def async_step_hub(self, user_input=None):
        """ configure the MQTT discovery device key"""
        if user_input is None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            config_schema = { vol.Optional(CONF_KEY): str }
            return self.async_show_form(step_id="hub", data_schema=vol.Schema(config_schema))
        return self.async_create_entry(title="MQTT Hub", data=user_input)

    async def async_step_device(self, user_input=None):
        """ common device configuration"""
        errors = {}

        if user_input is not None:
            self._host = user_input[CONF_HOST]
            self._key = user_input.get(CONF_KEY)
            try:
                _discovery_info = await _http_discovery(self.hass, self._host, self._key)
                await self._async_set_info(_discovery_info)
                return self.show_finalize()
            except ConfigError as error:
                errors[ERR_BASE] = error.reason
            except MerossKeyError:
                self._cloud_key = None
                return self.show_keyerror()
            except AbortFlow:
                errors[ERR_BASE] = ERR_ALREADY_CONFIGURED_DEVICE
            except Exception as error:
                LOGGER.warning("Error (%s) configuring meross device (host:%s)", str(error), self._host)
                errors[ERR_BASE] = ERR_CANNOT_CONNECT

        config_schema = {
            vol.Required(CONF_HOST, description={ DESCR: self._host}): str,
            vol.Optional(CONF_KEY, description={ DESCR: self._key}): str,
        }
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(config_schema),
            errors=errors,
            description_placeholders=self._placeholders
        )

    async def async_step_discovery(self, discovery_info: DiscoveryInfoType):
        """
        this is actually the entry point for devices discovered through our mqtt hub
        """
        await self._async_set_info(discovery_info)
        return self.show_finalize()

    async def async_step_dhcp(self, discovery_info):
        """Handle a flow initialized by DHCP discovery."""
        if LOGGER.isEnabledFor(DEBUG):
            LOGGER.debug("received dhcp discovery: %s", str(discovery_info))
        self._host = discovery_info.ip
        macaddress = discovery_info.macaddress.replace(':', '').lower()
        # check if the device is already registered
        try:
            entries = self.hass.config_entries
            for entry in entries.async_entries(DOMAIN):
                descriptor = MerossDeviceDescriptor(entry.data.get(CONF_PAYLOAD, {}))
                if descriptor.macAddress.replace(':', '').lower() != macaddress:
                    continue
                data = dict(entry.data) # deepcopy? not needed: see CONF_TIMESTAMP
                data[CONF_HOST] = self._host
                data[CONF_TIMESTAMP] = time() # force ConfigEntry update..
                entries.async_update_entry(entry, data=data)
                LOGGER.info("DHCP updated device ip address (%s) for device %s", self._host, descriptor.uuid)
                return self.async_abort(reason='already_configured')
        except Exception as error:
            LOGGER.warning("DHCP update internal error: %s", str(error))
        # we'll update the unique_id for the flow when we'll have the device_id
        # Here this is needed in case we cannot correctly identify the device
        # via our api and the dhcp integration keeps pushing us discoveries for
        # the same device
        # update 2022-12-19: adding DOMAIN prefix since macaddress alone might be set by other
        # integrations and that would conflict with our unique_id likely raising issues
        # on DHCP discovery not working in some configurations
        await self.async_set_unique_id(DOMAIN + macaddress, raise_on_progress=True)
        # Check we already dont have the device registered.
        # This is a legacy (dead code) check since we've already
        # looped through our config entries and updated the ip address there
        api = MerossApi.peek(self.hass)
        if api is not None:
            if api.get_device_with_mac(macaddress) is not None:
                return self.async_abort(reason='already_configured')
        try:
            # try device identification so the user/UI has a good context to start with
            if api is not None:
                # we'll see if any previous device already used a 'cloud_key' retrieved
                # from meross api for cloud-paired devices and try it
                _discovery_info = None
                if api.cloud_key is not None:
                    try:
                        _discovery_info = await _http_discovery(self.hass, self._host, api.cloud_key)
                        self._key = api.cloud_key
                        self._cloud_key = api.cloud_key # pass along so we'll save it in this entry too
                    except MerossKeyError:
                        pass
                if (_discovery_info is None) and (api.key is not None) and (api.key != api.cloud_key):
                    try:
                        _discovery_info = await _http_discovery(self.hass, self._host, api.key)
                        self._key = api.key
                    except MerossKeyError:
                        pass
                if _discovery_info is not None:
                    await self._async_set_info(_discovery_info) # type: ignore

            # we're now skipping key-hack discovery since devices on recent firmware
            # look like they really hate this hack...
            # if _discovery_info is None:
            #     self._key = None # no other options: try empty key (which will eventually use the reply-hack)
            #     _discovery_info = await self._http_discovery()
            # await self._async_set_info(_discovery_info)
        except Exception as error:
            if LOGGER.isEnabledFor(DEBUG):
                LOGGER.debug("Error (%s) identifying meross device (host:%s)", str(error), self._host)
            if isinstance(error, AbortFlow):
                # we might have 'correctly' identified an already configured entry
                return self.async_abort(reason='already_configured')
            # forgive and continue if we cant discover the device...let the user work it out

        return await self.async_step_device()

    async def async_step_mqtt(self, discovery_info):
        """manage the MQTT discovery flow"""
        await self.async_set_unique_id(DOMAIN)
        # this entry should only ever called once after startup
        # when HA thinks we're interested in discovery.
        # If our MerossApi is already running it will manage the discovery itself
        # so this flow is only useful when MerossLan has no configuration yet
        # and we leverage the default mqtt discovery to setup our manager
        api = MerossApi.get(self.hass)
        if api.mqtt_is_subscribed():
            return self.async_abort(reason='already_configured')
        # try setup the mqtt subscription
        # this call might not register because of errors or because of an overlapping
        # request from 'async_setup_entry' (we're preventing overlapped calls to MQTT
        # subscription)
        await api.async_mqtt_register()
        if api.mqtt_is_subscribed():
            # ok, now pass along the discovering mqtt message so our MerossApi state machine
            # gets to work on this
            await api.async_mqtt_receive(discovery_info)
        # just in case, setup the MQTT Hub entry to enable the (default) device key configuration
        # if the entry hub is already configured this will disable the discovery
        # subscription (by returning 'already_configured') stopping any subsequent async_step_mqtt message:
        # our MerossApi should already be in place
        return await self.async_step_hub()

    def show_finalize(self):
        """just a recap form"""
        return self.async_show_form(
            step_id="finalize",
            data_schema=vol.Schema({}),
            description_placeholders=self._placeholders
        )

    async def async_step_finalize(self, user_input=None):
        return self.async_create_entry(
            title=f"{self._descriptor.type} {self._device_id}",
            data=self._discovery_info # type: ignore
        )

    async def _async_set_info(self, discovery_info: dict):
        self._discovery_info = discovery_info
        self._descriptor = MerossDeviceDescriptor(discovery_info.get(CONF_PAYLOAD, {}))
        self._device_id = self._descriptor.uuid
        self._placeholders = {
            CONF_DEVICE_TYPE: get_productnametype(self._descriptor.type),
            CONF_DEVICE_ID: self._device_id,
        }
        self.context["title_placeholders"] = self._placeholders
        await self.async_set_unique_id(self._device_id)
        self._abort_if_unique_id_configured()

        if CONF_DEVICE_ID not in discovery_info:#this is coming from manual user entry or dhcp discovery
            discovery_info[CONF_DEVICE_ID] = self._device_id

        if (self._cloud_key is not None) and (self._cloud_key == self._key):
            # save (only if good) so we can later automatically retrieve for new devices
            discovery_info[CONF_CLOUD_KEY] = self._cloud_key
        else:
            discovery_info.pop(CONF_CLOUD_KEY, None)


class OptionsFlowHandler(MerossFlowHandlerMixin, config_entries.OptionsFlow):
    """
        Manage device options configuration
    """
    def __init__(self, config_entry: config_entries.ConfigEntry):
        self._config_entry = config_entry
        if config_entry.unique_id != DOMAIN:
            data = config_entry.data
            self._device_id = data.get(CONF_DEVICE_ID)
            self._host = data.get(CONF_HOST) # null for devices discovered over mqtt
            self._key = data.get(CONF_KEY)
            self._cloud_key = data.get(CONF_CLOUD_KEY) # null for non cloud keys
            self._protocol = data.get(CONF_PROTOCOL)
            self._polling_period = data.get(CONF_POLLING_PERIOD)
            self._trace = data.get(CONF_TRACE, 0) > time()
            self._trace_timeout = data.get(CONF_TRACE_TIMEOUT)
            self._placeholders = {
                CONF_DEVICE_ID: self._device_id,
                CONF_HOST: self._host or "MQTT"
            }

    async def async_step_init(self, user_input=None):
        if self._config_entry.unique_id == DOMAIN:
            return await self.async_step_hub(user_input)
        return await self.async_step_device(user_input)

    async def async_step_hub(self, user_input=None):

        if user_input is not None:
            data = dict(self._config_entry.data)
            data[CONF_KEY] = user_input.get(CONF_KEY)
            self.hass.config_entries.async_update_entry(self._config_entry, data=data)
            return self.async_create_entry(title="", data=None) # type: ignore

        config_schema = {
            vol.Optional(
                CONF_KEY,
                description={ DESCR: self._config_entry.data.get(CONF_KEY) }
                ): str
        }
        return self.async_show_form(step_id="hub", data_schema=vol.Schema(config_schema))

    async def async_step_device(self, user_input=None):
        """
        general (common) device configuration allowing key set and
        general parameters to be entered/modified
        """
        errors = {}
        device = MerossApi.peek_device(self.hass, self._device_id)
        if user_input is not None:
            self._host = user_input.get(CONF_HOST)
            self._key = user_input.get(CONF_KEY)
            self._protocol = user_input.get(CONF_PROTOCOL)
            self._polling_period = user_input.get(CONF_POLLING_PERIOD)
            self._trace = user_input.get(CONF_TRACE)
            self._trace_timeout = user_input.get(CONF_TRACE_TIMEOUT, CONF_TRACE_TIMEOUT_DEFAULT)
            try:
                if self._host is not None:
                    _discovery_info = await _http_discovery(self.hass, self._host, self._key)
                    _descriptor = MerossDeviceDescriptor(_discovery_info.get(CONF_PAYLOAD, {})) # type: ignore
                    if self._device_id != _descriptor.uuid:
                        raise ConfigError(ERR_DEVICE_ID_MISMATCH)

                data = dict(self._config_entry.data)
                if self._host is not None:
                    data[CONF_HOST] = self._host
                    if self._cloud_key and (self._cloud_key == self._key):
                        data[CONF_CLOUD_KEY] = self._cloud_key
                    else:
                        data.pop(CONF_CLOUD_KEY, None)
                data[CONF_KEY] = self._key
                data[CONF_PROTOCOL] = self._protocol
                data[CONF_POLLING_PERIOD] = self._polling_period
                data[CONF_TRACE] = (time() + self._trace_timeout) if self._trace else 0
                data[CONF_TRACE_TIMEOUT] = self._trace_timeout
                if device is not None:
                    try:
                        device.entry_option_update(user_input)
                    except:
                        pass # forgive any error
                # we're not following HA 'etiquette' and we're just updating the
                # config_entry data with this dirty trick
                self.hass.config_entries.async_update_entry(self._config_entry, data=data)
                # return None in data so the async_update_entry is not called for the
                # options to be updated
                return self.async_create_entry(title=None, data=None) # type: ignore

            except MerossKeyError:
                self._cloud_key = None
                return self.show_keyerror()
            except ConfigError as error:
                errors[ERR_BASE] = error.reason
            except Exception:
                errors[ERR_BASE] = ERR_CANNOT_CONNECT

        config_schema = {}
        if self._host is not None:
            config_schema[
                vol.Required(
                    CONF_HOST,
                    description={ DESCR: self._host}
                )] = str
        config_schema[
            vol.Optional(
                CONF_KEY,
                description={ DESCR: self._key}
            )] = str
        config_schema[
            vol.Optional(
                CONF_PROTOCOL,
                description={ DESCR: self._protocol}
            )] = vol.In(CONF_PROTOCOL_OPTIONS.keys())
        config_schema[
            vol.Optional(
                CONF_POLLING_PERIOD,
                default=CONF_POLLING_PERIOD_DEFAULT, # type: ignore
                description={ DESCR: self._polling_period}
            )] = cv.positive_int
        # setup device specific config right before last option
        if device is not None:
            self._placeholders[CONF_DEVICE_TYPE] = get_productnametype(device.descriptor.type)
            try:
                device.entry_option_setup(config_schema)
            except:
                pass # forgive any error

        config_schema[
            vol.Optional(
                CONF_TRACE,
                # CONF_TRACE contains the trace 'end' time epoch if set
                description={ DESCR: self._trace}
            )] = bool
        config_schema[
            vol.Optional(
                CONF_TRACE_TIMEOUT,
                default=CONF_TRACE_TIMEOUT_DEFAULT, # type: ignore
                description={ DESCR: self._trace_timeout}
            )] = cv.positive_int

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(config_schema),
            description_placeholders=self._placeholders,
            errors=errors
        )