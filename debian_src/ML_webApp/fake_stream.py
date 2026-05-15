"""
Simula il flusso di eventi che arriva dall'Arduino:
  - sensor="door"   → cambio stato porta (toggle)
  - sensor="shower" → booleano, True se la doccia è attiva in quel momento

Uso:
    python fake_stream.py                  # 30 giorni, velocità x500
    python fake_stream.py --days 60
    python fake_stream.py --speed 2000
    python fake_stream.py --no-anomalies
"""

from __future__ import annotations

import argparse
import random
import time
from collections import defaultdict
from datetime import datetime, timedelta

from debian_src.ML_webApp.monitoring import (
    AnomalyDetector,
    BaselineModel,
    BathroomSession,
    CaregiverRules,
    RawEvent,
    SessionExtractor,
    config_to_rules,
    load_config,
)

random.seed(0)


# ---------------------------------------------------------------------------
# Generatore di eventi fittizi
# ---------------------------------------------------------------------------

def _shower_events(start: datetime, end: datetime, has_shower: bool) -> list[RawEvent]:
    """Genera letture periodiche del sensore doccia durante una sessione."""
    events = []
    t = start + timedelta(seconds=30)
    while t < end:
        duration = (end - start).total_seconds()
        progress = (t - start).total_seconds() / duration
        if has_shower:
            value = 0.2 < progress < 0.85
        else:
            value = False
        events.append(RawEvent(timestamp=t, sensor="shower", value=value))
        t += timedelta(seconds=30)
    return events


def session_events(start: datetime, duration_min: float, has_shower: bool) -> list[RawEvent]:
    """Produce gli eventi raw per una singola visita al bagno."""
    end = start + timedelta(minutes=duration_min)
    return (
        [RawEvent(start, "door", True)]
        + _shower_events(start, end, has_shower)
        + [RawEvent(end, "door", True)]
    )


def _generate_day_with_drift(date: datetime, shower: bool, extra_minutes: float) -> list[RawEvent]:
    """Giornata normale ma con durate aumentate di extra_minutes (simula deriva graduale)."""
    schedule = [
        (7,  15, shower, random.gauss(14, 2) + extra_minutes),
        (10, 20, False,  random.gauss(7,  1) + extra_minutes * 0.5),
        (14, 25, False,  random.gauss(8,  1) + extra_minutes * 0.5),
        (20, 18, False,  random.gauss(9,  1) + extra_minutes * 0.5),
    ]
    events = []
    for hour, jitter_max_min, sh, dur in schedule:
        t = date.replace(hour=hour, minute=0, second=0, microsecond=0)
        t += timedelta(minutes=random.randint(0, jitter_max_min))
        events.extend(session_events(t, max(dur, 2.0), sh))
    return events


def _generate_day_short(date: datetime, shower: bool) -> list[RawEvent]:
    """Giornata con sessioni molto brevi (malessere)."""
    schedule = [
        (7,  5, shower, random.gauss(3, 0.5)),
        (10, 5, False,  random.gauss(2, 0.5)),
        (14, 5, False,  random.gauss(3, 0.5)),
        (20, 5, False,  random.gauss(2, 0.5)),
    ]
    events = []
    for hour, jitter_max_min, sh, dur in schedule:
        t = date.replace(hour=hour, minute=0, second=0, microsecond=0)
        t += timedelta(minutes=random.randint(0, jitter_max_min))
        events.extend(session_events(t, max(dur, 1.0), sh))
    return events


def generate_day(date: datetime, shower: bool) -> list[RawEvent]:
    """4 visite tipiche con jitter realistico."""
    schedule = [
        (7,  15, shower, random.gauss(14, 3)),
        (10, 20, False,  random.gauss(7,  2)),
        (14, 25, False,  random.gauss(8,  2)),
        (20, 18, False,  random.gauss(9,  2)),
    ]
    events = []
    for hour, jitter_max_min, sh, dur in schedule:
        t = date.replace(hour=hour, minute=0, second=0, microsecond=0)
        t += timedelta(minutes=random.randint(0, jitter_max_min))
        events.extend(session_events(t, max(dur, 2.0), sh))
    return events


# ---------------------------------------------------------------------------
# Processor — riceve un evento alla volta, come farebbe un listener MQTT
# ---------------------------------------------------------------------------

