#!/bin/sh
# Restart after a config.ini change; runit starts it again automatically
svc -t /service/dbus-phoenix-evcharger
