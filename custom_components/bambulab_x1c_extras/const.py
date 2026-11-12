"""Bambu Lab X1C: Spezifikation (Port von bambulab_x1c.yaml)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from .framework import Field, Spec

DOMAIN = "bambulab_x1c_extras"
TITLE = "Bambu Lab X1C Zusatz"
PREFIX = "x1c"
VERSION = "1.0.0"
DESCRIPTION = "Statistik, Kosten, Meldungen und Filament-Warnungen für den X1C. Baut auf der Integration ha-bambulab auf."
DASH = "dashboard-x1c"

FIELDS = [
    Field("praefix", "x1c_seriennummer", "text", "Entitäts-Präfix der Bambu-Integration (z. B. x1c_<seriennummer>)"),
    Field("notify_person1", "mobile_app_mein_handy", "text", "Notify-Dienst Person 1"),
    Field("notify_person2", "mobile_app_partner_handy", "text", "Notify-Dienst Person 2"),
]

RUNNING = ("running", "printing", "prepare", "pause", "paused", "slicing")
STATUS_DE = {
    "running": "Druckt", "printing": "Druckt", "prepare": "Vorbereitung", "pause": "Pausiert", "paused": "Pausiert",
    "finish": "Fertig", "failed": "Fehlgeschlagen", "idle": "Bereit", "offline": "Offline", "slicing": "Slicen",
    "init": "Initialisierung", "unknown": "Unbekannt", "unavailable": "Nicht verbunden",
}


def p(rt) -> str:
    return rt.cfg("praefix") or "x1c_seriennummer"


def eid(rt, kind: str, suffix: str) -> str:
    return f"{kind}.{p(rt)}_{suffix}"


def _f(rt, entity: str, default: float = 0.0) -> float:
    try:
        return float(rt.state(entity))
    except (TypeError, ValueError):
        return default


def druckt(rt) -> bool:
    return rt.state(eid(rt, "sensor", "druckstatus")) in RUNNING


def status(rt) -> str:
    s = rt.state(eid(rt, "sensor", "druckstatus")) or "unknown"
    return STATUS_DE.get(s, s.capitalize())


def status_icon(rt) -> str:
    s = rt.state(eid(rt, "sensor", "druckstatus"))
    if s in ("running", "printing"):
        return "mdi:printer-3d-nozzle"
    if s in ("pause", "paused"):
        return "mdi:pause-circle"
    if s == "finish":
        return "mdi:check-circle"
    if s == "failed":
        return "mdi:alert-circle"
    if s == "prepare":
        return "mdi:progress-wrench"
    if s in ("offline", "unavailable"):
        return "mdi:printer-3d-off"
    return "mdi:printer-3d"


def restzeit_min(rt) -> int:
    e = eid(rt, "sensor", "verbleibende_zeit")
    r = _f(rt, e)
    u = rt.attr(e, "unit_of_measurement")
    return int(round(r * 60 if u == "h" else (r / 60 if u == "s" else r)))


def restzeit(rt) -> str:
    m = restzeit_min(rt)
    if not druckt(rt):
        return "–"
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def fertig_um(rt) -> str:
    if not druckt(rt):
        return "–"
    from .framework import now_local

    now = now_local()
    ende = now + timedelta(minutes=restzeit_min(rt))
    if ende.date() == now.date():
        tag = "heute"
    elif ende.date() == (now + timedelta(days=1)).date():
        tag = "morgen"
    else:
        tag = ende.strftime("%d.%m.")
    return f"{tag} {ende.strftime('%H:%M')} Uhr"


def schicht(rt) -> str:
    return f"{int(_f(rt, eid(rt, 'sensor', 'aktuelle_schicht')))} / {int(_f(rt, eid(rt, 'sensor', 'gesamtzahl_der_schichten')))}"


def dauer_h(rt) -> float:
    from homeassistant.util import dt as dt_util

    s = dt_util.parse_datetime(rt.state(eid(rt, "sensor", "startzeit")) or "")
    e = dt_util.parse_datetime(rt.state(eid(rt, "sensor", "vrs_fertigstellung")) or "")
    if s and e and e > s:
        return round((e - s).total_seconds() / 3600, 2)
    return 0.0


def filamentkosten(rt) -> float:
    return round(_f(rt, eid(rt, "sensor", "gewicht_des_drucks")) / 1000 * rt.flt("x1c_filamentpreis"), 2)


def stromkosten(rt) -> float:
    return round(dauer_h(rt) * rt.flt("x1c_leistung") / 1000 * rt.flt("x1c_strompreis"), 2)


def kosten(rt) -> float:
    return round(filamentkosten(rt) + stromkosten(rt), 2)


def erfolgsquote(rt) -> float:
    ok, nok = int(rt.mem.get("ok", 0)), int(rt.mem.get("nok", 0))
    return round(ok / (ok + nok) * 100, 1) if ok + nok else 0


def aktives_filament(rt) -> str:
    e = eid(rt, "sensor", "aktiver_slot")
    n, t = rt.attr(e, "name"), rt.attr(e, "type")
    if n or t:
        return f"{n or ''}{' (' + t + ')' if t else ''}"
    return rt.state(e) or ""


def aktives_filament_attrs(rt) -> dict:
    return {"farbe": str(rt.attr(eid(rt, "sensor", "aktiver_slot"), "color") or "")[:7]}


def _slot(rt, i: int) -> str:
    return eid(rt, "sensor", f"ams_1_slot_{i}_2")


def ams_belegt(rt) -> int:
    n = 0
    for i in range(1, 5):
        e = _slot(rt, i)
        if rt.state(e) not in (None, "unknown", "unavailable", "") and not rt.attr(e, "empty"):
            n += 1
    return n


def knappe_slots(rt) -> list[str]:
    grenze = rt.flt("x1c_filament_rest_grenze", 10)
    out = []
    for i in range(1, 5):
        e = _slot(rt, i)
        r = rt.attr(e, "remain")
        if isinstance(r, (int, float)) and not isinstance(r, bool) and 0 <= r <= grenze and not rt.attr(e, "empty"):
            out.append(f"Slot {i} ({rt.attr(e, 'name') or '?'}, {r} %)")
    return out


def filament_knapp(rt) -> bool:
    return bool(knappe_slots(rt))


def hms_attrs_state(rt):
    return rt.hass.states.get(eid(rt, "binary_sensor", "hms_fehler"))


def hms_ernst(rt) -> bool:
    st = hms_attrs_state(rt)
    if st is None or st.state != "on":
        return False
    return any(k.endswith("-Severity") and v in ("serious", "fatal") for k, v in st.attributes.items())


def hms_text(rt) -> str:
    st = hms_attrs_state(rt)
    if st is None or st.state != "on":
        return "Keine Meldungen"
    l = [str(v) for k, v in st.attributes.items() if k.endswith("-Error") or k.endswith("-Fehler")]
    return " | ".join(l) if l else "Fehler aktiv – Details im Gerät"


def hms_count(rt) -> int:
    st = hms_attrs_state(rt)
    if st is None:
        return 0
    c = st.attributes.get("Count")
    if isinstance(c, (int, float)):
        return int(c)
    return 1 if st.state == "on" else 0


def hms_attrs(rt) -> dict:
    return {"text": hms_text(rt)}


def druckzeit(rt, tage: int) -> float:
    """Druckzeit in Stunden, heute (tage=0) oder die letzten N Tage."""
    daily = rt.mem.get("daily", {})
    heute = datetime.now().astimezone().date()
    total = 0.0
    for i in range(0 if tage == 0 else tage):
        d = (heute - timedelta(days=i)).isoformat()
        total += float(daily.get(d, 0))
        if tage == 0:
            break
    since = rt.mem.get("since")
    if since:
        laufend = time.time() - since
        # laufender Druck zählt komplett zur Statistik (vereinfacht: heute)
        total += laufend
    return round(total / 3600, 2)


SOLL = [
    "sensor:druckstatus", "sensor:aktueller_arbeitsschritt", "sensor:druckfortschritt", "sensor:verbleibende_zeit",
    "sensor:startzeit", "sensor:vrs_fertigstellung", "sensor:aktuelle_schicht", "sensor:gesamtzahl_der_schichten",
    "sensor:name_der_aufgabe", "sensor:gcode_dateiname", "sensor:gewicht_des_drucks", "sensor:drucklange",
    "sensor:druckbett_typ", "sensor:temperatur_der_duse", "sensor:zieltemperatur_der_duse",
    "sensor:druckbetttemperatur", "sensor:zieltemperatur_des_druckbett", "sensor:temperatur_im_druckraum",
    "sensor:aktiver_slot", "sensor:ams_1_slot_1_2", "sensor:ams_1_slot_2_2", "sensor:ams_1_slot_3_2",
    "sensor:ams_1_slot_4_2", "sensor:ams_1_luftfeuchtigkeit_2", "sensor:ams_1_temperatur_2",
    "binary_sensor:online", "binary_sensor:hms_fehler", "binary_sensor:gehausetur", "binary_sensor:firmware_status",
    "binary_sensor:druckfehler", "light:druckraumbeleuchtung", "camera:kamera", "image:titelbild",
]


def fehlend(rt) -> list[str]:
    vorhanden = set(rt.hass.states.async_entity_ids())
    out = []
    for item in SOLL:
        kind, suffix = item.split(":")
        e = eid(rt, kind, suffix)
        if e not in vorhanden:
            out.append(e)
    return out


def check(rt) -> str:
    f = fehlend(rt)
    return "OK" if not f else f"{len(f)} fehlen"


def check_attrs(rt) -> dict:
    return {"fehlend": fehlend(rt)}


SPECS = [
    Spec("switch", "x1c_benachrichtigungen", "X1C Benachrichtigungen", "mdi:bell-ring-outline", True),
    Spec("switch", "x1c_licht_automatik", "X1C Kammerlicht automatisch", "mdi:lightbulb-auto-outline", False),
    Spec("switch", "x1c_ziel_ha", "Meldung in Home Assistant", "mdi:bell", True),
    Spec("switch", "x1c_ziel_person1", "Push an Person 1", "mdi:cellphone-message", True),
    Spec("switch", "x1c_ziel_person2", "Push an Person 2", "mdi:cellphone-message", False),
    Spec("number", "x1c_filamentpreis", "X1C Filamentpreis", "mdi:currency-eur", 20, 0, 200, 0.5, "€/kg", "box"),
    Spec("number", "x1c_strompreis", "X1C Strompreis", "mdi:flash", 0.30, 0, 2, 0.01, "€/kWh", "box"),
    Spec("number", "x1c_leistung", "X1C mittlere Leistung beim Drucken", "mdi:lightning-bolt-outline", 150, 0, 1500, 5, "W", "box"),
    Spec("number", "x1c_ams_feuchte_grenze", "X1C AMS Feuchte-Grenzwert", "mdi:water-percent-alert", 40, 10, 90, 1, "%", "slider"),
    Spec("number", "x1c_filament_rest_grenze", "X1C Filament-Restwarnung", "mdi:printer-3d-nozzle-alert-outline", 10, 1, 50, 1, "%", "slider"),
    Spec("number", "x1c_filament_gesamt", "X1C Filament verbraucht gesamt", "mdi:weight-gram", 0, 0, 10000000, 0.1, "g", "box"),
    Spec("binary_sensor", "x1c_druckt", "X1C druckt", "mdi:printer-3d-nozzle", fn=druckt),
    Spec("binary_sensor", "x1c_hms_ernst", "X1C HMS ernst", "mdi:alert-octagon", device_class="problem", fn=hms_ernst),
    Spec("binary_sensor", "x1c_filament_fast_leer", "X1C Filament fast leer", "mdi:printer-3d-nozzle-alert", device_class="problem", fn=filament_knapp, attrs=lambda rt: {"slots": ", ".join(knappe_slots(rt))}),
    Spec("sensor", "x1c_status", "X1C Status", fn=status, attrs=lambda rt: {"icon_hint": status_icon(rt)}),
    Spec("sensor", "x1c_restzeit_minuten", "X1C Restzeit Minuten", unit="min", device_class="duration", state_class="measurement", fn=restzeit_min),
    Spec("sensor", "x1c_restzeit", "X1C Restzeit", "mdi:timer-sand", fn=restzeit),
    Spec("sensor", "x1c_fertig_um", "X1C Fertig um", "mdi:clock-end", fn=fertig_um),
    Spec("sensor", "x1c_schicht", "X1C Schicht", "mdi:layers-triple", fn=schicht),
    Spec("sensor", "x1c_druckdauer_gesamt", "X1C Druckdauer gesamt", unit="h", fn=dauer_h),
    Spec("sensor", "x1c_filamentkosten_druck", "X1C Filamentkosten Druck", "mdi:cash", unit="€", fn=filamentkosten),
    Spec("sensor", "x1c_stromkosten_druck", "X1C Stromkosten Druck", "mdi:cash-plus", unit="€", fn=stromkosten),
    Spec("sensor", "x1c_kosten_druck_gesamt", "X1C Kosten Druck gesamt", "mdi:cash-multiple", unit="€", fn=kosten),
    Spec("sensor", "x1c_erfolgsquote", "X1C Erfolgsquote", "mdi:percent-circle-outline", unit="%", fn=erfolgsquote),
    Spec("sensor", "x1c_aktives_filament", "X1C Aktives Filament", "mdi:printer-3d-nozzle-outline", fn=aktives_filament, attrs=aktives_filament_attrs),
    Spec("sensor", "x1c_ams_belegt", "X1C AMS belegt", "mdi:tray-full", unit="Slots", fn=ams_belegt),
    Spec("sensor", "x1c_hms_meldungen", "X1C HMS Meldungen", "mdi:alert-octagon-outline", fn=hms_count, attrs=hms_attrs),
    Spec("sensor", "x1c_druckzeit_heute", "X1C Druckzeit heute", "mdi:timer", unit="h", fn=lambda rt: druckzeit(rt, 0)),
    Spec("sensor", "x1c_druckzeit_7_tage", "X1C Druckzeit 7 Tage", "mdi:timer", unit="h", fn=lambda rt: druckzeit(rt, 7)),
    Spec("sensor", "x1c_druckzeit_30_tage", "X1C Druckzeit 30 Tage", "mdi:timer", unit="h", fn=lambda rt: druckzeit(rt, 30)),
    Spec("sensor", "x1c_drucke_erfolgreich", "X1C Drucke erfolgreich", "mdi:check-circle-outline", state_class="total_increasing", fn=lambda rt: int(rt.mem.get("ok", 0))),
    Spec("sensor", "x1c_drucke_fehlgeschlagen", "X1C Drucke fehlgeschlagen", "mdi:close-circle-outline", state_class="total_increasing", fn=lambda rt: int(rt.mem.get("nok", 0))),
    Spec("sensor", "x1c_letzter_druck", "X1C Letzter Druck", "mdi:file-cad-box", fn=lambda rt: rt.mem.get("letzter") or None),
    Spec("sensor", "x1c_letzter_druck_ende", "X1C Letzter Druck beendet", "mdi:calendar-check", fn=lambda rt: rt.mem.get("ende") or None),
    Spec("sensor", "x1c_entitaeten_check", "X1C Entitäten-Check", "mdi:clipboard-check-outline", fn=check, attrs=check_attrs),
    Spec("button", "x1c_test", "Testmeldung senden", "mdi:bell-check", press="test"),
    Spec("button", "x1c_zaehler_zuruecksetzen", "Zähler zurücksetzen", "mdi:counter", press="zaehler_reset"),
    Spec("button", "x1c_standardwerte", "Standardwerte setzen", "mdi:restore", press="reset"),
]

SECTIONS = [
    ("Drucker", ["x1c_status", "x1c_druckt", "x1c_restzeit", "x1c_fertig_um", "x1c_schicht", "x1c_aktives_filament", "x1c_ams_belegt", "x1c_hms_meldungen"]),
    ("Kosten", ["x1c_filamentkosten_druck", "x1c_stromkosten_druck", "x1c_kosten_druck_gesamt", "x1c_filamentpreis", "x1c_strompreis", "x1c_leistung"]),
    ("Statistik", ["x1c_druckzeit_heute", "x1c_druckzeit_7_tage", "x1c_druckzeit_30_tage", "x1c_drucke_erfolgreich", "x1c_drucke_fehlgeschlagen", "x1c_erfolgsquote", "x1c_filament_gesamt", "x1c_letzter_druck", "x1c_letzter_druck_ende", "x1c_zaehler_zuruecksetzen"]),
    ("Warnungen", ["x1c_benachrichtigungen", "x1c_ziel_ha", "x1c_ziel_person1", "x1c_ziel_person2", "x1c_ams_feuchte_grenze", "x1c_filament_rest_grenze", "x1c_filament_fast_leer", "x1c_hms_ernst", "x1c_licht_automatik", "x1c_test", "x1c_standardwerte"]),
    ("Diagnose", ["x1c_entitaeten_check"]),
]
