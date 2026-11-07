# ElectroSmart UPS-POD — automatyczne archiwum potwierdzeń doręczenia

**Adres:** https://ups.xlap.pl (Cloudflare Tunnel `ds1821`, aplikacja Access z polityką `laptopshop.pl`).

Aplikacja webowa zastępuje desktopowy sposób pracy przy zbieraniu potwierdzeń. Jej baza i pliki są trwałe na Synology DS1821+, niezależnie od restartów i aktualizacji kontenerów. Dotychczasowa wersja desktopowa pozostaje w katalogu `UPS_POD/` repozytorium.

## Obsługa

1. Zaloguj się przez Cloudflare Access na https://ups.xlap.pl.
2. Wklej numery UPS do pola tekstowego. Separatory: spacje, wiele spacji, tabulatory, przecinki, średniki, CR/LF i nowe wiersze.
3. Kliknij **Dodaj do kolejki**. Identyczne numery z obecnego i wcześniejszych importów są ignorowane (`tracking` jest kluczem unikatowym SQLite).
4. Worker wewnątrz tego samego kontenera odpytuje UPS Tracking API sekwencyjnie, pobierając POD tylko dla doręczonych przesyłek. Proces odbywa się niezależnie od otwartej przeglądarki.
5. Zielony znacznik oznacza gotowy PDF; czerwony — brak POD lub inny błąd, którego szczegóły można odczytać po najechaniu kursorem. Można filtrować, wyszukiwać i zaznaczać pozycje.
6. Pobierz jeden PDF, wybrane PDF-y w ZIP lub **wszystkie POD** w ZIP (do 1000 dokumentów i 150 MB na żądanie).
7. Nieudane pobrania są ponawiane automatycznie co 3 godziny. **Ponów zaznaczone** pozwala uruchomić próbę wcześniej.

Pliki źródłowe UPS są archiwizowane obok czytelnego PDF. Gdy UPS zwraca PDF, przechowywany jest bez zmian; dla HTML/obrazu powstaje PDF przez Chromium i zachowany zostaje oryginał. Brak POD nigdy nie jest zastępowany sztucznie wygenerowanym „potwierdzeniem”.

## Lokalizacja i trwałość danych

- NAS: `/volume2/docker/ups-pod/`
- Sekrety: `/volume2/docker/ups-pod/.env` — uprawnienia `600`, **nigdy w GitHub**
- Dane: `data/ups_pod.sqlite3` (SQLite + WAL), `data/ups_pod.sqlite3-wal`, `data/ups_pod.sqlite3-shm`
- **Archiwum PDF POD: `/volume1/dokumentacja/UPS/POD/` (bind mount `/pod`)**, nazwy `RRRR-MM-DD-FBA_ID-tracking.pdf`.
- **Brak FBA:** automatycznie `FBA0000000`; po wprowadzeniu ID plik jest przemianowywany fizycznie.
- **Brak wiarygodnej daty:** w nazwie `BRAK_DATY`, bez fabrykowania daty. Gdy UPS zwróci `pickupDate`, plik zmienia nazwę automatycznie.
- Starszy `data/pod/` jest zachowany jako kopia migracyjna (nie jest już katalogiem roboczym).
- Oryginalne pliki otrzymane od UPS: `data/source/<tracking>.<typ>`
- Klucz sesji aplikacji: `data/.web_session_key`
- Stary plik `data/apka.xlsx` jest zachowany, **nie służy jako baza nowego webservice'u**.

Zabezpieczaj kopią/snapshotem **cały katalog `data`**, a nie wyłącznie plik SQLite — baza może mieć aktywny dziennik WAL.

## Konfiguracja UPS

Przykład `.env` (na NAS, tylko dla uprawnionego użytkownika):

```dotenv
client_id=
client_secret=
# UPS_SHIPPER_NUMBER=ABC123
```

Obsługiwane są także nazwy `UPS_CLIENT_ID` i `UPS_CLIENT_SECRET`. Zmiany `.env` wymagają ponownego utworzenia kontenerów. W portalu UPS wystarcza produkt **Tracking API** i OAuth Client Credentials. Konto UPS musi mieć prawo do POD danej przesyłki — sam status „Delivered” nie gwarantuje dokumentu.

`UPS_POD_RETRY_HOURS=3` i `UPS_POD_POLL_SECONDS=10` określają odpowiednio odstęp ponowień i częstotliwość sprawdzania kolejki.

