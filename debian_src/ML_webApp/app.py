import logging
from datetime import datetime

from flask import Flask, render_template, request, jsonify

import debian_src.ML_webApp.processor as processor
from debian_src.ML_webApp.monitoring import RawEvent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = Flask(__name__)


@app.route("/")
def dashboard():
    data = processor.get_snapshot()
    return render_template(
        "dashboard.html",
        person_name=data["person_name"],
        total_sessions=data["total_sessions"],
        ml_enabled=data["ml_enabled"],
        alerts=data["alerts"],
        sessions=data["sessions"],
    )


@app.route("/event", methods=["POST"])
def ingest_event():
    body = request.get_json(force=True, silent=True)
    if not body:
        return jsonify({"error": "invalid JSON"}), 400

    sensor = body.get("sensor")
    if sensor not in ("door", "shower"):
        return jsonify({"error": "sensor must be 'door' or 'shower'"}), 400

    value = body.get("value")
    if not isinstance(value, bool):
        return jsonify({"error": "value must be a boolean"}), 400

    ts_raw = body.get("timestamp")
    if ts_raw is not None:
        try:
            if isinstance(ts_raw, (int, float)):
                ts = datetime.fromtimestamp(ts_raw)
            else:
                ts = datetime.fromisoformat(str(ts_raw))
        except (ValueError, OSError):
            return jsonify({"error": "timestamp must be ISO 8601 string or unix timestamp"}), 400
    else:
        ts = datetime.now()

    processor.ingest_event(RawEvent(timestamp=ts, sensor=sensor, value=value))
    return jsonify({"ok": True}), 200


@app.route("/debug")
def debug():
    data = processor.get_snapshot()
    pending = processor.get_pending()
    return jsonify({
        "total_sessions": data["total_sessions"],
        "alerts_count": len(data["alerts"]),
        "pending_events": pending,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
