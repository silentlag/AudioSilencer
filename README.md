<div align="center">

# AudioSilencer

Mutes Discord when an osu! combo crosses a threshold you set. When the combo drops or the map ends, sound comes back.

The mute is done through Discord's own IPC (self-deafen), so stream audio keeps working. The alternative is a Windows mixer mute, which works for any process name.

## What it does

- Reads osu! gameplay data through [tosu](https://github.com/tosu-dev/tosu) (memory reader, signature-free)
- Connects to Discord via local IPC pipe (`\\.\pipe\discord-ipc-N`)
- Issues `SET_VOICE_SETTINGS {deaf: true}` - self-deafen, not a mixer mute
- Unmutes on combo drop (instant mode), map end (map mode), or on miss (FC mode)

## Modes

| Mode | Trigger |
|---|---|
| 1 · until map ends | combo >= threshold -> mute, holds until map ends |
| 2 · instant | combo >= threshold -> mute, below -> unmute immediately |
| 3 · progress % | mute at given % of the map, holds until map ends |
| 4 · FC % | mute at given % while full combo is clean (no misses, no slider breaks); unmutes on miss only |

The threshold accepts a number (`100`) or a percentage (`33`, `33%`).

## Setup

1. Download or build `AudioSilencer.exe`. Place the `tosu/` folder next to it (contains `tosu.exe`).
2. Run it. It starts tosu hidden in the background.
3. For IPC deafen you need your own Discord application (RPC OAuth only authorizes the app owner):
   - [discord.com/developers/applications](https://discord.com/developers/applications) -> **New Application**
   - OAuth2 tab -> **Redirect URIs** -> add `http://localhost:8000` -> Save
   - Copy **Client ID** and **Client Secret** from the OAuth2 tab into the app
4. Press **Login Discord** and authorize. Tokens are stored encrypted (DPAPI, tied to your Windows user) and auto-refreshed.

## Mute methods

- **Discord deafen (IPC)** - for Discord PTB / Discord targets. Stream audio survives.
- **Windows mixer** - for any other process name. Mutes all audio of that process.

The method is picked automatically: Discord targets use IPC, everything else uses the mixer.

## Launch modes

```
AudioSilencer.exe             # window (default)
AudioSilencer.exe --tray      # tray icon only
audio_silencer.py --console   # console
AudioSilencer.exe --diag      # IPC diagnostics, writes diag.txt
```

## Settings

`config.json` next to the exe: threshold, target process, mute mode, mute method, encrypted Discord tokens (`access_enc` / `refresh_enc`).

tosu is preconfigured in `tosu/tosu.env`:

```env
POLL_RATE=100
CALCULATE_PP=false
OPEN_DASHBOARD_ON_STARTUP=false
```

## Building

```bash
pip install websocket-client pycaw comtypes pystray Pillow pyinstaller
pyinstaller --onefile --noconsole --name AudioSilencer --icon app.ico --add-data "app.ico;." audio_silencer.py
```

## Sharing

Run `make_share.bat` - produces a `share/` folder with the exe, icon, tosu, and a config template (`YOUR_CLIENT_ID` / `YOUR_CLIENT_SECRET` placeholders). No tokens are included; each person logs in with their own application.

## Requirements

- Windows 10/11
- osu! (any build - tosu reads memory, not signatures)
- Discord PTB / Discord / Canary for IPC deafen

## Credits

- [tosu](https://github.com/tosuapp/tosu) - osu! memory reader
- [AutoDeafen](https://github.com/Lynxdeer/AutoDeafen) - Inspired from (Geometry dash analogue)
- [pycaw](https://github.com/AndreMiras/pycaw) - Windows audio sessions (mixer method)

<div align="center">
