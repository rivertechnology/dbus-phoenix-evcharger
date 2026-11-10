# dbus-phoenix-evcharger

Minimal Venus OS (Victron GX) driver for a **Phoenix Contact EM-CP-PP-ETH** charge controller over Modbus TCP, published as `com.victronenergy.evcharger`.

- Manual mode: start/stop (coil 400) and charge current (holding 300) from the GX GUI
- Status from CP state (input register 100, ASCII A–F)
- No energy meter: power = setpoint × voltage × phases while in state C/D; kWh integrated and persisted in `state.json`
- No external Python dependencies, no auto-updates

## Install on the GX

```sh
cd /data
wget -O dbus-phoenix-evcharger.tar.gz https://github.com/rivertechnology/dbus-phoenix-evcharger/archive/refs/heads/main.tar.gz
tar xzf dbus-phoenix-evcharger.tar.gz && mv dbus-phoenix-evcharger-main dbus-phoenix-evcharger && rm dbus-phoenix-evcharger.tar.gz
cd dbus-phoenix-evcharger
nano config.ini      # Host, UnitId, Position, MaxCurrent
sh install.sh
```

Enable *Settings → General → Modification checks → custom scripts* in the GX GUI, otherwise `/data/rc.local` is disabled after a firmware update.

## Operate

```sh
svstat /service/dbus-phoenix-evcharger
tail -f /var/log/dbus-phoenix-evcharger/current | tai64nlocal
sh restart.sh        # after editing config.ini
sh uninstall.sh
```

## Update

Copy your `config.ini` and `state.json` aside, download again as above, put them back, `sh restart.sh`.
