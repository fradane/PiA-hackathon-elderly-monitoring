"""
Elderly bathroom monitoring: session extraction, baseline modelling, anomaly detection.

Formato eventi in ingresso (da Arduino):
  - sensor="door"   → la porta ha cambiato stato (toggle: dispari=aperta, pari=chiusa)
  - sensor="shower" → valore booleano, True se la doccia è in corso / è stata rilevata
"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def load_config(path: str | Path = "config.json") -> dict:
    return json.loads(Path(path).read_text())


def config_to_rules(cfg: dict) -> "CaregiverRules":
    daily_visits = cfg["typical_daily_visits"]
    duration = cfg["typical_duration_minutes"]
    shower_days = cfg["typical_shower_every_days"]

    return CaregiverRules(
        min_daily_visits=max(1, daily_visits - 1),
        max_visit_duration_minutes=duration * 4,
        max_hours_without_visit=max(4, 16 // daily_visits + 2),
        shower_max_days_without=shower_days + 1,
        sleep_window=(23, 7),
        sequence_window_hours=6,
    )


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class RawEvent:
    timestamp: datetime
    sensor: str        # "door" | "shower"
    value: bool        # door: True = cambio rilevato; shower: True/False


@dataclass
class BathroomSession:
    start_time: datetime
    end_time: datetime
    duration_minutes: float
    has_shower: bool
    hour_of_day: int
    day_of_week: int                    # 0 = lunedì
    gap_from_last_session_hours: float


@dataclass
class Anomaly:
    timestamp: datetime
    anomaly_type: str
    severity: str                       # "LOW" | "MEDIUM" | "HIGH"
    message: str
    session: dict[str, Any] | None
    baseline_context: dict[str, Any]


# ---------------------------------------------------------------------------
# 1. SessionExtractor
# ---------------------------------------------------------------------------

class SessionExtractor:
    """
    Ricostruisce sessioni bagno da un flusso di eventi raw.

    Logica porta:
      - Gli eventi door arrivano in sequenza di toggle.
      - Numero dispari di door events visti finora → porta aperta.
      - Numero pari → porta chiusa.
      - Una sessione è il tratto tra un'apertura e la chiusura successiva.

    Logica doccia:
      - Se almeno un evento shower=True arriva durante la sessione → has_shower=True.
    """

    MIN_SESSION_SECONDS = 5    # ignora variazioni di porta molto brevi (rumore)

    def extract(
        self,
        events: list[RawEvent],
        last_session_end: datetime | None = None,
    ) -> list[BathroomSession]:
        events = sorted(events, key=lambda e: e.timestamp)
        sessions: list[BathroomSession] = []
        prev_end = last_session_end

        door_events = [e for e in events if e.sensor == "door"]

        i = 0
        while i < len(door_events) - 1:
            open_ev = door_events[i]
            close_ev = door_events[i + 1]

            open_time = open_ev.timestamp
            close_time = close_ev.timestamp

            duration_s = (close_time - open_time).total_seconds()
            if duration_s < self.MIN_SESSION_SECONDS:
                i += 2
                continue

            # Raccoglie eventi shower tra apertura e chiusura
            shower_events = [
                e for e in events
                if e.sensor == "shower"
                and open_time <= e.timestamp <= close_time
            ]
            has_shower = any(e.value for e in shower_events)

            gap_hours = (
                (open_time - prev_end).total_seconds() / 3600.0
                if prev_end is not None
                else 0.0
            )

            sessions.append(BathroomSession(
                start_time=open_time,
                end_time=close_time,
                duration_minutes=duration_s / 60.0,
                has_shower=has_shower,
                hour_of_day=open_time.hour,
                day_of_week=open_time.weekday(),
                gap_from_last_session_hours=gap_hours,
            ))

            prev_end = close_time
            i += 2

        return sessions


# ---------------------------------------------------------------------------
# 2. BaselineModel
# ---------------------------------------------------------------------------

@dataclass
class RunningStats:
    """Welford online mean / variance — aggiorna senza tenere tutti i valori."""
    n: int = 0
    mean: float = 0.0
    M2: float = 0.0

    @property
    def std(self) -> float:
        return math.sqrt(self.M2 / self.n) if self.n > 1 else 0.0

    def update(self, x: float) -> None:
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        self.M2 += delta * (x - self.mean)

    def to_dict(self) -> dict:
        return {"n": self.n, "mean": self.mean, "M2": self.M2}

    @classmethod
    def from_dict(cls, d: dict) -> "RunningStats":
        return cls(n=d["n"], mean=d["mean"], M2=d["M2"])


@dataclass
class CaregiverRules:
    shower_expected_window: tuple[int, int] = (7, 9)
    shower_max_days_without: int = 3
    min_daily_visits: int = 3
    max_visit_duration_minutes: int = 45
    max_hours_without_visit: int = 8
    sleep_window: tuple[int, int] = (23, 7)   # [ora inizio sonno, ora fine sonno]
    sequence_window_hours: int = 6


class BaselineModel:
    """
    Baseline ibrida:
    - Fase 1 (< 50 sessioni): regole caregiver + statistiche running (Welford)
    - Fase 2 (≥ 50 sessioni): aggiunge Isolation Forest sulle feature numeriche
    """

    ML_THRESHOLD = 50

    def __init__(self, rules: CaregiverRules, persistence_path: Path | None = None):
        self.rules = rules
        self.persistence_path = persistence_path

        self.duration_stats = RunningStats()
        self.gap_stats = RunningStats()
        self._slot_stats: dict[str, RunningStats] = {
            "morning": RunningStats(),
            "afternoon": RunningStats(),
            "evening": RunningStats(),
            "night": RunningStats(),
        }
        self._daily_visits: dict[str, int] = {}
        self._weekly_showers: dict[str, int] = {}
        self._weekly_days: dict[str, int] = {}
        self._seen_hours: set[int] = set()

        self.total_sessions = 0
        self._iso_forest = None
        self._all_feature_vectors: list[list[float]] = []

        if persistence_path and persistence_path.exists():
            self._load(persistence_path)

    # ------------------------------------------------------------------
    # API pubblica
    # ------------------------------------------------------------------

    def update(self, session: BathroomSession) -> None:
        self.total_sessions += 1
        self.duration_stats.update(session.duration_minutes)
        if session.gap_from_last_session_hours > 0:
            self.gap_stats.update(session.gap_from_last_session_hours)

        slot = self._time_slot(session.hour_of_day)
        self._slot_stats[slot].update(session.duration_minutes)
        self._seen_hours.add(session.hour_of_day)

        date_key = session.start_time.date().isoformat()
        self._daily_visits[date_key] = self._daily_visits.get(date_key, 0) + 1

        week_key = session.start_time.isocalendar()[:2]
        wk = f"{week_key[0]}-W{week_key[1]:02d}"
        if session.has_shower:
            self._weekly_showers[wk] = self._weekly_showers.get(wk, 0) + 1
        self._weekly_days[wk] = max(
            self._weekly_days.get(wk, 0), session.start_time.weekday() + 1
        )

        if self.total_sessions >= self.ML_THRESHOLD:
            self._all_feature_vectors.append(self._to_feature_vector(session))
            self._refit_isolation_forest()

        if self.persistence_path:
            self._save(self.persistence_path)

    def isolation_forest_score(self, session: BathroomSession) -> float | None:
        if self._iso_forest is None:
            return None
        fv = self._to_feature_vector(session)
        return float(self._iso_forest.score_samples([fv])[0])

    @property
    def daily_visit_stats(self) -> RunningStats:
        rs = RunningStats()
        for c in self._daily_visits.values():
            rs.update(float(c))
        return rs

    def days_since_last_shower(
        self,
        sessions: list[BathroomSession],
        reference_time: datetime | None = None,
    ) -> float:
        shower_sessions = [s for s in sessions if s.has_shower]
        if not shower_sessions:
            return float("inf")
        last = max(shower_sessions, key=lambda s: s.start_time)
        now = reference_time if reference_time is not None else datetime.now()
        return (now - last.start_time).total_seconds() / 86400.0

    def is_hour_unusual(self, hour: int) -> bool:
        if not self._seen_hours:
            return False
        return all(abs(hour - h) > 2 for h in self._seen_hours)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _time_slot(hour: int) -> str:
        if 6 <= hour < 12:
            return "morning"
        if 12 <= hour < 18:
            return "afternoon"
        if 18 <= hour < 23:
            return "evening"
        return "night"

    @staticmethod
    def _to_feature_vector(session: BathroomSession) -> list[float]:
        return [
            session.duration_minutes,
            session.hour_of_day,
            session.day_of_week,
            float(session.has_shower),
            session.gap_from_last_session_hours,
        ]

    def _refit_isolation_forest(self) -> None:
        try:
            from sklearn.ensemble import IsolationForest
        except ImportError:
            return
        if len(self._all_feature_vectors) < self.ML_THRESHOLD:
            return
        self._iso_forest = IsolationForest(
            n_estimators=100, contamination=0.05, random_state=42
        )
        self._iso_forest.fit(self._all_feature_vectors)

    # ------------------------------------------------------------------
    # Persistenza
    # ------------------------------------------------------------------

    def _save(self, path: Path) -> None:
        data = {
            "total_sessions": self.total_sessions,
            "duration_stats": self.duration_stats.to_dict(),
            "gap_stats": self.gap_stats.to_dict(),
            "slot_stats": {k: v.to_dict() for k, v in self._slot_stats.items()},
            "daily_visits": self._daily_visits,
            "weekly_showers": self._weekly_showers,
            "weekly_days": self._weekly_days,
            "seen_hours": list(self._seen_hours),
            "feature_vectors": self._all_feature_vectors,
        }
        path.write_text(json.dumps(data, indent=2))

    def _load(self, path: Path) -> None:
        data = json.loads(path.read_text())
        self.total_sessions = data["total_sessions"]
        self.duration_stats = RunningStats.from_dict(data["duration_stats"])
        self.gap_stats = RunningStats.from_dict(data["gap_stats"])
        self._slot_stats = {k: RunningStats.from_dict(v) for k, v in data["slot_stats"].items()}
        self._daily_visits = data["daily_visits"]
        self._weekly_showers = data["weekly_showers"]
        self._weekly_days = data["weekly_days"]
        self._seen_hours = set(data["seen_hours"])
        self._all_feature_vectors = data.get("feature_vectors", [])
        if self.total_sessions >= self.ML_THRESHOLD:
            self._refit_isolation_forest()


# ---------------------------------------------------------------------------
# 3. AnomalyDetector
# ---------------------------------------------------------------------------

def _linear_slope(values: list[float]) -> float:
    """Pendenza della retta di regressione su una sequenza di valori."""
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den != 0 else 0.0


class AnomalyDetector:

    ISO_SCORE_THRESHOLD = -0.55
    NO_SHOWER_DAYS_THRESHOLD = 3

    # Deriva: finestra recente e soglia z-score
    DRIFT_WINDOW = 14          # sessioni recenti da confrontare con la baseline
    DRIFT_MIN_BASELINE = 20    # baseline minima prima di attivare drift detection
    DRIFT_Z_THRESHOLD = 2.0    # z-score oltre cui la media recente è "derivata"

    # Sequenze: finestra temporale per pattern anomali
    SEQ_WINDOW_HOURS = 6       # ore da guardare indietro
    SEQ_MIN_SESSIONS = 2       # minimo sessioni nella finestra per attivare il check
    SEQ_MIN_BASELINE = 10

    def __init__(self, baseline: BaselineModel, person_name: str = "La persona"):
        self.baseline = baseline
        self.name = person_name

    def check_session(self, session: BathroomSession) -> list[Anomaly]:
        anomalies: list[Anomaly] = []
        b = self.baseline
        dur_mean = b.duration_stats.mean
        dur_std = b.duration_stats.std

        # DURATION_HIGH — statistica
        if b.total_sessions >= 5 and dur_std > 0:
            if session.duration_minutes > dur_mean + 2 * dur_std:
                anomalies.append(Anomaly(
                    timestamp=session.start_time,
                    anomaly_type="DURATION_HIGH",
                    severity="HIGH",
                    message=(
                        f"{self.name} è in bagno da {session.duration_minutes:.0f} minuti, "
                        f"di solito ci sta circa {dur_mean:.0f}."
                    ),
                    session=self._session_dict(session),
                    baseline_context={"duration_mean": round(dur_mean, 2), "duration_std": round(dur_std, 2)},
                ))

        # DURATION_HIGH — regola caregiver
        if session.duration_minutes > b.rules.max_visit_duration_minutes:
            if not any(a.anomaly_type == "DURATION_HIGH" for a in anomalies):
                anomalies.append(Anomaly(
                    timestamp=session.start_time,
                    anomaly_type="DURATION_HIGH",
                    severity="HIGH",
                    message=(
                        f"{self.name} è in bagno da {session.duration_minutes:.0f} minuti, "
                        f"di solito ci sta circa {dur_mean:.0f}."
                    ),
                    session=self._session_dict(session),
                    baseline_context={"max_visit_duration_minutes": b.rules.max_visit_duration_minutes},
                ))

        # UNUSUAL_TIME
        usual_hours = sorted(b._seen_hours)
        if b.total_sessions >= 10 and b.is_hour_unusual(session.hour_of_day):
            hours_str = ", ".join(f"{h}:00" for h in usual_hours)
            anomalies.append(Anomaly(
                timestamp=session.start_time,
                anomaly_type="UNUSUAL_TIME",
                severity="MEDIUM",
                message=(
                    f"{self.name} è andato in bagno alle {session.hour_of_day}:00"
                ),
                session=self._session_dict(session),
                baseline_context={"seen_hours": usual_hours},
            ))

        # ML_ANOMALY (solo fase 2)
        iso_score = b.isolation_forest_score(session)
        if iso_score is not None and iso_score < self.ISO_SCORE_THRESHOLD:
            if not anomalies:
                anomalies.append(Anomaly(
                    timestamp=session.start_time,
                    anomaly_type="ML_ANOMALY",
                    severity="MEDIUM",
                    message=(
                        f"La visita di {self.name} alle {session.hour_of_day}:00 "
                        f"è risultata insolita rispetto alle sue abitudini."
                    ),
                    session=self._session_dict(session),
                    baseline_context={"isolation_forest_score": round(iso_score, 4)},
                ))

        return anomalies

    def check_gap(self, last_session_end: datetime, now: datetime) -> list[Anomaly]:
        sleep_start, sleep_end = self.baseline.rules.sleep_window
        if self._in_sleep_window(now.hour, sleep_start, sleep_end):
            return []

        # Calcola il gap escludendo le ore di sonno
        gap_hours = self._gap_excluding_sleep(last_session_end, now, sleep_start, sleep_end)
        if gap_hours > self.baseline.rules.max_hours_without_visit:
            return [Anomaly(
                timestamp=now,
                anomaly_type="NO_VISIT_TOO_LONG",
                severity="HIGH",
                message=f"{self.name} non è andato in bagno da {gap_hours:.0f} ore.",
                session=None,
                baseline_context={
                    "max_hours_without_visit": self.baseline.rules.max_hours_without_visit,
                    "gap_hours": round(gap_hours, 2),
                },
            )]
        return []

    @staticmethod
    def _in_sleep_window(hour: int, sleep_start: int, sleep_end: int) -> bool:
        if sleep_start > sleep_end:  # es. 23-7: attraversa mezzanotte
            return hour >= sleep_start or hour < sleep_end
        return sleep_start <= hour < sleep_end

    @staticmethod
    def _gap_excluding_sleep(
        start: datetime, end: datetime, sleep_start: int, sleep_end: int
    ) -> float:
        """Calcola le ore tra start e end escludendo la finestra di sonno."""
        total = (end - start).total_seconds() / 3600.0
        # Stima ore di sonno nel periodo
        sleep_hours_per_day = (
            (24 - sleep_start + sleep_end) if sleep_start > sleep_end
            else (sleep_end - sleep_start)
        )
        days = total / 24.0
        excluded = days * sleep_hours_per_day
        return max(total - excluded, 0.0)

    def check_daily_frequency(self, date: datetime, count: int) -> list[Anomaly]:
        b = self.baseline
        dvs = b.daily_visit_stats
        usual = round(dvs.mean) if dvs.n >= 3 else b.rules.min_daily_visits
        if count < b.rules.min_daily_visits:
            return [Anomaly(
                timestamp=date,
                anomaly_type="FREQUENCY_ANOMALY",
                severity="MEDIUM",
                message=(
                    f"{self.name} è andato in bagno solo {count} "
                    f"{'volta' if count == 1 else 'volte'} oggi, "
                    f"di solito ci va {usual} volte."
                ),
                session=None,
                baseline_context={"min_daily_visits": b.rules.min_daily_visits, "actual_count": count},
            )]
        if dvs.n >= 7 and dvs.std > 0 and count > dvs.mean + 2 * dvs.std:
            return [Anomaly(
                timestamp=date,
                anomaly_type="FREQUENCY_ANOMALY",
                severity="LOW",
                message=(
                    f"{self.name} è andato in bagno {count} volte oggi, "
                    f"di solito ci va {usual} volte."
                ),
                session=None,
                baseline_context={"daily_mean": round(dvs.mean, 2), "daily_std": round(dvs.std, 2), "actual_count": count},
            )]
        return []

    def check_shower_regularity(
        self,
        all_sessions: list[BathroomSession],
        reference_time: datetime | None = None,
    ) -> list[Anomaly]:
        threshold = self.baseline.rules.shower_max_days_without
        days = self.baseline.days_since_last_shower(all_sessions, reference_time)
        if days > threshold:
            return [Anomaly(
                timestamp=reference_time if reference_time is not None else datetime.now(),
                anomaly_type="MISSING_SHOWER",
                severity="MEDIUM",
                message=f"{self.name} non ha fatto la doccia da {days:.0f} giorni.",
                session=None,
                baseline_context={
                    "no_shower_threshold_days": threshold,
                    "days_since_last_shower": round(days, 2),
                },
            )]
        return []

    def check_drift(self, recent_sessions: list[BathroomSession]) -> list[Anomaly]:
        """
        Confronta la media delle ultime DRIFT_WINDOW sessioni con la baseline storica.
        Un cambiamento graduale ma sistematico si manifesta come una media recente
        che si sposta oltre DRIFT_Z_THRESHOLD deviazioni standard dalla media storica.
        Rileva anche la direzione del trend con regressione lineare.
        """
        b = self.baseline
        if b.total_sessions < self.DRIFT_MIN_BASELINE:
            return []
        if len(recent_sessions) < self.DRIFT_WINDOW:
            return []

        window = recent_sessions[-self.DRIFT_WINDOW:]
        window_durations = [s.duration_minutes for s in window]
        window_mean = sum(window_durations) / len(window_durations)

        overall_mean = b.duration_stats.mean
        overall_std = b.duration_stats.std
        if overall_std == 0:
            return []

        # z-score della media della finestra rispetto alla baseline
        # diviso sqrt(n) perché è la distribuzione della media campionaria
        z = (window_mean - overall_mean) / (overall_std / math.sqrt(self.DRIFT_WINDOW))
        slope = _linear_slope(window_durations)

        anomalies = []

        if z > self.DRIFT_Z_THRESHOLD:
            anomalies.append(Anomaly(
                timestamp=window[-1].start_time,
                anomaly_type="DURATION_DRIFT_UP",
                severity="MEDIUM",
                message=(
                    f"Nelle ultime due settimane {self.name} passa più tempo in bagno del solito: "
                    f"in media {window_mean:.0f} minuti invece di {overall_mean:.0f}."
                ),
                session=None,
                baseline_context={
                    "window_mean": round(window_mean, 2),
                    "overall_mean": round(overall_mean, 2),
                    "overall_std": round(overall_std, 2),
                    "z_score": round(z, 2),
                    "slope_min_per_session": round(slope, 3),
                },
            ))
        elif z < -self.DRIFT_Z_THRESHOLD:
            anomalies.append(Anomaly(
                timestamp=window[-1].start_time,
                anomaly_type="DURATION_DRIFT_DOWN",
                severity="MEDIUM",
                message=(
                    f"Nelle ultime due settimane {self.name} passa meno tempo in bagno del solito: "
                    f"in media {window_mean:.0f} minuti invece di {overall_mean:.0f}."
                ),
                session=None,
                baseline_context={
                    "window_mean": round(window_mean, 2),
                    "overall_mean": round(overall_mean, 2),
                    "overall_std": round(overall_std, 2),
                    "z_score": round(z, 2),
                    "slope_min_per_session": round(slope, 3),
                },
            ))

        # Drift nell'orario medio delle visite
        window_hours = [s.hour_of_day for s in window]
        hour_mean_recent = sum(window_hours) / len(window_hours)
        all_hours = list(b._seen_hours)
        if all_hours:
            hour_mean_baseline = sum(all_hours) / len(all_hours)
            hour_drift = abs(hour_mean_recent - hour_mean_baseline)
            if hour_drift > 2.5 and b.total_sessions >= self.DRIFT_MIN_BASELINE:
                direction = "più tardi" if hour_mean_recent > hour_mean_baseline else "prima"
                anomalies.append(Anomaly(
                    timestamp=window[-1].start_time,
                    anomaly_type="TIMING_DRIFT",
                    severity="LOW",
                    message=(
                        f"Ultimamente {self.name} va in bagno {direction} del solito."
                    ),
                    session=None,
                    baseline_context={
                        "recent_hour_mean": round(hour_mean_recent, 2),
                        "baseline_hour_mean": round(hour_mean_baseline, 2),
                        "drift_hours": round(hour_drift, 2),
                    },
                ))

        return anomalies

    def check_sequence(self, recent_sessions: list[BathroomSession]) -> list[Anomaly]:
        """
        Guarda tutte le sessioni nelle ultime SEQ_WINDOW_HOURS ore e cerca pattern:
        - Tutte brevi → possibile malessere
        - Durate in crescita continua → peggioramento progressivo
        - Tutte in orari insoliti → alterazione del ritmo
        """
        b = self.baseline
        if b.total_sessions < self.SEQ_MIN_BASELINE or not recent_sessions:
            return []

        window_hours = b.rules.sequence_window_hours
        cutoff = recent_sessions[-1].start_time - timedelta(hours=window_hours)
        window = [s for s in recent_sessions if s.start_time >= cutoff]

        if len(window) < self.SEQ_MIN_SESSIONS:
            return []

        durations = [s.duration_minutes for s in window]
        dur_mean = b.duration_stats.mean
        dur_std = b.duration_stats.std
        if dur_std == 0:
            return []

        anomalies = []
        n = len(window)

        # Tutte le sessioni nella finestra sono brevi
        short_threshold = dur_mean - 0.8 * dur_std
        if all(d < short_threshold for d in durations):
            anomalies.append(Anomaly(
                timestamp=window[-1].start_time,
                anomaly_type="SHORT_SESSIONS_WINDOW",
                severity="MEDIUM",
                message=(
                    f"{self.name} è stato in bagno {n} volte nelle ultime "
                    f"{window_hours} ore, ogni volta per meno di "
                    f"{short_threshold:.0f} minuti."
                ),
                session=self._session_dict(window[-1]),
                baseline_context={
                    "durations": [round(d, 1) for d in durations],
                    "short_threshold": round(short_threshold, 2),
                    "window_hours": window_hours,
                },
            ))

        # Durate in crescita continua nella finestra
        if len(durations) >= 3 and all(
            durations[i] < durations[i + 1] for i in range(len(durations) - 1)
        ):
            slope = _linear_slope(durations)
            if slope > dur_std * 0.3:
                anomalies.append(Anomaly(
                    timestamp=window[-1].start_time,
                    anomaly_type="DURATION_TREND_UP",
                    severity="MEDIUM",
                    message=(
                        f"Nelle ultime {window_hours} ore le visite di {self.name} "
                        f"al bagno sono durate sempre di più: "
                        f"{', '.join(f'{d:.0f}' for d in durations)} minuti."
                    ),
                    session=self._session_dict(window[-1]),
                    baseline_context={
                        "durations": [round(d, 1) for d in durations],
                        "slope": round(slope, 3),
                    },
                ))

        # Tutti gli orari insoliti nella finestra
        if all(b.is_hour_unusual(s.hour_of_day) for s in window):
            hours = [f"{s.hour_of_day}:00" for s in window]
            anomalies.append(Anomaly(
                timestamp=window[-1].start_time,
                anomaly_type="UNUSUAL_TIME_WINDOW",
                severity="HIGH",
                message=(
                    f"{self.name} è andato in bagno {n} volte nelle ultime "
                    f"{window_hours} ore, sempre in orari insoliti: "
                    f"{', '.join(hours)}."
                ),
                session=self._session_dict(window[-1]),
                baseline_context={
                    "hours": hours,
                    "seen_hours": sorted(b._seen_hours),
                    "window_hours": window_hours,
                },
            ))

        return anomalies

    @staticmethod
    def _session_dict(session: BathroomSession) -> dict:
        d = asdict(session)
        d["start_time"] = session.start_time.isoformat()
        d["end_time"] = session.end_time.isoformat()
        return d
