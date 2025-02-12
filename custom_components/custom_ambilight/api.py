"""API module for Custom Ambilight integration."""

import asyncio
from base64 import b64decode
import logging
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
import httpx

from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_EFFECT, ATTR_HS_COLOR

from .effects import EFFECTS

_LOGGER = logging.getLogger(__name__)
# Define the rate limit (in seconds)
RATE_LIMIT = 0.1


class MyApi:
    """The Custom Ambilight API."""

    def __init__(self, host: str, connection_type: str, username: str = None, password: str = None) -> None:
        """Initialise the API."""
        self.host = host
        self.connection_type = connection_type
        self.username = username
        self.password = password
        self.url = f"{connection_type}://{host}:1926/6" if connection_type == "https" else f"http://{host}:1925/6"
        self.client = httpx.AsyncClient(
            auth=httpx.DigestAuth(username, password) if connection_type == "https" else None, verify=False
        )
        self.EFFECTS = EFFECTS
        self.previous_state = None
        self._data = {}

    async def get_data(self) -> Any:
        """Fetch data from the API."""
        response = await self.client.get(f"{self.url}/ambilight/currentconfiguration")
        await asyncio.sleep(RATE_LIMIT)
        self._data = response.json()

        # Check if the response matches the glitch state
        glitch_state = {
            "styleName": "Lounge light",
            "isExpert": True,
            "colorSettings": {
                "color": {"hue": 0, "saturation": 0, "brightness": 0},
                "colorDelta": {"hue": 0, "saturation": 0, "brightness": 0},
                "speed": 255,
                "algorithm": "MANUAL_HUE",
            },
        }
        if self._data == glitch_state:
            # If it does, save the previous state
            self.previous_state = {
                "brightness": self.get_brightness(),
                "hs_color": self.get_hs_color(),
                "effect": self.get_effect(),
            }
            # Reset the connection
            await self.client.aclose()
            self.client = httpx.AsyncClient(
                auth=httpx.DigestAuth(self.username, self.password), verify=False
            )
            # Restore the previous state
            if any(
                key in self.previous_state
                for key in [ATTR_BRIGHTNESS, ATTR_HS_COLOR, ATTR_EFFECT]
            ):
                await self.turn_on(**self.previous_state)
            else:
                await self.turn_off()

        return self._data

    async def send_data(self, endpoint: str, data: Any) -> int:
        """Send data to the API."""
        url = f"{self.url}/{endpoint}"
        response = await self.client.post(url, json=data)
        # Sleep for the rate limit duration
        await asyncio.sleep(RATE_LIMIT)
        return response.status_code

    def cbc_decode(self, key: bytes, data: str):
        """Decode encrypted fields based on shared key."""
        if data == "":
            return ""
        raw = b64decode(data)
        assert len(raw) >= 16, f"Length of data too short: '{data}'"
        decryptor = Cipher(algorithms.AES(key[0:16]), modes.CBC(raw[0:16])).decryptor()
        unpadder = PKCS7(128).unpadder()
        result = decryptor.update(raw[16:]) + decryptor.finalize()
        result = unpadder.update(result) + unpadder.finalize()
        return result.decode("utf-8")

    async def validate_connection(self) -> bool:
        """Validate the initial connection."""
        try:
            response = await self.client.get(f"{self.url}/system")
            if response.status_code == 200:
                # Assuming the response is a JSON object
                data = response.json()
                # Decode the password
                key = b64decode(
                    "ZmVay1EQVFOaZhwQ4Kv81ypLAZNczV9sG4KkseXWn1NEk6cXmPKO/MCa9sryslvLCFMnNe4Z4CPXzToowvhHvA=="
                )
                for k, encrypted_value in data.items():
                    if k.endswith("_encrypted"):
                        decrypted_key = k.replace("_encrypted", "")
                        decrypted_value = self.cbc_decode(key, encrypted_value.strip())
                        setattr(self, decrypted_key, decrypted_value)
                    elif k == "name":
                        setattr(self, k, encrypted_value)
                return True
            return False
        except Exception as e:
            _LOGGER.error(f"Failed to connect: {e}")
            return False

    def get_is_on(self):
        """Get the current power status from the data."""
        return self._data.get("styleName") != "OFF"

    def get_brightness(self):
        """Get the current brightness from the data."""
        # If the light is in normal hs color mode
        if (
            self._data.get("styleName") == "Lounge light"
            and self._data.get("isExpert") == True
        ):
            # Get the brightness value
            brightness = (
                self._data.get("colorSettings", {}).get("color", {}).get("brightness")
            )
            return brightness
        else:
            # If the light is not in normal hs color mode, return None
            return None

    def get_hs_color(self):
        """Get the current color from the data."""
        # If the light is in normal hs color mode
        if (
            self._data.get("styleName") == "Lounge light"
            and self._data.get("isExpert") == True
        ):
            # Get the hue and saturation values
            hue = self._data.get("colorSettings", {}).get("color", {}).get("hue")
            saturation = (
                self._data.get("colorSettings", {}).get("color", {}).get("saturation")
            )

            # Convert hue and saturation to the correct ranges and round to 0 decimal places
            if hue is not None:
                hue = round((hue / 255) * 360)
            if saturation is not None:
                saturation = round((saturation / 255) * 100)

            # Return the hs color as a tuple
            return (hue, saturation)
        else:
            # If the light is not in normal hs color mode, return None
            return None

    def get_effect(self):
        """Get the current effect from the data."""
        # If the light is in effect mode or isExpert is False
        if (
            self._data.get("styleName") != "Lounge light"
            and self._data.get("styleName") != "OFF"
        ) or not self._data.get("isExpert"):
            # Get the menuSetting value
            menu_setting = self._data.get("menuSetting")
            # Return the friendly name for the effect, or the original value if no friendly name is defined
            return self.EFFECTS.get(menu_setting, {"friendly_name": menu_setting})[
                "friendly_name"
            ]
        else:
            # If the light is not in effect mode and isExpert is True, return None
            return None

    async def turn_on(self, **kwargs):
        """Turn the light on."""
        # Get the current brightness, hue, and saturation
        current_brightness = self.get_brightness()
        current_hs_color = self.get_hs_color()

        # Check if brightness or color is in kwargs
        if kwargs.get(ATTR_BRIGHTNESS) or kwargs.get(ATTR_HS_COLOR):
            # If the light is off, activate the Natural effect first
            if not self.get_is_on():
                await self.send_data(
                    "ambilight/currentconfiguration",
                    {
                        "styleName": "FOLLOW_VIDEO",
                        "isExpert": False,
                        "menuSetting": "NATURAL",
                    },
                )
            # Determine the brightness value
            brightness = kwargs.get(ATTR_BRIGHTNESS) or current_brightness or 255

            # Determine the hue and saturation values
            if kwargs.get(ATTR_HS_COLOR):
                hue, saturation = kwargs.get(ATTR_HS_COLOR)
            elif current_hs_color:
                # If color is not provided but current_hs_color is not None, use the previous values
                hue, saturation = current_hs_color
            else:
                # If both color and current_hs_color are None, set default values
                hue, saturation = 360, 100

            # Convert hue and saturation to the range 0-255
            hue = int((hue / 360) * 255)
            saturation = int((saturation / 100) * 255)

            # Send the color data to the API
            color_data = {
                "color": {
                    "hue": hue,
                    "saturation": saturation,
                    "brightness": brightness,
                },
                "colorDelta": {"hue": 0, "saturation": 0, "brightness": 0},
                "speed": 255,
                "algorithm": "MANUAL_HUE",
            }
            await self.send_data("ambilight/lounge", color_data)

        elif kwargs.get(ATTR_EFFECT):
            friendly_name = kwargs.get(ATTR_EFFECT)
            for effect in self.EFFECTS.values():
                if effect["friendly_name"] == friendly_name:
                    # Check if the light is currently in HS mode
                    if self.get_effect() is None:
                        # If it is, turn off the light first
                        await self.send_data("ambilight/power", {"power": "off"})
                    # Then apply the new effect
                    await self.send_data(
                        effect["endpoint"],
                        effect["data"],
                    )
                    break

        # If no kwargs are provided and it's not a recursive call, restore the previous state
        elif self.previous_state and not self.get_is_on():
            # If the light is currently off and there's a stored state, restore it
            if any(
                key in self.previous_state
                for key in [ATTR_BRIGHTNESS, ATTR_HS_COLOR, ATTR_EFFECT]
            ):
                await self.turn_on(**self.previous_state)
        else:
            # Default behavior when previous_state doesn't contain any key
            # For example, turn on the light with default brightness, color, and effect
            await self.turn_on(brightness=255, hs_color=(360, 0))

    async def turn_off(self):
        """Turn the light off."""
        # Store the current Home Assistant-reported state before turning off the light
        self.previous_state = {
            "brightness": self.get_brightness(),
            "hs_color": self.get_hs_color(),
            "effect": self.get_effect(),
        }
        # If the current effect is None, switch to the Natural effect
        if self.get_effect() is None:
            await self.send_data(
                "ambilight/currentconfiguration",
                {
                    "styleName": "FOLLOW_VIDEO",
                    "isExpert": False,
                    "menuSetting": "NATURAL",
                },
            )
        # Turn off the light
        await self.send_data("ambilight/power", {"power": "off"})