## Technologia i wdrożenie

- Flask + Gunicorn (panel, API), worker Python (sekwencyjny) w tym samym kontenerze, nadzorowany wraz z Gunicorn przez `supervisor.py`, SQLite (unikalny tracking, status i harmonogram), UPS Tracking API + OAuth, Playwright/Chromium (HTML→PDF)
- Dockerfile w korzeniu, Docker Compose `compose.yaml`. Portainer: stack `ups-pod`.
- **Jeden kontener `ups-pod-web`**, zawierający panel i worker; nie publikuje portów i jest dostępny tylko przez istniejący Cloudflare Tunnel `cf_ups`.
- Ten sam kontener należy również do sieci `ups-pod_ups_outbound`, aby łączyć się z UPS API; osobny kontener `ups-pod-worker` jest usuwany przy wdrożeniu (`--remove-orphans`).
- Polityka Access: istniejąca reguła `laptopshop.pl`, bez dodatkowych tokenów service.
- `/healthz`: healthcheck wewnętrzny (z nagłówkiem `Host: ups.xlap.pl`).
- `.dockerignore` wyłącza `.env` i katalog `data/` z kontekstu budowy obrazu.

## Testy

```bash
python -m pip install -r requirements-dev.txt
ruff check web tests_web
python -m playwright install --with-deps chromium
python -m pytest -q tests_web -v
```

`tests_web/test_e2e.py` sprawdza rzeczywiste API UPS, przeglądarkę, świeży POD, PDF i ZIP, aktualizacje AJAX, import trzech wierszy Excela, równoczesną deduplikację, trwałość i paginację audytu, CSRF oraz ponowienia kolejki. Wymaga prawdziwych poświadczeń UPS i numeru doręczonej przesyłki. Workflow: `.github/workflows/web-ci.yml`.

**Test produkcyjny:** token OAuth UPS sprawdzony z rzeczywistym `.env`; faktycznego POD nie pobierano bez wskazanego numeru doręczonej przesyłki.

## Bezpieczeństwo i WDT

Nie ujawniaj sekretów ani odpowiedzi UPS zawierających dane osobowe. Cloudflare Access chroni cały host; API ma dodatkowo ścisłe Host/Origin, CSRF i CSP. Oryginał dokumentu UPS zachowuj razem z fakturą i specyfikacją towarów — aplikacja gromadzi dokumenty przewoźnika, a nie rozstrzyga samodzielnie warunków stawki VAT 0%.

## CI/CD i dziennik operacji

Workflow `web-ci.yml` uruchamia Ruff oraz testy przeglądarkowe z rzeczywistym
produkcyjnym API UPS. Wymaga sekretów GitHub `UPS_ENV` (zawartość konfiguracji UPS)
i `UPS_E2E_TRACKING` (doręczona przesyłka z dostępnym POD). Brak konfiguracji albo
brak rzeczywistego POD powoduje błąd testu. Test korzysta z osobnej tymczasowej
bazy i pobiera nowy dokument, bez produkcyjnego cache i bez symulatora.

Dopiero powodzenie zadania `quality` na `main` uruchamia `deploy`, który wywołuje
istniejący hook `https://deploy.xlap.pl/deploy/elconcept/UPS-POD`.
Sekrety `DEPLOY_CF_CLIENT_ID`, `DEPLOY_CF_CLIENT_SECRET`, `DEPLOY_TOKEN` pozostają
w GitHub Secrets. Hook pobiera repo przez klucz deploy tylko do odczytu,
uruchamia `deploy.sh` i czeka na gotowość kontenerów. Kod na NAS jest w
`/volume2/docker/ups-pod/repo`, a trwała baza, POD i `.env` pozostają w katalogu
nadrzędnym. Obraz zawiera etykietę rewizji Git.

Zakładka Logi odczytuje trwałą tabelę `audit` w tej samej bazie SQLite. Zapisuje
importy, pobrania PDF/ZIP, ręczne ponowienia, start workera, zapytania i odpowiedzi
HTTP OAuth/Tracking, statusy HTTP, wyniki analizy oraz terminy ponownych prób.
Nie zapisuje nagłówków uwierzytelnienia, tokenów ani pełnych odpowiedzi UPS.
Błędy odpowiedzi są ograniczone do kodów maszynowych. Widok korzysta z `textContent`.
Przesyłki i bieżące logi odświeżają się co 7 sekund przez AJAX. Starsze logi mają
paginację. Parser wyodrębnia formaty UPS 1Z (18 znaków), T (11 znaków), 9 i 12 cyfr,
ignoruje pozostały tekst i usuwa duplikaty. Klucz główny SQLite wymusza unikalność
również przy równoczesnych importach.

