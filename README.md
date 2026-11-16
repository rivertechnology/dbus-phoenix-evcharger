# dbus-phoenix-evcharger

Lightweight Venus OS (Victron GX) driver for a **Phoenix Contact EM-CP-PP-ETH** charge controller over Modbus TCP, published as `com.victronenergy.evcharger`.

- **Manual mode**: start/stop (coil 400) and charge current (holding 300) from the GX GUI
- **Auto mode**: charge on PV surplus (grid export + battery charge power above `MinSoc`), current follows the surplus between `MinCurrent` and `MaxCurrent`, with start/stop delays, minimum runtime and off-time
- Status from CP state (input register 100, ASCII A–F)
- Same D-Bus paths as Victron's own EV Charging Station driver (`/Session/Energy`, `/Session/Time`, `/Ac/Energy/Forward`, `/Position`, `/AutoStart`, …), so the GX GUI shows it as an EVCS
- Position, name and AutoStart set in the GUI are remembered (`state.json`); editing `config.ini` overrides them again
- Optional **Eastron SDM72D-M (MID)** via a Modbus TCP gateway (`[METER]` in `config.ini`): real power, current, voltage and the meter's import kWh
- Without a meter: power = setpoint × `CurrentFactor` × voltage × phases while in state C/D; total kWh persisted in `state.json`
- Optional OCPP 1.6J reporting of sessions with MID readings (e.g. to Joulo)
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

## Energy meter (SDM72D-M)

Connect the meter's RS485 to a Modbus RTU→TCP gateway (9600 8N1, meter address 1), then:

```sh
cd /data/dbus-phoenix-evcharger
python3 dbus-phoenix-evcharger.py --test-meter 192.168.178.xx   # try both word orders
nano config.ini      # [METER] Host=192.168.178.xx (and WordOrder if needed)
sh restart.sh
```

If the meter can't be read, the driver falls back to calculated power and freezes the kWh counter until the meter is back.

## Update

```sh
sh /data/dbus-phoenix-evcharger/update.sh
```

Downloads `main`, keeps `config.ini` and `state.json`, backs up the whole folder to `/data/dbus-phoenix-evcharger.bak`, restarts the service and lists settings that are new in `config.default.ini`. Only runs when you start it.

## OCPP (e.g. Joulo)

Optionally reports every charging session to an OCPP 1.6J central system, with the MID meter readings in Wh: `StartTransaction` (meterStart) when the car is plugged in, `MeterValues` every minute, `StopTransaction` (meterStop) when it is unplugged. Report-only: remote start/stop from the backend is rejected, charging stays under control of the GX.

```ini
[OCPP]
Enabled=1
Url=wss://ocpp.joulo.nl/JL-XXXX-XXXX
AuthKey=<key from the provider>
```

```sh
python3 /data/dbus-phoenix-evcharger/dbus-phoenix-evcharger.py --test-ocpp   # connect + BootNotification
sh /data/dbus-phoenix-evcharger/restart.sh
```

Messages are queued in `ocpp_state.json` and delivered in order after an internet outage or a restart, so no session is lost. The key is never written to the log. With `RequireMeter=1` (default) sessions are only reported when a meter reading is available.

## Check the installation

```sh
sh /data/dbus-phoenix-evcharger/check.sh
```

Read-only. Verifies service and boot hook, `config.ini`, the D-Bus paths the Victron GUI uses for an EVCS, reads the charger and the meter directly and compares them with the D-Bus, scans the last 24 h of the log, and looks for other controllers of the same charger (old drivers, Node-RED Modbus writes). A line `write enable=… (charger had …)` in the log means a second Modbus master is writing to the charger.

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

Written with Claude (Anthropic).
