"""Alertmanager webhook v4 payload builders for the e2e test."""
import time

EXTERNAL_URL = "http://alertmanager.test:9093"


def alert(name, severity="critical", status="firing", labels=None, annotations=None, fingerprint=None, starts_at=None):
    lab = {"alertname": name, "severity": severity}
    lab.update(labels or {})
    ann = {"summary": f"{name} is firing", "description": f"description for {name}"}
    ann.update(annotations or {})
    return {
        "status": status, "labels": lab, "annotations": ann,
        "startsAt": starts_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600)),
        "endsAt": "0001-01-01T00:00:00Z" if status == "firing" else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "generatorURL": "http://prometheus.test:9090/graph?g0.expr=up",
        "fingerprint": fingerprint or f"fp{abs(hash((name, status, str(labels)))) % 10 ** 12:012d}",
    }


def alertmanager(alerts, status=None, group_labels=None, common_labels=None, receiver="n8n-triage"):
    firing = [a for a in alerts if a["status"] == "firing"]
    common = dict(common_labels or {})
    if not common and alerts:
        keys = set(alerts[0]["labels"])
        for a in alerts[1:]:
            keys &= {k for k in a["labels"] if a["labels"][k] == alerts[0]["labels"][k]}
        common = {k: alerts[0]["labels"][k] for k in sorted(keys)}
    return {
        "version": "4", "groupKey": "{}:{alertname=\"%s\"}" % alerts[0]["labels"]["alertname"],
        "truncatedAlerts": 0, "status": status or ("firing" if firing else "resolved"), "receiver": receiver,
        "groupLabels": group_labels or {"alertname": alerts[0]["labels"]["alertname"]},
        "commonLabels": common, "commonAnnotations": {}, "externalURL": EXTERNAL_URL, "alerts": alerts,
    }
