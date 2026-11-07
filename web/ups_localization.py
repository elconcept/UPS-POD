"""Human-readable Polish UPS tracking terminology.

Keep raw UPS API strings unchanged in SQLite. These localized display values
derive from the response and explicitly preserve distinctions between statuses
such as On the Way: Delayed (exception) and Delayed (in transit).
"""
from __future__ import annotations

import re


def _norm(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("’", "'")).strip().casefold()


_STATUS = {
    "Delivered": "Doręczono",
    "Doręczono": "Doręczono",
    "On the Way": "W drodze",
    "On the Way: Delayed": "W drodze — opóźniona",
    "Delayed": "Opóźniona",
    "Delivery Rescheduled": "Zmieniono termin doręczenia",
    "Reschedule Delivery Requested": "Zlecono zmianę terminu doręczenia",
    "Out for Delivery": "W doręczeniu",
    "Label Created": "Utworzono etykietę",
    "Shipment Ready for UPS": "Oczekuje na odbiór przez UPS",
    "Picked Up": "Odebrano przez UPS",
    "Exception": "Wyjątek w doręczeniu",
    "Wyjątek w doręczeniu": "Wyjątek w doręczeniu",
}
_DETAILS = {
    "Departed from Facility": "Przesyłka opuściła sortownię UPS.",
    "Arrived at Facility": "Przesyłka dotarła do sortowni UPS.",
    "Your package has arrived at a local UPS location and is still being processed for delivery.":
        "Przesyłka jest w lokalnym oddziale UPS i trwa przygotowanie do doręczenia.",
    "Processing at UPS Facility": "Przesyłka jest przetwarzana w sortowni UPS.",
    "Delivery will be delayed by one business day.":
        "Doręczenie opóźni się o jeden dzień roboczy.",
    "We're sorry, delivery is taking longer than expected. Your package has been temporarily delayed.":
        "Doręczenie trwa dłużej niż oczekiwano. Przesyłka jest tymczasowo opóźniona.",
    "This package is being held for a future delivery date.":
        "Przesyłka jest przechowywana do późniejszego terminu doręczenia.",
    "The package is being held for a future delivery date. We'll attempt to deliver on the date requested.":
        "Przesyłka oczekuje na nowy termin. UPS podejmie próbę doręczenia w wybranym dniu.",
    "Your delivery will be rescheduled.":
        "UPS zmieni planowany termin doręczenia.",
    "Uncontrollable events have delayed delivery. We are adjusting delivery plans as quickly as possible. Please check back on the next business day for updates.":
        "Zdarzenia niezależne od UPS spowodowały opóźnienie. Przewoźnik koryguje plan doręczenia; aktualizacja powinna pojawić się w następnym dniu roboczym.",
    "Delivery is scheduled to be rescheduled.":
        "UPS planuje zmianę terminu doręczenia.",
    "Package is out for delivery.": "Przesyłka została przekazana kurierowi do doręczenia.",
    "Your package was delivered.": "Według UPS przesyłka została doręczona.",
}
STATUS_LABELS = {_norm(key): value for key, value in _STATUS.items()}
DETAIL_LABELS = {_norm(key): value for key, value in _DETAILS.items()}


def status_pl(value: str, type_code: str = "") -> str:
    key = _norm(value)
    if key in STATUS_LABELS:
        return STATUS_LABELS[key]
    if not key:
        return "Bez statusu"
    if "delay" in key or "opóźn" in key:
        return "W drodze — opóźniona" if "on the way" in key else "Opóźniona"
    if "reschedul" in key or "przełożon" in key:
        return "Zmieniono termin doręczenia"
    if "on the way" in key or "transit" in key or "w drodze" in key:
        return "W drodze"
    if "deliver" in key or "doręcz" in key:
        return "Doręczono" if type_code.upper() == "D" else "Aktualizacja doręczenia"
    # For previously unseen UPS texts do not invent a detailed Polish
    # translation. Return a type-level description and retain raw source in DB.
    return {"D": "Doręczono", "I": "W drodze", "X": "Zmiana w doręczeniu",
            "M": "Utworzono etykietę", "MV": "Anulowano etykietę",
            "U": "Aktualizacja terminu"}.get(type_code.upper(), "Status UPS — sprawdź szczegóły")


def detail_pl(value: str, status: str, type_code: str = "") -> str:
    key = _norm(value)
    if key in DETAIL_LABELS:
        return DETAIL_LABELS[key]
    if not key:
        return {
            "Doręczono": "UPS zarejestrował doręczenie przesyłki.",
            "W drodze": "Przesyłka jest transportowana w sieci UPS.",
            "Opóźniona": "UPS poinformował o opóźnieniu doręczenia.",
            "W drodze — opóźniona": "Przesyłka jest w transporcie, ale doręczenie się opóźni.",
        }.get(status, "Szczegóły zdarzenia można sprawdzić na stronie UPS.")
    if "arrived" in key and "facility" in key:
        return "Przesyłka dotarła do oddziału UPS."
    if "departed" in key:
        return "Przesyłka opuściła oddział UPS."
    if "delay" in key:
        return "UPS odnotował opóźnienie doręczenia. Aktualny termin można sprawdzić w śledzeniu."
    if "reschedul" in key or "held for a future" in key:
        return "UPS zmienił lub przygotowuje zmianę terminu doręczenia."
    if "deliver" in key and type_code.upper() == "D":
        return "Według UPS przesyłka została doręczona."
    return "Szczegółowe informacje o tym zdarzeniu są dostępne w śledzeniu UPS."


def presentation(row: dict) -> tuple[str, str]:
    label = status_pl(row.get("ups_status", ""), row.get("ups_status_code", ""))
    description = detail_pl(row.get("ups_status_detail", ""), label,
                            row.get("ups_status_code", ""))
    # Legacy rows may have a meaningful Polish description without a modern
    # simplifiedTextDescription. Retain that evidence instead of overwriting
    # it with a generic fallback; English descriptions are localized above.
    old_detail = str(row.get("description") or "").strip()
    if not row.get("ups_status_detail") and old_detail != row.get("ups_status"):
        if len(old_detail) <= 180 and re.search(
            r"(?i)nieprawidłow|przesyłk|doręcz|opóźn|odbiorc|termin",
            old_detail,
        ):
            description = old_detail
    return label, description
