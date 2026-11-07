# UPS-POD: Gmail, Tracking API, XLSX oraz import Google Sheets (2026-11-07)

## Gmail bez projektu GCP

Domyślne nadawanie wiadomości odbywa się przez Gmail SMTP po TLS, smtp.gmail.com:465, za pomocą wbudowanego modułu Python smtplib. Formularz „Konfiguracja → Gmail” przyjmuje adres skrzynki oraz osobne, 16-znakowe hasło aplikacji. Nie wymaga Google OAuth Client ID/Secret ani projektu Google Cloud.

Przycisk „Sprawdź logowanie” wykonuje wyłącznie uwierzytelnienie SMTP, bez wysyłki wiadomości. Faktyczna wysyłka do UPS następuje wyłącznie po ręcznym kliknięciu „Zgłoś UPS” przy kwalifikującej się przesyłce. Mechanizm jednokrotnej wysyłki i ochrony przed duplikatami pozostaje.

Hasła aplikacji wymagają odpowiednio skonfigurowanego konta z 2FA i mogą być wyłączone przez administratora Workspace. NIE należy wpisywać zwykłego hasła Google. Dla kont bez haseł aplikacji pozostaje zwinięty wariant OAuth 2.0 wymagający zarejestrowanej aplikacji. Dane przechowywane są w /volume2/docker/ups-pod/data/.env z uprawnieniami 0600 i nie są zwracane w odpowiedziach API.

## Wszystkie dane UPS Tracking API

Nowa tabela SQLite: ups_tracking_payloads: tracking, response_json_zlib, response_sha256, raw_bytes, captured_at, http_status. Wartość BLOB przechowuje pełny JSON odpowiedzi UPS, wszystkie zagnieżdżenia/klucze/wartości i tablice, bez odrzucania pól (kompresja bezstratna zlib, kontrola SHA-256). Dane potencjalnie osobowe, takie jak adresy, podpisy czy POD, nie są przekazywane do zwykłego API dashboardu. W UI pozostają wyłącznie 16 pól jawnie dopuszczonych do wyświetlenia w dodatkowych kolumnach.

Druga tabela SQLite: ups_backfill śledzi queued/done/error, próbę, kod HTTP i datę ponowienia. Przy starcie worker rejestruje idempotentnie wszystkie rekordy bez pełnego JSON, a następnie pobiera odpowiedzi pojedynczo z rzeczywistego UPS API, po obsłużeniu kolejki bieżących POD. Zakończone pozycje są odhaczane; błędy do maksymalnie 3 prób z odstępami 3 godzin. Rekordy ready zachowują oryginalne PDF-y, FBA i stan doręczenia — aktualizowane są tylko metadane. Postęp: zakładka „Konfiguracja”, sekcja UPS. Nie wszystkie pola są zwracane przez UPS dla każdej przesyłki, więc pewne komórki mogą być prawidłowo puste.

## Tabela, raport i podgląd POD

Tabela z dynamiczną szerokością przez colgroup i table-layout: fixed zajmuje 100% strony przy każdej liczbie włączonych kolumn; poziome przewijanie jest wyłączone. Przy 24 polach na małym ekranie wartości wizualnie się skracają, ale pozostają w bazie. Resize działa przez requestAnimationFrame, w czasie przeciągania aktualizowane są tylko definicje kolumn — nie tysiące osobnych komórek. Filtry kolumn startują zwinięte i zachowują wybór w cookie. Linki UPS używają języka polskiego (loc=pl_PL).

Eksport „Zestawienie XLSX” używa wszystkich zaznaczonych checkboxami rekordów (do 5000, także bez POD). Otrzymuje się skoroszyt z arkuszami „Przesyłki” i „Statystyki”: FBA, UPS, daty, statusy, flagi POD, liczebność każdego statusu i klikalne linki UPS. Odczyt wykonywany jest z SQLite, niezależnie od aktualnie widocznej strony. Zawartość użytkownika nie jest wykonywana jako formuły Excela.

Miniatury pierwszej strony POD są renderowane przez PyMuPDF lokalnie w serwisie, zapisane w osobnym cache PNG, niezależnym od archiwum oryginalnych PDF-ów. Pojawiają się przy najechaniu/fokusie na „↓ PDF”; chroniony endpoint zwraca tylko miniaturkę.

## Propozycja importu Google Sheets — do realizacji w osobnym etapie

**Rekomendacja przy prywatnych arkuszach:** oficjalne Google Sheets API v4 + biblioteka Python gspread z uprawnieniem spreadsheets.readonly, autoryzacja przez konto serwisowe Workspace lub OAuth operatora. API wymaga skonfigurowanych danych dostępowych Google — w przeciwieństwie do SMTP Gmail nie da się go używać wiarygodnie przez „zalogowanie bez projektu”.

**Wariant bez API / bez GCP:** skopiowanie wielokolumnowych wierszy i wklejenie jako TSV, albo import plików XLSX/ODS/CSV przez python-calamine oraz standardowy csv; prostszy dla proof of concept.

Proponowany algorytm wierszowy: przeczytać każdą komórkę jako tekst bez wykonywania formuł; w każdej znaleźć wszystkie wystąpienia FBA[A-Z0-9]+ i UPS 1Z[A-Z0-9]{16} także w komórkach zawierających wiele numerów oddzielonych dowolnym znakiem niealfanumerycznym, nową linią lub spacją; normalizować i deduplikować; dla dokładnie jednego FBA w wierszu przypisać wszystkie UPS z wiersza; wiele FBA, konflikt lub brak UPS oznaczyć do ręcznej decyzji; pokazać dry-run z podglądem, numerem wiersza, duplikatami i konfliktem przed atomowym zapisem. Przy dużych arkuszach pobierać wartości wsadowo i zachować dziennik importu.
