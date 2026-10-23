# Govee BLE Lighting Integration for HomeAssistant

![Govee Logo](assets/govee-logo.png)

A powerful and seamless integration to control your Govee lighting devices via Govee API or BLE directly from HomeAssistant with full features support.

---

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Support & Contribution](#support--contribution)
- [License](#license)

---

## Features

- 🚀 **Direct BLE control**: no bridges or middlemen. Lights are controlled over Bluetooth Low Energy, including the encrypted ones (see [Supported BLE lights](#supported-ble-lights)).

- ☁️ **Cloud API control**: every Govee light on your account, with scenes and DIY scenes. State is polled every 30 seconds.

- 🏠 **LAN control**: lights reachable over your local network are controlled locally first, falling back to the cloud API on any error. State is pushed live. Effects always go through the cloud, because the scene catalog is the cloud's.

- 🌈 **Scenes**: pick from the full scene list in the effect dropdown. BLE scene catalogs are synced with Govee's live catalog, and scenes with several variants are named plainly (for example "Halloween C").

- 🧩 **Segments**: lights with addressable segments get one extra light entity per segment, for both cloud and BLE lights.

- 💡 **Lighting control**: brightness, color, color temperature and on/off.

- 📶 **Connection type sensor**: each device reports how it is connected (Cloud API, Bluetooth, LAN or Hybrid).

---

## Installation

Install through [HACS](https://hacs.xyz/) as a custom repository, or copy `custom_components/govee_ble_lights/` into your Home Assistant `custom_components` folder, then restart Home Assistant.

> **Upgrading from an older version?** The integration domain was renamed to `govee_ble_lights`. Remove the old entry and add the integration again.

---

## Configuration

### What is needed

For direct BLE control:
- Home Assistant must be able to use Bluetooth on your host (or through a Bluetooth proxy). Keep the light within range.

For Govee API control:
- Get a Govee API key as described [here](https://developer.govee.com/reference/apply-you-govee-api-key), add the integration with the API type, and enter the key.

For LAN control:
- Turn on LAN control for the light in the Govee Home app. Cloud lights that are also reachable on the LAN are picked up automatically.

## Usage

With the integration set up, your Govee devices appear as entities in Home Assistant. For BLE, select your device model when adding it.

### Supported BLE lights

Different Govee models speak different BLE protocols. The integration detects which one a light uses and remembers it.

| Protocol | Models |
|---|---|
| Plain | H705A, H70B1, H7092 (other models are detected automatically) |
| Encrypted v1 | H7093, H6095, H619D |
| Encrypted v2 (AES-GCM) | H3501 |

Lights with segments (H6053, H6072, H6102, H6199, H70B1, H6095, H7092, H619D) get one entity per segment. **BLE segment control is new and unverified on real hardware**: the packet format and the default of 15 segments per model are assumptions. If a segment lights the wrong place or your model has a different count, please open an issue. Segment state is optimistic, because neither BLE nor the cloud API reports it back.

---

## Troubleshooting for BLE

1. **Check the BLE connection**: keep the light within Bluetooth range of your Home Assistant host or proxy.

2. **Model check**: make sure you selected the correct device model.

3. **Light shows "on" but stays off**: the light was probably detected as plain when it is not. Delete its saved scheme file, `<config>/.storage/govee_ble_lights/ble_scheme_<MAC>.json`, and restart Home Assistant.

4. **Logs**: Home Assistant logs show errors from the integration under **Settings > System > Logs**.

BLE lights have no state read-back, so their on/off state, brightness and active scene are tracked optimistically.

---

## Support & Contribution

- **Found an Issue?** 
   
   Raise it in the [Issues section](https://github.com/BigWebstas/govee_ble_lights/issues) of this repository.

- **Device support**:

   Almost every Govee device has its own BLE message protocol. If you have an Android smartphone and your device is not supported, please contact me on [Telegram](https://t.me/Beshelmek).

- **Contributions**:

   We welcome community contributions! If you'd like to improve the integration or add new features, please fork the repository and submit a pull request.

---

## Future Plans

We aim to continuously improve this integration by:

- Supporting more Govee device models for BLE
- Enhancing the overall user experience and stability

---

## License

This project is under the MIT License. For full license details, please refer to the [LICENSE file](https://github.com/Beshelmek/govee_ble_lights/blob/main/LICENSE) in this repository.
