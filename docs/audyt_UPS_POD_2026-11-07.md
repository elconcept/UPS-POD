# Audyt UPS-POD — 7 listopada 2026

Przedmiot: elconcept/UPS-POD, Flask/Gunicorn, SQLite, worker UPS, web UI vanilla JS, Synology Docker/Cloudflare Access.

## Metody

- Przegląd kodu: web/app.py, web/service.py, web/worker.py, web/supervisor.py, web/static/app.js, web/static/style.css, web/templates/index.html, UPS_POD/ups_pod.py, Dockerfile, compose.yaml, deploy.sh oraz GitHub Actions.
- Testy: Ruff, pytest, Playwright/Chromium; odczyt i filtrowanie całej bazy, sortowanie, drag-and-drop, cookie, FBA, workflow kontaktu UPS.
- Renderowanie odizolowanej aplikacji na danych syntetycznych: 1440x900 oraz 390x844.
- Alerty GitHub Dependabot: pięć otwartych powiadomień na trzy zależności (pytest, requests, Flask); wersje podniesione w tej zmianie, ponowna kontrola po wdrożeniu.

## Zrealizowano

- Dowolna kolejność ośmiu kolumn przez uchwyty DnD i Alt+strzałki, zapis cookie.
- Sortowanie po kliknięciu i 8 filtrów kolumnowych. Backend stosuje whitelistę wyrażeń SQL, parametryzuje wartości i filtruje całą bazę, nie tylko bieżącą stronę. Cookies zapisują preferencje.
- Kompaktowy header, KPI, taby, mniejszy odstęp przed tabelą, domyślnie zwinięty importer, responsywny pasek działań.
- Edycja FBA w dostępnym formularzu z walidacją i jawnie zaznaczanym zastąpieniem przypisania; bez systemowych prompt/confirm.
- Oznaczenie planowanej daty SDD jako Plan, a nie potwierdzonej daty DEL.
- Po SDD w przeszłości opcja przygotowania zapytania do UPS dopiero po zaznaczeniu potwierdzenia fizycznego odbioru i podaniu źródła. Brak automatycznej wysyłki lub lokalnej zmiany statusu.
- Usunięto niewykorzystywane ścieżki NAS i dane odbiorcy z odpowiedzi HTTP listy.
- Zależności z aktualnymi poprawkami bezpieczeństwa: Flask 3.1.3, requests 2.33.0 i pytest 9.0.3.

## a) Zmienić — priorytety

P1: web/service.py:init_db uruchamia synchronizację i hashowanie gotowych PDF podczas startu aplikacji; wydzielić migracje poza start web.
P1: web/worker.py:main wykonuje backfill do 100 zapytań API po uruchomieniu; przenieść do osobnej, limitowanej kolejki.
P1: web/static/app.js zastępuje całą treść tabeli podczas odświeżania co 7 sekund. Może resetować fokus. Dodać aktualizację zmian tylko w wierszach.
P2: Statusy UPS i kolejki są tłumaczone w SQL i frontendzie; stworzyć wspólną taksonomię stanów.
P2: Logi operacyjne przetłumaczyć na zrozumiałą historię przesyłki oraz kody diagnostyczne.

## b) Dodać

P1: Rejestr spraw UPS (numer sprawy, data wysłania, kanał, źródło potwierdzenia, dokumenty, odpowiedź, status rozwiązania).
P1: Alert „przekroczona data SDD bez formalnego potwierdzenia”, oddzielny od „doręczono”; nie potwierdzać automatycznie fizycznego odbioru.
P1: Wybrane N rekordów, w tym M poza bieżącym filtrem, i przycisk usunięcia zaznaczenia; obecnie wybrane numery utrzymują się po zmianie strony.
P2: Eksport CSV/XLSX z historią i raport SLA dla kierownictwa bez potrzeby Amazon API.
P2: Audyt zmian Shipment ID i przypisanie działań do konkretnego operatora.
P3: Mobilny edytor kolejności kolumn dotykiem (weryfikacja Safari i Android); alternatywa klawiaturowa działa już teraz.

## c) Poprawić

P0/P1: Upstream Cloudflare Access ogranicza wejście do serwisu, ale origin w web/app.py weryfikuje głównie nagłówek Host i token CSRF, nie podpisane JWT użytkownika. Kontenery podpięte do tej samej sieci mogą potencjalnie ominąć Access. Ograniczyć sieć i dodać weryfikację tożsamości w origin po sprawdzeniu konfiguracji tunelu.
P1: Usunąć wewnętrzne ścieżki NAS z list API — poprawiono; nadal warto przeglądać zakres wszystkich endpointów.
P1: Nierozwiązane podatności Dependabot — trzy zależności podniesione; wymaga potwierdzenia zamknięcia alertów i skanu bazowego obrazu.
P1: web/app.py:zip_download przygotowuje ZIP w pamięci RAM do limitu 150 MB. Zaplanować strumieniowanie i limit jednoczesnych pobrań.
P1: web/worker.py:mark_retry stosuje sztywne 3 godziny. Dodać backoff z jitterem i obsługę HTTP 429/Retry-After.
P2: Zrobić spójny backup SQLite WAL + archiwum POD i regularny test odtwarzania; snapshot sam w sobie nie jest testem restore.
P2: Dziennik audytu nie ma retencji i przechowuje ogólny aktor user; dodać politykę archiwizacji i identyfikowanie upoważnionych użytkowników.
P2: Poprawić czytelność błędów worker bez ujawniania danych osobowych i sekretów.

## d) Ulepszyć

- Mierzyć czas zapytań i dobierać indeksy SQLite po zwiększeniu bazy (filtry z INSTR i LOWER wymagają skanowania).
- Odświeżać tylko istotne zmiany zamiast przebudowywać 200 wierszy; wstrzymywać polling przy braku aktywnej zakładki.
- Zapisywać i wyświetlać bezpieczne szczegóły zdarzeń z UPS package.activity, żeby rozróżnić wyjątki.
- Utrzymać wymagany test z rzeczywistym UPS API, ale oddzielić deterministyczne testy UI od niezawodności zewnętrznego API.
- Sprawdzić kontrast/fokus/obsługę dotykiem na rzeczywistych urządzeniach, nie tylko w emulatorze Chromium.
- Potwierdzić formalne prawo do oznaczania CONCEPT symbolem ®.

## Doręczenie bez skanu UPS

Według oficjalnej dokumentacji UPS status Delivered oznacza rejestrowane doręczenie z timestampem; SDD jest prognozą, nie dowodem dostarczenia. Oficjalny kanał dla Polski to formularz: https://www.ups.com/upsemail/input?loc=pl_PL. Publicznie nie ustalono autoryzowanego API pozwalającego zewnętrznej aplikacji dowolnie zmieniać status UPS. Po uzyskaniu niezależnego potwierdzenia odbioru operator przygotowuje szkic zapytania i przekazuje go UPS do sprawdzenia/korekty. Wdrożenie wysyłki automatycznej wymaga zweryfikowanego kanału i upoważnienia, deduplikacji, limitów i jawnej kontroli operatora. Bez tego system nie wysyła wiadomości.

Źródła:
https://www.ups.com/pl/en/support/tracking-support/where-is-my-package/understanding-tracking-status
https://www.ups.com/pl/pl/support/contact-us
https://www.ups.com/upsemail/input?loc=pl_PL
