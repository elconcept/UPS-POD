"""Selection-scoped UPS tracking report, xlsxwriter (no macro, no formulas from user text)."""
from __future__ import annotations

import collections
import datetime as dt
import io
from zoneinfo import ZoneInfo

import xlsxwriter

import service


COLUMNS = [
    ("Shipment ID", "shipment_id", 22),
    ("Numer UPS", "tracking", 26),
    ("Nadanie", "ship_date", 16),
    ("Doręczenie", "delivery_date", 18),
    ("W transporcie", "transit_days", 16),
    ("Typ daty", "delivery_date_type", 12),
    ("Waga", "weight", 14),
    ("Status UPS", "ups_status_pl", 31),
    ("Opis statusu", "ups_detail_pl", 45),
    ("API", "state", 16),
    ("POD", "document", 15),
    ("Śledzenie UPS", "link", 19),
]
STATE_LABELS = {
    "ready": "POD pobrany",
    "queued": "Oczekuje",
    "processing": "Przetwarzanie",
    "retry": "Oczekuje / ponowienie",
}


def _date(raw: str) -> dt.date | None:
    if not raw:
        return None
    try:
        value = raw[:4] + "-" + raw[4:6] + "-" + raw[6:8] if len(raw) == 8 else raw
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _rows(trackings: list[str]) -> list[dict]:
    if not isinstance(trackings, list) or not 1 <= len(trackings) <= 5000:
        raise ValueError("Zaznacz od 1 do 5000 przesyłek do zestawienia.")
    if any(not isinstance(t, str) or not service.NUMBER.fullmatch(t) for t in trackings):
        raise ValueError("Nieprawidłowe numery przesyłek.")
    trackings = list(dict.fromkeys(trackings))
    found = {}
    duration_sql = service.transit_days_expression()
    with service.connection() as db:
        for start in range(0, len(trackings), 400):
            batch = trackings[start:start + 400]
            q = ",".join("?" for _ in batch)
            for row in db.execute(
                f"""SELECT tracking,shipment_id,ship_date,delivery_date,
                      {duration_sql} AS transit_days,
                      delivery_date_type,weight_value,weight_unit,ups_status,
                      ups_status_code,ups_status_detail,state FROM shipments
                      WHERE tracking IN ({q})""", batch,
            ):
                record = dict(row)
                import ups_localization
                record["ups_status_pl"], record["ups_detail_pl"] = (
                    ups_localization.presentation(record))
                found[record["tracking"]] = record
    unknown = [number for number in trackings if number not in found]
    if unknown:
        raise ValueError("W zestawieniu występują numery nieobecne w bazie.")
    return [found[number] for number in trackings]


