# Arduino UNO Q – Cloud Dashboard with LED Matrix

An **Arduino App Lab** project for the **Arduino UNO Q** that:

- Displays scrolling text on the onboard **8×13 monochrome blue LED matrix**
- Controls **RGB LED 3 and RGB LED 4** (both MCU side, STM32U585) simultaneously via PWM
- Connects to **Arduino Cloud** for remote control from a web dashboard

Code for **lesson 5** of the **Creativity, Science and Innovation** course (May 11) on **Cloud Integration & System Deployment**.

---

## Hardware

| Component | Pins | Controller |
| --- | --- | --- |
| LED Matrix (D27001–D27104) | — | MCU (STM32U585) |
| RGB LED 3 (D27401) | PH10 / PH11 / PH12 | MCU (STM32U585) |
| RGB LED 4 (D27402) | PH13 / PH14 / PH15 | MCU (STM32U585) |
| Bridge | RPC MPU ↔ MCU | both |
| Wi-Fi | 802.11ac (WCBN3536A) | MPU (QRB2210) |

> RGB LED 1 and RGB LED 2 are reserved by the system (user / wlan / bt / panic) and are not used by this project.

---

## Project structure

```
arduino-cloud/
├── app.yaml            ← App Lab config: credentials and bricks
├── sketch/
│   └── sketch.ino      ← MCU: LED matrix scroll, RGB LEDs 3 & 4, Bridge RPC
└── python/
    └── main.py         ← MPU: Arduino Cloud MQTT, Bridge calls
```

---

## Step 1 – Arduino Cloud setup

### 1.1 Add the device

1. Go to [cloud.arduino.cc](https://cloud.arduino.cc) → **Devices** → **Add device**
2. Select **Arduino UNO Q** → follow the wizard
3. Copy the **Device ID** and **Secret Key** into `app.yaml` under `secrets`

### 1.2 Create a Thing

Create a new Thing linked to the device above and add these variables:

| Variable name | Type | Permission |
| --- | --- | --- |
| `cloud_message` | String | Read / Write |
| `cloud_color` | Color | Read / Write |
| `cloud_mode` | String | Read / Write |
| `board_events` | int | Read only |

### 1.3 Create the Dashboard

Create a Dashboard linked to the same Thing and add these widgets:

| Widget type | Linked variable | Configuration |
| --- | --- | --- |
| **Messenger** (Chat) | `cloud_message` | Send messages → text scrolls on the matrix |
| **Color Picker** | `cloud_color` | Pick any color → both RGB LEDs update via PWM |
| **Dropdown** | `cloud_mode` | Add two options: value `message`, value `off` |
| **Value** | `board_events` | Label: "Commands sent" |

---

## Step 2 – Load and run in App Lab

1. Open **Arduino App Lab** on your PC or directly on the board
2. **File → Open** → select the `arduino-cloud/` folder
3. App Lab reads `app.yaml` and detects `sketch.ino` + `main.py`
4. Press **Run** — App Lab will:
   - Compile and upload `sketch.ino` to the STM32U585
   - Start `main.py` on the Linux (MPU) side
5. Check the console for:
   ```
   [MCU] Bridge ready
   [MCU] Bridge providers registered
   Dashboard ready. Open cloud.arduino.cc to control the board.
   ```

---

## Import in App Lab (ZIP)

You can import this project directly in Arduino App Lab using the ZIP from the GitHub Release.

1. Download the latest release ZIP from the repository Releases page.
2. Open **Arduino App Lab** → **File** → **Import** (or **Open Project**).
3. Select the ZIP file.
4. App Lab reads `app.yaml` and loads `sketch.ino` + `main.py` automatically.

---

## Step 3 – Use the Dashboard

| Action | What happens on the board |
|---|---|
| Type a message in the **Messenger** and send | Text scrolls on the blue LED matrix |
| Set **Dropdown** to `message` | Last received message restarts scrolling |
| Set **Dropdown** to `off` | Matrix clears |
| Pick a color in **Color Picker** | RGB LED 3 and RGB LED 4 light up with that color |
| **Value** widget | Shows total commands received since last boot |

---

## Bridge API

Functions exposed by `sketch.ino` and called by `main.py` over the Bridge:

| Function | Arguments | Returns | Effect |
| --- | --- | --- | --- |
| `set_message` | `string` | `true` | Scroll text on the LED matrix |
| `clear_matrix` | — | `true` | Blank the LED matrix |
| `set_led3_color` | `r, g, b` (0–255) | `true` | Set RGB LED 3 and LED 4 via PWM |
| `get_status` | — | JSON | Returns `{"events": N}` |

---

## Troubleshooting

**Matrix shows nothing after boot**
- Wait ~30 s for the full Linux boot to complete before the Bridge is ready
- Confirm `Bridge.begin()` runs before `Bridge.provide(...)` in `sketch.ino`

**Arduino Cloud stays offline**
- Double-check Device ID and Secret Key in `app.yaml`
- Make sure Wi-Fi is configured in App Lab → Network settings

**`arduino.app_bricks.arduino_cloud` not found**
- In App Lab go to **Bricks → Add Brick → Arduino Cloud** and add it to the project

**Color picker has no effect**
- The `cloud_color` variable must be type **Color** (not String) in the Thing
- The Dashboard widget must be **Color Picker** linked to `cloud_color`

**RGB LED colors look wrong**
- LED 3 (PH10–12) is active-high; LED 4 (PH13–15) is active-low — the sketch handles both internally via `setAllLedsPwm()`

---

## Useful links

| Resource | URL |
|---|---|
| UNO Q User Manual | https://docs.arduino.cc/tutorials/uno-q/user-manual/ |
| UNO Q Hardware page | https://docs.arduino.cc/hardware/uno-q |
| App Lab documentation | https://docs.arduino.cc/software/app-lab/ |
| Arduino Cloud | https://cloud.arduino.cc |
| Bridge RPC documentation | https://docs.arduino.cc/software/app-lab/bridge/get-started-with-bridge/ |
| LED Matrix library | https://docs.arduino.cc/tutorials/uno-r4-wifi/led-matrix/ |