## Zmiana architektury z 7.11.2026

Usługa Docker Compose składa się teraz z jednego kontenera (dwa nadzorowane procesy: Gunicorn + worker). Proces nadzorczy propaguje SIGTERM, a gdy któryś proces się zakończy, kontener uruchamia się ponownie zgodnie z polityką Docker. `healthz` sprawdza także możliwość zapisu w montowanym katalogu PDF. Worker używa `locale=pl_PL` we wszystkich zapytaniach Tracking API.

Wolumen NAS `/volume1/dokumentacja/UPS/POD:/pod` musi istnieć; Compose nie może go tworzyć po cichu. Utworzono na Synology wyłączone do logowania konto serwisowe `upspodsvc` o UID 1031, grupa systemowa `users` GID 100; kontener działa jako `1031:100`, a właściciel katalogu POD i plików SQLite jest z tym zgodny. Przy migracji poprzednich dokumentów zachowywane są źródłowe kopie w `data/pod` i weryfikowana integralność SHA-256 po kopiowaniu. **Jedynym aktywnym archiwum** pozostaje `/pod`. Po podaniu FBA zmieniana jest nazwa istniejącego PDF, a nie kopiowany drugi dokument.

## Import mieszany i automatyczne przypisywanie FBA

Dwa pola importowe obsługują uporządkowany strumień dowolnych tokenów: `FBA1234567`, tracking `1Z` + 16 znaków, dowolne separatory i wielokrotne trackingi w jednej komórce. Każdy FBA jest przypisany do kolejnych trackingów aż do następnego FBA. Obsługiwane są także istniejące w bazie identyfikatory 12-znakowe (np. `FBA15MSX7FKL`). Śmieci wejściowe są ignorowane, ale raportowane inline. Ponowny import istniejącego UPS bez FBA uzupełnia jego przypisanie. Sprzeczne przypisania są blokowane transakcyjnie. Dla niewysłanych jeszcze przesyłek status `W drodze` to oczekiwanie, nie błąd POD. Każda próba ma przypisany numer UPS w audycie; starsze wpisy `retry` bez logu otrzymują jeden odtworzony wpis przy migracji SQLite. UPS `pickupDate` ma pierwszeństwo nad ręcznymi datami.

Status przesyłki jest kolorową etykietą. W tabeli nie ma kolumn prób i ostatniego sprawdzenia; metadane są dostępne w kolumnie `Dokument` przy braku POD. Shift-kliknięcie checkboxa zaznacza zakres do najbliższego wybranego wiersza powyżej; zaznaczonym numerom można nadać zbiorowo FBA.

## Panel miesięczny i dodatkowe dane UPS (07.11.2026)

Główny widok domyślnie filtruje bieżący miesiąc wg zwróconej przez UPS daty `shipment.pickupDate`; dla numerów oczekujących na wynik UPS używana jest data dodania do kolejki. Menu „Miesiąc” udostępnia wcześniejsze miesiące i „Wszystkie miesiące”. Wiersze sortowane są malejąco po dacie nadania. Import UPS i Amazon FBA odbywa się w jednym wielowierszowym polu.

Kolumny z rzeczywistych danych API: `package.weight.weight`, `package.weight.unitOfMeasurement`, `package.deliveryTime.type/startTime/endTime`, `package.deliveryDate[].type/date`, `package.deliveryInformation.receivedBy`, `package.deliveryInformation.deliveryPhoto.photo`. Typ `DEL` oznacza doręczenie, `EDW` przewidywane okno, `SDD` planowaną datę, nie należy ich utożsamiać. Miniaturka jest udostępniana pod prywatnym endpointem `/api/shipments/<tracking>/photo` tylko w przypadku gdy UPS dostarczy prawidłowy obraz. `photoCaptureInd=N` nie jest obrazem i skutkuje brakiem miniaturki. Obrazy przechowywane są w `/data/delivery_photos` i nie opuszczają chronionego dostępu do aplikacji.

Pole „Dokument” ma skrótowy wskaźnik; szczegóły prób i termin kolejnej są w tooltipie. „W drodze” jest żółtym stanem oczekiwania, czerwony oznacza błąd.
