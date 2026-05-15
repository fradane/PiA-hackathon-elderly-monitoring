"""
Fake Bluetooth/Arduino reader — simulates the event stream from the hardware sensor.
Feeds events into the shared processor so the Flask dashboard shows live data.
"""

import time
from datetime import datetime, timedelta

import debian_src.ML_webApp.processor as processor
from debian_src.ML_webApp.fake_stream import generate_day, session_events, _generate_day_with_drift, _generate_day_short
from debian_src.ML_webApp.monitoring import load_config, config_to_rules, RawEvent

SPEED = 500.0   # compress time: 1 real second = SPEED simulated seconds


def run(days: int = 60, inject_anomalies: bool = True) -> None:
    import random
    random.seed(0)

    start = datetime(2024, 1, 1)

    for day_offset in range(days):
        day = start + timedelta(days=day_offset)
        shower = day_offset % 2 == 0

        if inject_anomalies and day_offset == 7:
            events = session_events(day.replace(hour=3, minute=22), 48.0, False)
            events += generate_day(day, shower)
        elif inject_anomalies and 18 <= day_offset <= 21:
            events = generate_day(day, shower=False)
        elif inject_anomalies and day_offset == 25:
            events = session_events(day.replace(hour=8, minute=10), 8.0, False)
        elif inject_anomalies and 28 <= day_offset <= 44:
            drift = (day_offset - 28) * 1.2
            events = _generate_day_with_drift(day, shower, drift)
        elif inject_anomalies and 48 <= day_offset <= 51:
            events = _generate_day_short(day, shower)
        else:
            events = generate_day(day, shower)

        events.sort(key=lambda e: e.timestamp)

        prev_ts = events[0].timestamp
        for ev in events:
            gap = (ev.timestamp - prev_ts).total_seconds() / SPEED
            if gap > 0:
                time.sleep(min(gap, 0.05))
            prev_ts = ev.timestamp
            processor.ingest_event(ev)
