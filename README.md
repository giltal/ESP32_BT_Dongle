# ESP32 BT Dongle

Firmware that turns a classic **ESP32 dev board** into a dual-mode (BR/EDR Classic + Bluetooth LE)
**Bluetooth controller** reached over **HCI H4 on the board's USB-serial chip**. A host plugged into
the board's USB port sees a serial port that speaks HCI. The host can be a PC running a Python
script or BlueZ, or the ESP32-P4 Launcher running Bluedroid.

It was made for the ESP32-P4 Launcher. Its A2DP speaker and its Classic + BLE gamepads need one
controller that does Classic and LE together without quirks. Commercial USB dongles fall short
here: counterfeit CSR8510s can't run a speaker and a pad together, and Realtek chips need a
firmware download.

## Boards

Any board with the **original ESP32** (ESP32-D0WD / D0WDQ6 / D2WD / PICO-D4): ESP32-WROOM-32/32D/32E/32U,
ESP32-WROVER, DevKitC, NodeMCU-32S, LOLIN32 and similar. It is the only ESP32 with Classic BR/EDR.
The S2, S3, C3, C6 and H2 have no Classic (and the S2 has no Bluetooth at all), so they won't work.

USB-serial chip: CP2102/CP2104, CH340 or CH9102. Tested on an ESP32-D0WD-V3 (rev 3.1) board with a
**CH340** on Windows 11.

## Flash a prebuilt image (no compiling)

