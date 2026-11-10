# dbus-phoenix-evcharger

Lightweight Venus OS (Victron GX) driver for a **Phoenix Contact EM-CP-PP-ETH** charge controller over Modbus TCP, published as `com.victronenergy.evcharger`.

- **Manual mode**: start/stop (coil 400) and charge current (holding 300) from the GX GUI
- **Auto mode**: charge on PV surplus (grid export + battery charge power above `MinSoc`), current follows the surplus between `MinCurrent` and `MaxCurrent`, with start/stop delays, minimum runtime and off-time
- Status from CP state (input register 100, ASCII A–F)
- No energy meter: power = setpoint × `CurrentFactor` × voltage × phases while in state C/D; total kWh persisted in `state.json`
- No external Python dependencies, **no automatic updates**

## Install on the GX

```sh
cd /data
wget -O dbus-phoenix-evcharger.tar.gz https://github.com/rivertechnology/dbus-phoenix-evcharger/archive/refs/heads/main.tar.gz
tar xzf dbus-phoenix-evcharger.tar.gz && mv dbus-phoenix-evcharger-main dbus-phoenix-evcharger && rm dbus-phoenix-evcharger.tar.gz
cd dbus-phoenix-evcharger
sh install.sh        # creates config.ini from config.default.ini
nano config.ini      # Host, UnitId, Position, MaxCurrent, [AUTO]
sh restart.sh
```

Enable *Settings → General → Modification checks → custom scripts* in the GX GUI, otherwise `/data/rc.local` is disabled after a firmware update.

## Update

```sh
sh /data/dbus-phoenix-evcharger/update.sh
```

Downloads `main`, keeps `config.ini` and `state.json`, backs up the whole folder to `/data/dbus-phoenix-evcharger.bak`, restarts the service and lists settings that are new in `config.default.ini`. Only runs when you start it.

## Operate

```sh
svstat /service/dbus-phoenix-evcharger
tail -f /var/log/dbus-phoenix-evcharger/current | tai64nlocal
dbus -y com.victronenergy.evcharger.phoenix_60 /Auto/Reason GetValue
sh restart.sh        # after editing config.ini
sh uninstall.sh
```

## License

MIT, see [LICENSE](LICENSE). Not affiliated with Victron Energy or Phoenix Contact.
