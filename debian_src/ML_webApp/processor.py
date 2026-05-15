"""
Shared processor state — single instance used by app.
"""

import threading
from collections import defaultdict
from datetime import datetime

from debian_src.ML_webApp.monitoring import (
    RawEvent,
    BathroomSession,
    CaregiverRules,
    BaselineModel,
    SessionExtractor,
    AnomalyDetector,
    load_config,
    config_to_rules,
)

_lock = threading.Lock()

_cfg = load_config()
PERSON_NAME: str = _cfg["person_name"]


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


_proc = StreamProcessor(config_to_rules(_cfg), person_name=PERSON_NAME)
_alerts: list[dict] = []


def ingest_event(event: RawEvent) -> None:
    with _lock:
        new_alerts = _proc.ingest(event)
        _alerts.extend(new_alerts)
        if len(_alerts) > 200:
            del _alerts[: len(_alerts) - 200]


def get_pending() -> list:
    with _lock:
        return [
            {"sensor": e.sensor, "value": e.value, "timestamp": e.timestamp.isoformat()}
            for e in _proc._pending
        ]


def get_snapshot() -> dict:
    with _lock:
        return {
            "person_name": PERSON_NAME,
            "sessions": list(_proc._all_sessions[-20:]),
            "alerts": list(_alerts[-20:]),
            "total_sessions": _proc.baseline.total_sessions,
            "ml_enabled": _proc.baseline._iso_forest is not None,
        }