[`firmware/`](firmware) holds the ready-built v1.0.0 images; they are also attached to the
[GitHub release](https://github.com/giltal/ESP32_BT_Dongle/releases/latest). The easiest is the single
merged image, written at offset **0x0**:

```powershell
pip install esptool
python -m esptool --chip esp32 -p COM19 -b 460800 write_flash 0x0 firmware\esp32_bt_dongle_v1.0.0_merged.bin
```

Use your own port: `COMx` on Windows, `/dev/ttyUSB0` on Linux. If the board doesn't enter the
bootloader by itself, hold **BOOT** while the flash starts.

With no Python at all, Espressif's browser flasher works in Chrome or Edge:
<https://espressif.github.io/esptool-js/>. Connect, add `esp32_bt_dongle_v1.0.0_merged.bin` at
address `0x0`, then Program.

The separate images are there too, for tools that want them: `bootloader.bin` at 0x1000,
`partition-table.bin` at 0x8000, `esp32_bt_dongle.bin` at 0x10000 (DIO, 40 MHz). Checksums are in
`SHA256SUMS.txt`. The images are built for 2 MB flash and run on any larger flash too. The baud is
the default 921600; to change it at build time, build from source.

## Build and flash

ESP-IDF **v5.5.6** with the ESP32 (Xtensa) tools installed:

```powershell
cd $env:IDF_PATH; .\install.ps1 esp32       # once: the xtensa-esp-elf toolchain
. $env:IDF_PATH\export.ps1
cd <this folder>
idf.py build
idf.py -p COM19 flash
```

`sdkconfig.defaults` holds the whole configuration. `idf.py menuconfig` → *ESP32 BT Dongle* has
the boot baud, RTS/CTS and the status LED.

## Wiring and boot

Only USB is needed. HCI runs on **UART0** (GPIO1 TX, GPIO3 RX), the pins the board already wires to
its USB-serial chip. The console, bootloader and app logs are all off so they stay off that line.
A panic reboots silently.

* **DTR/RTS** drive the board's auto-reset circuit (RTS → EN, DTR → IO0). A host must keep both
  *deasserted* in normal use. Pulsing RTS (EN low for ~100 ms, DTR released) is a clean hardware
  reset; `tools/hci_test.py` does that.
* After a reset the **ROM prints its boot banner** at 115200 baud (~870 bytes of junk at the HCI
  rate). The controller is ready about **1 s** after reset. The host should wait for that, flush
  its input, then send HCI Reset. The bridge itself skips anything that can't start an H4 packet,
  and drops a packet that stops for more than 50 ms.
  If you don't want the banner, pull **GPIO15 (MTDO) low** at boot; on the ESP32 that silences
  the ROM output.
* The **status LED** (GPIO2 by default, `-1` = none) is lit while the controller runs and flickers
  while the host sends.

## Baud rate

**921600 baud at boot, 8N1, no flow control** (`CONFIG_DONGLE_BAUD`). The host can switch rates
later with the vendor command `0xFC7D`. The dongle goes back to 921600 on every reset.

* Most dev boards don't wire CTS/RTS to the USB chip (those lines run the auto-reset), so there's
  no hardware flow control. If your board has them, turn on `CONFIG_DONGLE_UART_FLOW_CTRL` and set
  the pins.
* 921600 works with every chip listed above. The original CP2102 tops out there; CP2102N and CH340
  go higher.
* Data budget: an A2DP SBC stream at ~330 kbit/s is ~43 KiB/s from the host, about 47% of 921600.
  Pads add a few KiB/s the other way (a DualShock 4 is ~79 B × up to 250 Hz).
* The dongle's receive side never lost a byte at any rate tested, up to 2 Mbaud: 16 KiB ring, the
  UART interrupt in IRAM on core 1, and the controller on core 0.

Measured on Windows 11 with a CH340 (`tools/hci_test.py COM19`). In each run, 400 × 255-byte
echoes are sent at line rate while 400 × 258-byte replies come back, so both directions are busy
at once:

| baud      | dongle receive | PC receive (CH340 → Windows driver)  |
|-----------|----------------|--------------------------------------|
| 460800    | clean          | clean                                |
| 576000    | clean          | clean, rarely one damaged reply      |
| 750000    | clean          | ~5 bytes lost per 100 KiB            |
| 921600    | clean          | ~25–50 bytes lost per 100 KiB        |
| 1000000   | clean          | ~10–70 bytes lost per 100 KiB        |
| 1500000   | clean          | ~70 bytes lost, more damaged replies |
| 2000000   | clean          | 6–8% of replies damaged              |

With one command in flight, the way an HCI host works, 921600 ran 300 rounds (227 KiB) clean.
A single large write at 921600/1M was also clean. The losses show up only while the PC writes
many small packets *and* reads at full rate at the same time. That points at the CH340's small
FIFO and how often the Windows driver polls it, not at this firmware. The P4's own USB host driver
will behave differently: test it there (see below). If it loses bytes, switch the dongle to
576000, or lower the SBC bitpool.

## Vendor commands

The bridge answers these itself, so they never reach the controller. They use OGF 0x3F,
OCF 0x07B–0x07E; Espressif's own vendor opcodes start at OCF 0x080. Each one answers with a normal
Command Complete event: `04 0E len 01 <opcode lo> <opcode hi> <status> <return params…>`.

| opcode   | name     | parameters | return parameters after status |
|----------|----------|------------|--------------------------------|
| `0xFC7E` | identify | none       | `"LXBT"`, protocol version (1), firmware major, minor, patch, u32 current baud, flags (bit0 BR/EDR, bit1 LE, bit2 RTS/CTS), max ACL links, max LE links |
| `0xFC7D` | set baud | u32 baud (9600 … 5000000) | none. The reply is sent at the old rate and the new rate applies right after it. Status 0x12 if the rate is out of range |
| `0xFC7C` | stats    | none       | 8 × u32: rx_bytes, tx_bytes, rx_pkts, tx_pkts, resync_bytes, dropped_pkts, rx_overflow, rx_errors (since boot) |
| `0xFC7B` | echo     | 0–251 bytes | the same bytes (link test) |

**Telling this dongle apart from other controllers:** send `01 7E FC 00`. This dongle answers
`04 0E 13 01 7E FC 00 4C 58 42 54 …` ("LXBT"). A generic ESP32 HCI firmware, or any other
controller, answers status 0x01 (unknown command) or nothing. Read Local Version alone isn't
enough: it reports manufacturer 96 (Espressif) like any ESP32.

## What the controller reports

From `tools/hci_test.py` on the test board:

* HCI / LMP version 8 (Bluetooth 4.2), subversion 0x030E, manufacturer 96 (Espressif).
* Features `bf ee cd fe db ff 7b 87`: BR/EDR, LE, EDR 2/3 Mbit, simultaneous LE and BR/EDR.
* **ACL buffers 9 × 1021 bytes**, and **LE buffers 10 × 251 bytes of its own** (not shared). SCO
  buffers are reported (4 × 255), but SCO/eSCO links are configured to 0.
* Links (sdkconfig): up to **7 BR/EDR ACL** (speaker + Classic pads) and **4 LE** connections.
  `BTDM_CTRL_AUTO_LATENCY` keeps LE links from starving the A2DP stream. No modem sleep (the host
  has no wake line). LE advertising-report flow control is off: it's an Espressif vendor command
  that a non-Espressif host never sends.

## Host-side test (no P4 needed)

```powershell
pip install pyserial
python tools\hci_test.py COM19
```

The script resets the board through RTS and runs these steps:

* identify
* HCI Reset
* version, BD_ADDR, features, buffer sizes
* a Classic inquiry (6.4 s)
* an LE active scan (5 s)
* the long-name guard
* a half-duplex stress (echo + Read Local Name, 300 rounds)
* the full-duplex loss table at 921600, 460800, 576000, 1M, 1.5M and 2M

Exit status 0 means every check passed. The table is information only. Options: `--baud`,
`--try-bauds`, `--inquiry 0`, `--le-scan 0`, `--rounds`, `--no-reset`. The last test run on the
CH340 board passed every check. The LE scan found 9–13 devices; the inquiry finished correctly
but found no Classic device, because nothing nearby was discoverable during the test.

On Linux, BlueZ can use the dongle directly (not tried yet):

```sh
sudo btattach -B /dev/ttyUSB0 -P h4 -S 921600
```

Hold DTR/RTS deasserted, or the board stays in reset. Some USB-serial drivers assert them on
open; `stty -F /dev/ttyUSB0 -hupcl` helps.

## Limits and known behaviour

* **Local name:** the ESP32 controller reboots when the name is set to about 78 bytes or more. The
  bridge cuts Write Local Name to 64 bytes on a UTF-8 boundary. Bluedroid's own limit is 64, so
  Bluedroid is unaffected.
* **Name changes:** the controller asserts (`ASSERT_ERR nvds.c:400`) after roughly 480 *different*
  names in one boot. The name lives in its NVDS store. Writing the same name again is fine, and a
  real host sets it once or twice.
* No SCO/eSCO (no headset voice), no low-power sleep, no hardware flow control on typical boards.
* The bridge doesn't police HCI flow control. The host must respect the controller's command
  credits (5) and ACL credits (Number of Completed Packets), as any HCI host does.
* Debugging a crash: build with the console on UART0 and panic printing, then read the text that
  arrives on the HCI line at the current baud:
  `idf.py -B build_dbg -D SDKCONFIG=sdkconfig_dbg -D "SDKCONFIG_DEFAULTS=sdkconfig.defaults;dbg.cfg" build`
  with `dbg.cfg` holding `CONFIG_ESP_CONSOLE_UART_DEFAULT=y`, `CONFIG_ESP_SYSTEM_PANIC_PRINT_HALT=y`.

## What the ESP32-P4 Launcher needs from it

The launcher's `lx_bt` uses this dongle the way it uses a USB dongle, through
`esp_bluedroid_attach_hci_driver`. It needs two new pieces:

1. **A USB host driver for the board's USB-serial chip.** CH34x is `1A86:7523` / `1A86:55D4`
   (CH9102); CP210x is `10C4:EA60`. The driver sets the line to 921600 8N1 with the chip's vendor
   control requests and keeps **DTR and RTS deasserted**. An RTS pulse gives a hardware reset if
   wanted. It must keep **bulk IN transfers queued all the time** (several, not one at a time):
   at 921600 about 92 bytes arrive per millisecond, and the CH340's FIFO is small.
2. **A UART H4 transport for Bluedroid.** Unlike the USB transport, H4 keeps the packet-type byte
   on the wire: Bluedroid's packets go out as they are, and the incoming stream is cut into
   packets by type + header length. On open:
   * optionally pulse RTS to reset the dongle
   * wait ~1 s, flush, and drop anything that isn't a valid packet (the ROM banner)
   * send identify `0xFC7E`. "LXBT" in the reply confirms this dongle.
   * optionally switch the rate with `0xFC7D`: wait for its Command Complete at the old rate,
     then change the host side
   * hand over to Bluedroid, which starts with HCI Reset

**No fix-ups are needed.** None of the patches in `lx_bt_usb.c` apply to this dongle:
`fix_features`, `s_fake_le`, `s_fake_p1` and `fix_buffers` / `LE_SHARE`. LE is in the features,
extended features page 1 is answered (all zeros), and LE has its own 10 × 251 buffers. Bluedroid's
local name (≤ 64) is within the guard.

Test on the P4 with a speaker streaming and a DualShock 4 plus a BLE pad connected. Watch the
dongle's `stats` (`0xFC7C`) for rx_overflow / rx_errors, and the host side for H4 framing errors.
If the CH34x path drops bytes at 921600, use 576000.
