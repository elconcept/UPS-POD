# UPS POD Web — automatyczne archiwum potwierdzeń doręczenia (WDT)

**Adres:** https://ups.xlap.pl (Cloudflare Tunnel `ds1821`, aplikacja Access z polityką `laptopshop.pl`).

Aplikacja webowa zastępuje desktopowy sposób pracy przy zbieraniu potwierdzeń. Jej baza i pliki są trwałe na Synology DS1821+, niezależnie od restartów i aktualizacji kontenerów. Dotychczasowa wersja desktopowa pozostaje w katalogu `UPS_POD/` repozytorium.

## Obsługa

1. Zaloguj się przez Cloudflare Access na https://ups.xlap.pl.
2. Wklej numery UPS do pola tekstowego. Separatory: spacje, wiele spacji, tabulatory, przecinki, średniki, CR/LF i nowe wiersze.
3. Kliknij **Dodaj do kolejki**. Identyczne numery z obecnego i wcześniejszych importów są ignorowane (`tracking` jest kluczem unikatowym SQLite).
4. Osobny worker odpytuje UPS Tracking API sekwencyjnie, pobierając POD tylko dla doręczonych przesyłek. Proces odbywa się niezależnie od otwartej przeglądarki.
5. Zielony znacznik oznacza gotowy PDF; czerwony — brak POD lub inny błąd, którego szczegóły można odczytać po najechaniu kursorem. Można filtrować, wyszukiwać i zaznaczać pozycje.
6. Pobierz jeden PDF, wybrane PDF-y w ZIP lub **wszystkie POD** w ZIP (do 1000 dokumentów i 150 MB na żądanie).
7. Nieudane pobrania są ponawiane automatycznie co 3 godziny. **Ponów zaznaczone** pozwala uruchomić próbę wcześniej.

Pliki źródłowe UPS są archiwizowane obok czytelnego PDF. Gdy UPS zwraca PDF, przechowywany jest bez zmian; dla HTML/obrazu powstaje PDF przez Chromium i zachowany zostaje oryginał. Brak POD nigdy nie jest zastępowany sztucznie wygenerowanym „potwierdzeniem”.

## Lokalizacja i trwałość danych

- NAS: `/volume2/docker/ups-pod/`
- Sekrety: `/volume2/docker/ups-pod/.env` — uprawnienia `600`, **nigdy w GitHub**
- Dane: `data/ups_pod.sqlite3` (SQLite + WAL), `data/ups_pod.sqlite3-wal`, `data/ups_pod.sqlite3-shm`
- Archiwum PDF: `data/pod/<tracking>.pdf`
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

- Flask + Gunicorn (panel, API), osobny worker Python (sekwencyjny), SQLite (unikalny tracking, status i harmonogram), UPS Tracking API + OAuth, Playwright/Chromium (HTML→PDF)
- Dockerfile w korzeniu, Docker Compose `compose.yaml`. Portainer: stack `ups-pod`.
- Kontener `ups-pod-web` nie publikuje portów; jest dostępny tylko przez dedykowaną sieć Docker `cf_ups` z `cloudflared`.
- Kontener `ups-pod-worker` jest odseparowany w sieci `ups-pod_ups_outbound`, wyłącznie do połączeń wychodzących.
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

`tests_web/test_e2e.py` sprawdza cały przepływ: HTTP i CSRF, parser separatorów, idempotentność, trwałość SQLite, OAuth na symulatorze UPS, POD PDF/HTML, konwersję Chromium, doręczone bez POD, niedoręczone, 404, ponowienia, pobieranie PDF i ZIP. Testy nie używają rzeczywistych poświadczeń. Workflow: `.github/workflows/web-ci.yml`.

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