def build_report(trackings: list[str]) -> tuple[io.BytesIO, int]:
    records = _rows(trackings)
    data = io.BytesIO()
    book = xlsxwriter.Workbook(data, {
        "in_memory": True, "strings_to_formulas": False,
        "strings_to_urls": False, "strings_to_numbers": False,
    })
    book.set_properties({"title": "Zestawienie przesyłek UPS-POD",
                         "subject": "Wybrane przesyłki i statusy UPS",
                         "comments": "Dane z UPS Tracking API, nie gwarantują fizycznego doręczenia."})
    title = book.add_format({"bold": True, "font_size": 18, "font_color": "#203A55"})
    subtitle = book.add_format({"font_size": 10, "font_color": "#52677B"})
    header = book.add_format({"bold": True, "font_color": "#FFFFFF",
                              "bg_color": "#235985", "border": 0,
                              "valign": "vcenter"})
    text = book.add_format({"font_size": 10, "valign": "vcenter"})
    dateformat = book.add_format({"font_size": 10, "num_format": "yyyy-mm-dd"})
    durationformat = book.add_format({"font_size": 10, "num_format": '0 "dni"'})
    urlformat = book.add_format({"font_size": 10, "font_color": "#17649C",
                                 "underline": True})
    green = book.add_format({"font_color": "#14774F", "bg_color": "#E7F5ED"})
    amber = book.add_format({"font_color": "#916016", "bg_color": "#FFF1D2"})
    stathead = book.add_format({"bold": True, "font_color": "#FFFFFF",
                                "bg_color": "#235985"})
    bold = book.add_format({"bold": True, "font_size": 11})

    ws = book.add_worksheet("Przesyłki")
    ws.hide_gridlines(2)
    ws.merge_range(0, 0, 0, len(COLUMNS)-1, "UPS-POD — zestawienie przesyłek", title)
    timestamp = dt.datetime.now(ZoneInfo("Europe/Warsaw")).strftime("%Y-%m-%d %H:%M")
    ws.merge_range(1, 0, 1, len(COLUMNS)-1,
                   f"Utworzono {timestamp} · {len(records)} wybranych przesyłek", subtitle)
    for j, (label, _, width) in enumerate(COLUMNS):
        ws.set_column(j, j, width)
        ws.write(3, j, label, header)
    ws.set_row(3, 29)
    ws.freeze_panes(4, 2)
    ws.autofilter(3, 0, 3+len(records), len(COLUMNS)-1)

    statuses = collections.Counter()
    for i, item in enumerate(records, 4):
        status = item["ups_status_pl"]
        state = item["state"]
        statuses[status] += 1
        weight = (item["weight_value"] or "").strip()
        unit = "kg" if item["weight_unit"] == "KGS" else "lb" if item["weight_unit"] == "LBS" else item["weight_unit"]
        url = (f"https://www.ups.com/track?tracknum={item['tracking']}"
               "&loc=pl_PL&requester=ST/trackdetails")
        values = [
            item["shipment_id"] or "Nieprzypisany", item["tracking"],
            _date(item["ship_date"]), _date(item["delivery_date"]),
            item["transit_days"],
            item["delivery_date_type"] or "—",
            (weight + " " + unit).strip() or "—",
            status, item["ups_detail_pl"], STATE_LABELS.get(state, state),
            "PDF dostępny" if state == "ready" else "Brak POD",
            "Śledź w UPS",
        ]
        for j, val in enumerate(values):
            field = COLUMNS[j][1]
            if field == "link":
                ws.write_url(i, j, url, urlformat, "Śledź w UPS")
            elif field == "transit_days":
                if isinstance(val, int):
                    ws.write_number(i, j, val, durationformat)
                else:
                    ws.write_string(i, j, "—", text)
            elif isinstance(val, dt.date):
                ws.write_datetime(i, j, val, dateformat)
            elif field == "state":
                ws.write_string(i, j, str(val), green if state == "ready" else amber)
            else:
                ws.write_string(i, j, str(val), text)
        if i % 2 == 0:
            ws.set_row(i, 20)
    ws.set_landscape()
    ws.fit_to_pages(1, 0)
    ws.repeat_rows(3)

    summary = book.add_worksheet("Statystyki")
    summary.hide_gridlines(2)
    summary.set_column("A:A", 37)
    summary.set_column("B:B", 20)
    summary.merge_range("A1:E1", "Statystyki zaznaczonych przesyłek", title)
    summary.write("A3", "Łącznie zaznaczonych", bold)
    summary.write_number("B3", len(records))
    summary.write("A4", "POD pobrane", bold)
    summary.write_number("B4", sum(r["state"] == "ready" for r in records), green)
    summary.write("A5", "Bez POD", bold)
    summary.write_number("B5", sum(r["state"] != "ready" for r in records), amber)
    summary.write("A7", "Status UPS", stathead)
    summary.write("B7", "Liczba", stathead)
    for index, (status, count) in enumerate(statuses.most_common(), start=7):
        summary.write_string(index, 0, status, text)
        summary.write_number(index, 1, count)
    summary.freeze_panes(7, 0)
    book.close()
    data.seek(0)
    return data, len(records)
