# UPS-POD: polskie statusy, wskaźniki API i statystyki (2026-11-07)

## Wyświetlanie statusów UPS

W bazie SQLite zachowujemy bez zmian surowe pola currentStatus.description,
currentStatus.simplifiedTextDescription, typ zdarzenia oraz kody API.
Polskie etykiety i podpowiedzi pochodzą z web/ups_localization.py i są
obliczane na potrzeby interfejsu. Nie należy utożsamiać typu X z jednym
tekstem „Wyjątek” ani typu I z brakiem opóźnienia.

Przykład z danych UPS przechowywanych 7 listopada 2026 r.:

- 1Z004R316837162143: On the Way: Delayed; typ X, status 048,
  „Delivery will be delayed by one business day.” → W drodze — opóźniona,
  „Doręczenie opóźni się o jeden dzień roboczy.”
- 1Z004R316839216224: Delayed; typ I, status 090,
  „We’re sorry, delivery is taking longer than expected. Your package
  has been temporarily delayed.” → Opóźniona,
  „Doręczenie trwa dłużej niż oczekiwano. Przesyłka jest tymczasowo opóźniona.”

Kody D/X/I, 048/090 i MF/AR są danymi diagnostycznymi przechowywanymi w
bazie i opcjonalnych polach API, ale nie w zwykłej podpowiedzi statusu.
Nieznane przyszłe sformułowania UPS otrzymują ostrożną ogólną etykietę po
polsku. Nie zgadujemy szczegółów niewystępujących w źródle.

## Wskaźnik „API”

- Zielony znacznik wyboru: POD pobrany i zapisany.
- Niebieski zegar: oczekuje na kolejne sprawdzenie UPS.
- Niebieskie strzałki: trwa połączenie / pobieranie danych UPS.
- Pomarańczowy trójkąt: problem doręczenia, zmieniony termin lub
  przeterminowana planowana dostawa.
- Czerwony symbol błędu: nieudana operacja techniczna.

Ikony mają polskie etykiety aria-label i title. W kolumnie Dokument
pozycje oczekujące wyświetlają małą ikonę dokumentu zamiast słowa
„Oczekuje”. Kliknięcie nadal otwiera historię operacji. Checkboxy
tworzą osobną, zawsze pierwszą kolumnę; nie podlegają zmianie kolejności.

## KPI i definicje

- Wszystkie przesyłki: pełna liczba rekordów.
- POD pobrane: state = ready.
- Wszystkie bez POD: state różny od ready (łącznie z oczekiwaniem,
  harmonogramem retry i przetwarzaniem).
- Bez POD w bieżącym roku: powyższe, ale tylko nadane w roku
  kalendarzowym Europe/Warsaw.
- Opóźnione: niedostarczone z bieżącym tekstem UPS zawierającym delayed
  lub opóźn (w tym On the Way: Delayed).
- W drodze / przełożone: niedostarczone z neutralnym On the Way oraz
  zmienionym terminem Rescheduled/Reschedule Requested; grupa
  rozłączna z opóźnionymi.
- Pasek ostatnich sześciu miesięcy: liczba przesyłek bez POD
  przypisana do miesiąca ship_date (data odbioru/nadania z UPS), a w
  przypadku braku tej daty — do miesiąca pierwszego importu. Są to
  stany bieżące według kohorty miesiąca, nie historyczne snapshoty.

Wszystkie liczniki odświeżają się wraz z listą co siedem sekund.
Zmiany sprawdzono testami SQLite i Playwright w obu motywach.
