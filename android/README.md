# Android MVP (Local)

This directory contains the Android MVP app for local usage.

## Implemented MVP scope

- Dashboard: latest snapshot + latest automation run
- Quick actions: create snapshot, add event
- History: snapshots/events list, keyword search
- Event management: create/update/delete
- Key records: list + search
- Settings: local backend base URL configuration

## Local run

1. Start backend server from project root:

```bash
python -m server.main
```

2. Open `android/` in Android Studio.
3. Let Gradle sync.
4. Run app on emulator/device.

## Backend URL notes

- Android emulator: use `http://10.0.2.2:8000/`
- Real device: use your LAN IP, e.g. `http://192.168.1.20:8000/`

Set the URL in app `设置` tab and tap `测试连接`.
