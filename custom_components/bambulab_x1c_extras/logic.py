"""Bambu Lab X1C: Fachlogik (Port der Automationen aus bambulab_x1c.yaml)."""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.core import Event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import RUNNING, eid, hms_ernst, hms_text, knappe_slots, restzeit, fertig_um, kosten
from .framework import LogicBase

_LOGGER = logging.getLogger(__name__)


class Logic(LogicBase):
    def __init__(self, rt) -> None:
        super().__init__(rt)
        rt.mem.update(ok=0, nok=0, letzter="", ende="", daily={}, since=None, halb=False, timers={})
        self.store = Store(rt.hass, 1, f"{rt.domain}_{rt.entry.entry_id}")
        rt.actions.update(test=self._test, zaehler_reset=self._zaehler_reset, reset=self._reset)

    # -- Start ---------------------------------------------------------------
    async def async_start(self) -> None:
        rt = self.rt
        data = await self.store.async_load() or {}
        for k in ("ok", "nok", "letzter", "ende", "daily"):
            if k in data:
                rt.mem[k] = data[k]
        if rt.state(eid(rt, "sensor", "druckstatus")) in RUNNING:
            rt.mem["since"] = data.get("since") or time.time()
        ids = [
            eid(rt, "sensor", "druckstatus"), eid(rt, "sensor", "druckfortschritt"),
            eid(rt, "binary_sensor", "hms_fehler"), eid(rt, "binary_sensor", "gehausetur"),
            eid(rt, "sensor", "ams_1_luftfeuchtigkeit_2"), eid(rt, "binary_sensor", "online"),
            eid(rt, "binary_sensor", "firmware_status"),
        ] + [eid(rt, "sensor", f"ams_1_slot_{i}_2") for i in range(1, 5)]
        rt.track_state(ids, self._changed)
        rt.track_interval(self._tick, timedelta(minutes=1))
        rt.refresh()

    async def _speichern(self) -> None:
        m = self.rt.mem
        await self.store.async_save({k: m[k] for k in ("ok", "nok", "letzter", "ende", "daily")} | {"since": m["since"]})

    async def _reset(self) -> None:
        self.rt.reset_defaults()

    async def _zaehler_reset(self) -> None:
        self.rt.mem.update(ok=0, nok=0, daily={})
        await self._speichern()
        self.rt.refresh()

    async def _tick(self, _now) -> None:
        self.rt.refresh()

    # -- Meldungen -----------------------------------------------------------
    async def melden(self, titel: str, text: str, bild: bool = False, push_extra: dict | None = None) -> None:
        rt = self.rt
        if not rt.on("x1c_benachrichtigungen"):
            return
        if rt.on("x1c_ziel_ha"):
            await rt.pn_create(titel, text, "x1c_meldung")
        data: dict = {"push": {"thread-id": "x1c"}}
        if push_extra:
            data["push"].update(push_extra)
        if bild:
            url = rt.attr(eid(rt, "image", "titelbild"), "entity_picture")
            if url:
                data["image"] = url
        await rt.notify(titel, text, data)

    async def _test(self) -> None:
        await self.melden("🖨️ X1C – Testmeldung", "Das ist eine Testmeldung.")

    def _auftrag(self) -> str:
        return self.rt.state(eid(self.rt, "sensor", "name_der_aufgabe")) or ""

    def _timer(self, name: str, seconds: float, cb) -> None:
        self._cancel(name)
        self.rt.mem["timers"][name] = self.rt.later(seconds, cb)

    def _cancel(self, name: str) -> None:
        unsub = self.rt.mem["timers"].pop(name, None)
        if unsub:
            unsub()

    # -- Zustandsänderungen ---------------------------------------------------
    async def _changed(self, event: Event) -> None:
        rt = self.rt
        ent = event.data["entity_id"]
        old, new = event.data.get("old_state"), event.data.get("new_state")
        rt.refresh()
        if new is None:
            return
        o = old.state if old else None
        n = new.state
        if ent == eid(rt, "sensor", "druckstatus"):
            await self._status(o, n)
        elif ent == eid(rt, "sensor", "druckfortschritt"):
            await self._fortschritt(n)
        elif ent == eid(rt, "binary_sensor", "hms_fehler") and n == "on":
            await self._hms()
        elif ent == eid(rt, "binary_sensor", "gehausetur"):
            self._tuer(n)
        elif ent == eid(rt, "sensor", "ams_1_luftfeuchtigkeit_2"):
            self._feuchte()
        elif ent == eid(rt, "binary_sensor", "online"):
            self._online(o, n)
        elif ent == eid(rt, "binary_sensor", "firmware_status") and n == "on":
            await self.melden("⬆️ X1C – Firmware-Update", "Für den X1C ist eine neue Firmware verfügbar.")
        elif ent.startswith(f"sensor.{rt.cfg('praefix')}_ams_1_slot_"):
            await self._filament()

    async def _status(self, o: str | None, n: str) -> None:
        rt = self.rt
        gestartet = n in ("running", "printing") and o not in ("pause", "paused", "running", "printing", "unavailable", "unknown", None)
        if gestartet:
            rt.mem["since"] = time.time()
            rt.mem["halb"] = False
            rt.mem["letzter"] = self._auftrag()[:250]
            if rt.on("x1c_licht_automatik"):
                await rt.call("light", "turn_on", {"entity_id": eid(rt, "light", "druckraumbeleuchtung")})
            await self._speichern()
            self._timer("start", 30, self._start_meldung)
        if n in ("pause", "paused") and o not in ("pause", "paused"):
            await self.melden("⏸️ X1C – Druck pausiert", f"Phase: {rt.state(eid(rt, 'sensor', 'aktueller_arbeitsschritt'))}. {hms_text(rt) if hms_text(rt) != 'Keine Meldungen' else ''}".strip())
        if n == "finish" and o not in ("unavailable", "unknown", "finish"):
            await self._ende(True)
        if n == "failed" and o not in ("unavailable", "unknown", "failed"):
            await self._ende(False)
        if n not in RUNNING and o in RUNNING:
            # Druck beendet: Laufzeit der Statistik zuschlagen, Licht später aus
            self._laufzeit_buchen()
            if rt.on("x1c_licht_automatik"):
                self._timer("licht", 600, self._licht_aus)
        elif n in RUNNING:
            self._cancel("licht")
            if rt.mem.get("since") is None:
                rt.mem["since"] = time.time()

    def _laufzeit_buchen(self) -> None:
        rt = self.rt
        since = rt.mem.get("since")
        rt.mem["since"] = None
        if not since:
            return
        tag = dt_util.now().date().isoformat()
        rt.mem["daily"][tag] = float(rt.mem["daily"].get(tag, 0)) + (time.time() - since)
        grenze = (dt_util.now().date() - timedelta(days=35)).isoformat()
        rt.mem["daily"] = {d: s for d, s in rt.mem["daily"].items() if d >= grenze}
        rt.spawn(self._speichern())

    async def _start_meldung(self, _now) -> None:
        rt = self.rt
        await self.melden(
            "🖨️ X1C – Druck gestartet",
            f"{self._auftrag()} · ca. {restzeit(rt)} · fertig {fertig_um(rt)}",
            bild=True,
        )

    async def _ende(self, erfolg: bool) -> None:
        rt = self.rt
        rt.mem["ok" if erfolg else "nok"] += 1
        rt.mem["ende"] = dt_util.now().isoformat()
        gewicht = rt.num(eid(rt, "sensor", "gewicht_des_drucks"), 0) or 0
        if erfolg:
            rt.set("x1c_filament_gesamt", round(rt.flt("x1c_filament_gesamt") + gewicht, 1))
        await self._speichern()
        if erfolg:
            await self.melden(
                "✅ X1C – Druck fertig",
                f"{self._auftrag()} ist fertig. {gewicht:g} g · ca. {kosten(rt)} €",
                bild=True,
                push_extra={"interruption-level": "time-sensitive"},
            )
        else:
            fort = rt.state(eid(rt, "sensor", "druckfortschritt"))
            await self.melden("❌ X1C – Druck fehlgeschlagen", f"{self._auftrag()} bei {fort} % beendet. {hms_text(rt)}")
        rt.refresh()

    async def _fortschritt(self, n: str) -> None:
        rt = self.rt
        try:
            v = float(n)
        except ValueError:
            return
        if v < 50:
            rt.mem["halb"] = False
        elif not rt.mem["halb"] and rt.state(eid(rt, "sensor", "druckstatus")) in RUNNING:
            rt.mem["halb"] = True
            await self.melden("⏳ X1C – 50 %", f"{self._auftrag()}: noch {restzeit(rt)}, fertig {fertig_um(rt)}")

    async def _hms(self) -> None:
        rt = self.rt
        if not hms_ernst(rt):
            return
        self._timer("hms", 5, self._hms_melden)

    async def _hms_melden(self, _now) -> None:
        await self.melden("⚠️ X1C – HMS-Meldung", hms_text(self.rt))

    def _tuer(self, n: str) -> None:
        if n == "on":
            self._timer("tuer", 60, self._tuer_melden)
        else:
            self._cancel("tuer")

    async def _tuer_melden(self, _now) -> None:
        rt = self.rt
        if rt.state(eid(rt, "binary_sensor", "gehausetur")) == "on" and rt.state(eid(rt, "sensor", "druckstatus")) in RUNNING:
            await self.melden("🚪 X1C – Tür offen", "Die Tür ist seit über einer Minute offen, während gedruckt wird.")

    def _feuchte(self) -> None:
        rt = self.rt
        if (rt.num(eid(rt, "sensor", "ams_1_luftfeuchtigkeit_2"), 0) or 0) > rt.flt("x1c_ams_feuchte_grenze", 40):
            if "feucht" not in rt.mem["timers"]:
                self._timer("feucht", 1800, self._feucht_melden)
        else:
            self._cancel("feucht")

    async def _feucht_melden(self, _now) -> None:
        rt = self.rt
        rt.mem["timers"].pop("feucht", None)
        wert = rt.num(eid(rt, "sensor", "ams_1_luftfeuchtigkeit_2"), 0) or 0
        if wert > rt.flt("x1c_ams_feuchte_grenze", 40):
            await self.melden("💧 X1C – AMS zu feucht", f"Luftfeuchte im AMS: {wert:g} %. Trockenmittel prüfen.")

    async def _filament(self) -> None:
        rt = self.rt
        slots = knappe_slots(rt)
        jetzt = bool(slots)
        if jetzt and not rt.mem.get("knapp"):
            await self.melden("🧵 X1C – Filament wird knapp", ", ".join(slots))
        rt.mem["knapp"] = jetzt

    def _online(self, o: str | None, n: str) -> None:
        if o == "on" and n in ("off", "unavailable"):
            self._timer("offline", 300, self._offline_melden)
        else:
            self._cancel("offline")

    async def _offline_melden(self, _now) -> None:
        rt = self.rt
        if rt.state(eid(rt, "binary_sensor", "online")) == "on":
            return
        fort = int(rt.num(eid(rt, "sensor", "druckfortschritt"), 0) or 0)
        if fort not in (0, 100):
            await self.melden("📡 X1C – offline", "Der Drucker ist seit 5 Minuten nicht erreichbar – ein Druck lief.")

    async def _licht_aus(self, _now) -> None:
        rt = self.rt
        rt.mem["timers"].pop("licht", None)
        if rt.on("x1c_licht_automatik") and rt.state(eid(rt, "sensor", "druckstatus")) not in RUNNING:
            await rt.call("light", "turn_off", {"entity_id": eid(rt, "light", "druckraumbeleuchtung")})