class StreamProcessor:
    def __init__(self, rules: CaregiverRules, person_name: str = "La persona"):
        self.baseline = BaselineModel(rules=rules)
        self.extractor = SessionExtractor()
        self.detector = AnomalyDetector(self.baseline, person_name=person_name)
        self._pending: list[RawEvent] = []
        self._last_session_end: datetime | None = None
        self._all_sessions: list[BathroomSession] = []
        self._daily_counts: dict[str, int] = defaultdict(int)

    def ingest(self, event: RawEvent) -> list[dict]:
        """
        Riceve un evento. Quando arriva il secondo door (chiusura),
        estrae la sessione e controlla le anomalie.
        """
        self._pending.append(event)

        door_events_in_pending = [e for e in self._pending if e.sensor == "door"]
        if len(door_events_in_pending) < 2 or len(door_events_in_pending) % 2 != 0:
            return []

        sessions = self.extractor.extract(self._pending, self._last_session_end)
        self._pending.clear()

        alerts = []
        for s in sessions:
            for a in self.detector.check_session(s):
                alerts.append(_fmt(a))
            self.baseline.update(s)
            self._all_sessions.append(s)
            self._last_session_end = s.end_time

            day_key = s.start_time.date().isoformat()
            self._daily_counts[day_key] += 1

        if sessions:
            last_day = sessions[-1].start_time.date().isoformat()
            for d in list(self._daily_counts):
                if d < last_day:
                    count = self._daily_counts.pop(d)
                    for a in self.detector.check_daily_frequency(
                        datetime.fromisoformat(d), count
                    ):
                        alerts.append(_fmt(a))

        return alerts

    def check_gap(self, now: datetime) -> list[dict]:
        if self._last_session_end is None:
            return []
        return [_fmt(a) for a in self.detector.check_gap(self._last_session_end, now)]

    def check_shower(self, reference: datetime) -> list[dict]:
        return [
            _fmt(a)
            for a in self.detector.check_shower_regularity(self._all_sessions, reference)
        ]

    def check_drift(self) -> list[dict]:
        return [_fmt(a) for a in self.detector.check_drift(self._all_sessions)]

    def check_sequence(self) -> list[dict]:
        return [_fmt(a) for a in self.detector.check_sequence(self._all_sessions)]


def _fmt(a) -> dict:
    return {
        "ts": a.timestamp.strftime("%Y-%m-%d %H:%M"),
        "type": a.anomaly_type,
        "severity": a.severity,
        "message": a.message,
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

COLORS = {"HIGH": "\033[91m", "MEDIUM": "\033[93m", "LOW": "\033[92m"}
RESET = "\033[0m"
ICONS = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}


def _print_alert(alert: dict) -> None:
    color = COLORS.get(alert["severity"], "")
    icon = ICONS.get(alert["severity"], "⚪")
    print(f"\n  {color}{icon} [{alert['severity']}] {alert['type']}{RESET}")
    print(f"     {alert['ts']}  —  {alert['message']}")


def run(days: int = 30, speed: float = 500.0, inject_anomalies: bool = True) -> None:
    cfg = load_config()
    rules = config_to_rules(cfg)
    person_name = cfg["person_name"]
    processor = StreamProcessor(rules, person_name=person_name)
    start = datetime(2024, 1, 1)

    print(f"{'─'*60}")
    print(f"  PiA Monitor — simulazione {days} giorni (speed ×{speed:.0f})")
    print(f"{'─'*60}")

    total_events = 0
    total_alerts = 0

    for day_offset in range(days):
        day = start + timedelta(days=day_offset)
        shower = day_offset % 2 == 0

        if inject_anomalies and day_offset == 7:
            # Sessione notturna lunga (possibile caduta)
            events = session_events(day.replace(hour=3, minute=22), 48.0, False)
            events += generate_day(day, shower)
        elif inject_anomalies and 18 <= day_offset <= 21:
            # 4 giorni senza doccia
            events = generate_day(day, shower=False)
        elif inject_anomalies and day_offset == 25:
            # Solo 1 visita
            events = session_events(day.replace(hour=8, minute=10), 8.0, False)
        elif inject_anomalies and 28 <= day_offset <= 44:
            # Deriva graduale: durate crescono di ~1 min al giorno
            drift = (day_offset - 28) * 1.2
            events = _generate_day_with_drift(day, shower, drift)
        elif inject_anomalies and 48 <= day_offset <= 51:
            # 4 sessioni consecutive molto brevi (malessere)
            events = _generate_day_short(day, shower)
        else:
            events = generate_day(day, shower)

        events.sort(key=lambda e: e.timestamp)
        print(f"\n  [{day.strftime('%a %d/%m')}]  {len(events)} eventi", end="", flush=True)

        prev_ts = events[0].timestamp
        for ev in events:
            if speed > 0:
                gap = (ev.timestamp - prev_ts).total_seconds() / speed
                if gap > 0:
                    time.sleep(min(gap, 0.05))
            prev_ts = ev.timestamp

            for alert in processor.ingest(ev):
                _print_alert(alert)
                total_alerts += 1
            total_events += 1

        end_of_day = day.replace(hour=23, minute=59)
        for alert in processor.check_shower(reference=end_of_day):
            _print_alert(alert)
            total_alerts += 1
        for alert in processor.check_gap(now=end_of_day):
            _print_alert(alert)
            total_alerts += 1
        for alert in processor.check_drift():
            _print_alert(alert)
            total_alerts += 1
        for alert in processor.check_sequence():
            _print_alert(alert)
            total_alerts += 1

    print(f"\n\n{'─'*60}")
    print(f"  Fine — {total_events} eventi, {total_alerts} alert")
    print(f"  Sessioni totali: {processor.baseline.total_sessions}")
    print(f"  ML attivo: {processor.baseline._iso_forest is not None}")
    print(f"{'─'*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",  type=int,   default=30)
    parser.add_argument("--speed", type=float, default=500)
    parser.add_argument("--no-anomalies", action="store_true")
    args = parser.parse_args()
    run(days=args.days, speed=args.speed, inject_anomalies=not args.no_anomalies)
