# A GoPro kept live for the eyes (gopro_link.py): the official Open GoPro SDK over the host's Bluetooth.
# BlueZ's client inside: the SDK pairs through bluetoothctl (to the host's bluetoothd over D-Bus).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends bluez && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir open-gopro==0.22.0
# the SDK's Wi-Fi driver refuses to load unless LANG is en_US (it is not used: BLE only)
ENV LANG=en_US.UTF-8 PYTHONUNBUFFERED=1
WORKDIR /app
COPY gopro_link.py /app/
CMD ["python", "gopro_link.py"]
