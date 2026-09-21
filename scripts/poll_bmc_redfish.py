# Copyright (c) 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0
"""Poll Galaxy system power from the BMC over Redfish and write a CSV.

Reads /redfish/v1/Chassis/chassis/Sensors/<name> for the power sensors
(power_Power_Total, PSU0..3, UBB0..3, MB_HSC, CPU, Memory, FAN) with HTTP basic
auth. Faster than in-band IPMI (~1-3 s per sample, half-watt values). Needs the
BMC address and a login; the password comes from the environment, never from a
file in the repo. Stops cleanly on SIGINT/SIGTERM. Standard library only.

    BMC_PASSWORD=... python3 scripts/poll_bmc_redfish.py --ip 10.x.y.z --user root --out bmc_power.csv --interval 1

Output columns: Timestamp (ISO, UTC) followed by the sensor names, one row per poll.
"""

import argparse
import base64
import csv
import datetime as dt
import json
import os
import signal
import ssl
import sys
import time
import urllib.error
import urllib.request

SENSORS = [
    "power_Power_Total",
    "power_Power_PSU0",
    "power_Power_PSU1",
    "power_Power_PSU2",
    "power_Power_PSU3",
    "power_Power_UBB0",
    "power_Power_UBB1",
    "power_Power_UBB2",
    "power_Power_UBB3",
    "power_Power_MB_HSC",
    "power_Power_CPU",
    "power_Power_Memory",
    "power_Power_FAN",
]
STOP = False


def _stop(signum, frame):  # noqa: ARG001
    global STOP
    STOP = True


def make_opener(user: str, password: str) -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # BMCs use self-signed certificates
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    opener.addheaders = [("Authorization", f"Basic {token}"), ("Accept", "application/json")]
    return opener


def read_sensor(opener, base: str, name: str, timeout: float):
    try:
        with opener.open(f"{base}/{name}", timeout=timeout) as resp:
            return json.load(resp).get("Reading")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError) as exc:
        return exc


def main() -> int:
    ap = argparse.ArgumentParser(description="Poll BMC Redfish power sensors to CSV")
    ap.add_argument("--ip", required=True, help="BMC address")
    ap.add_argument("--user", default=os.environ.get("BMC_USER", "root"))
    ap.add_argument("--out", default="bmc_power.csv")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--sensors", default=",".join(SENSORS), help="comma-separated sensor names")
    args = ap.parse_args()
    password = os.environ.get("BMC_PASSWORD")
    if not password:
        print("BMC_PASSWORD is not set; refusing to poll", file=sys.stderr)
        return 2

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    sensors = [s.strip() for s in args.sensors.split(",") if s.strip()]
    base = f"https://{args.ip}/redfish/v1/Chassis/chassis/Sensors"
    opener = make_opener(args.user, password)

    rows = 0
    with open(args.out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["Timestamp", *sensors])
        writer.writeheader()
        while not STOP:
            t0 = time.monotonic()
            row = {}
            errors = []
            for name in sensors:
                value = read_sensor(opener, base, name, args.timeout)
                if isinstance(value, Exception):
                    errors.append(f"{name}: {value}")
                    row[name] = None
                else:
                    row[name] = value
            if any(v is not None for v in row.values()):
                row["Timestamp"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
                writer.writerow(row)
                fh.flush()
                rows += 1
            elif rows == 0 and errors:
                print("no readings yet: " + "; ".join(errors[:3]), file=sys.stderr, flush=True)
            remaining = args.interval - (time.monotonic() - t0)
            end = time.monotonic() + max(remaining, 0)
            while not STOP and time.monotonic() < end:
                time.sleep(0.2)
    print(f"wrote {rows} rows to {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
