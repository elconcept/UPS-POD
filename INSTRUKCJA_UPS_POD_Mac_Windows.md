# UPS POD – instrukcja (Windows i Mac)

Kopia instrukcji przekazanej użytkownikowi (Claude Doc, stan 2026-10-11).

## Co robi aplikacja i co przetestowano

Aplikacja `ups_pod.py` (Python, ten sam kod na Windows i Mac) buduje arkusz CHECK z arkusza DATA, pyta UPS Tracking API o status każdego trackingu i zapisuje dowód doręczenia (POD) jako PDF, gdy przesyłka jest doręczona. Niedoręczone sprawdza ponownie przy każdym uruchomieniu.

| Kolumna CHECK | Zawartość |
| --- | --- |
| A DATA | powielona z DATA |
| B Shipment ID | powielony z DATA |
| C List UPS | jeden tracking w wierszu, bez powtórzeń w obrębie shipmentu |
| D STATUS | aktualny status z UPS (doręczone: „Doręczono”) |
| E NAZWA PLIKU PDF | DATA-Shipment ID-tracking |
| F KOMENTARZ | czy PDF pobrano i zapisano na dysku albo dlaczego nie |

PDF trafia do `POD/<DATA>-<Shipment ID>/<nazwa z kolumny E>.pdf`.

| Obszar | Wynik | Jak sprawdzono |
| --- | --- | --- |
| CHECK z Twojego pliku | FBA15MGCZZDB: 8 trackingów, wiersze 3–10 identyczne ze wzorcem; FBA15MGCM33K: 6 trackingów (1Z004R316839236819 wpisany 5 razy → jeden wiersz) | uruchomienie na apka.xlsx |
| Statusy, PDF, ponawianie, błędy 401/429/500, plik otwarty w Excelu, dwa uruchomienia naraz | 55/55 testów zaliczonych | symulator UPS API zbudowany według oficjalnej specyfikacji UPS (UPS-API/api-documentation) |
| Instalacja od zera (środowisko, biblioteki, silnik PDF) | działa | czysty folder, START_MAC.command w Linuksie |
| Prawdziwe UPS API | nieprzetestowane | brak Twoich kluczy w tej sesji; serwer UPS odpowiada (błąd 10401 na fałszywych kluczach) |
| Harmonogram: launchd (Mac), Harmonogram zadań (Windows) | nieuruchomiony na prawdziwym systemie | sprawdzone pliki plist i XML zadania; pierwsze włączenie u Ciebie jest testem |

## Krok 1 (Mac i Windows): klucze UPS API

Potrzebujesz dwóch wartości – Client ID i Client Secret – z aplikacji założonej na Twoim koncie UPS.

1. Wejdź na [developer.ups.com](https://developer.ups.com) i zaloguj się tym samym loginem co na ups.com.
2. Profil → **My Apps** → **Add Apps**.
3. Gdy portal zapyta o cel, wybierz integrację dla własnej firmy („I want to integrate UPS technology into my business”).
4. Wybierz swój numer konta UPS (6 znaków). Jedna aplikacja = jedno konto.
5. Uzupełnij dane kontaktowe i nazwę aplikacji, np. „UPS POD”.
6. Na liście produktów zaznacz **Tracking**. Inne nie są potrzebne.
7. **Save** → na stronie aplikacji skopiuj **Client ID** i **Client Secret**. Wkleisz je w kroku 3.

Etykiety w portalu mogą się nieznacznie różnić; liczy się aplikacja z produktem Tracking i para Client ID/Secret.

Co robi z tym aplikacja: loguje się metodą OAuth client credentials (`https://onlinetools.ups.com/security/v1/oauth/token`), a potem dla każdego trackingu wywołuje `/api/track/v1/details/<tracking>` z `returnPOD=true`, `returnSignature=true`, `locale=pl_PL`. Numer konta możesz dopisać w `ustawienia.ini` (`numer_konta`) – UPS przyjmuje go przy logowaniu jako nagłówek `x-merchant-id`.

## Mac – krok po kroku

Na Macu aplikację uruchamiasz z Terminala, harmonogram działa przez launchd, a folder musi leżeć w katalogu domowym.

1. **Python.** Pobierz instalator macOS (plik .pkg) z [python.org/downloads](https://www.python.org/downloads/) → otwórz → Dalej do końca. Sprawdzenie: Terminal → `python3 --version` → 3.9 lub wyżej. Jeśli zamiast wyniku pojawi się okno „Zainstaluj narzędzia wiersza poleceń”, zamknij je i zainstaluj Pythona z python.org.
2. **Folder.** Dwuklik `UPS_POD.zip` w Pobranych → przeciągnij powstały folder `UPS_POD` do katalogu domowego (Finder → Shift+Cmd+H). Wynik: `~/UPS_POD`. Nie zostawiaj go na Biurku, w Dokumentach ani w Pobranych – macOS blokuje tam programy działające w tle i harmonogram kończy się błędem „Operation not permitted”.
3. **Uruchomienie.** Terminal (Cmd+Spacja → „Terminal” → Enter) → wpisz `bash ~/UPS_POD/START_MAC.command` → Enter. Pierwszy raz instalacja trwa 2–5 min (silnik PDF ok. 150 MB), potem pojawia się menu. Dwuklik na `START_MAC.command` też działa, ale macOS może go zablokować jako plik z internetu – wtedy Ustawienia systemowe → Prywatność i ochrona → „Otwórz mimo to”.
4. **Klucze.** Menu **1** → wklej Client ID (Cmd+V) → Enter → wklej Client Secret (Cmd+V; Terminal nic nie pokazuje podczas wklejania) → Enter. „Logowanie do UPS API działa.” = klucze poprawne.
5. **Test i pierwsze uruchomienie.** Menu **2** (test jednego doręczonego trackingu, jak czytać wynik – sekcja niżej), potem menu **3**.
6. **Harmonogram.** Menu **5**. macOS może pokazać powiadomienie o nowym elemencie działającym w tle (Python) – zostaw go włączonego: Ustawienia systemowe → Ogólne → Elementy logowania (w nowszych wersjach: Elementy logowania i rozszerzenia) → „Zezwalaj w tle”: Python włączony. Wyłączenie harmonogramu: menu **6**.
7. **Test harmonogramu bez czekania do wtorku.** Terminal → `launchctl kickstart gui/$(id -u)/pl.upspod.automat` → po minucie sprawdź log w `~/UPS_POD/dane/logi/`.

| Harmonogram na Macu | Zachowanie |
| --- | --- |
| Mechanizm | launchd, plik `~/Library/LaunchAgents/pl.upspod.automat.plist` |
| Mac uśpiony o 15:00 | uruchomi się po wybudzeniu |
| Mac wyłączony o 15:00 | termin przepada, następny we wtorek lub czwartek |
| Warunek | użytkownik zalogowany |
| Log | `dane/logi/ups_pod_RRRR-MM.log` i `dane/logi/harmonogram_mac.log` |

| Problem na Macu | Rozwiązanie |
| --- | --- |
| „Nie znaleziono Pythona 3.9 lub nowszego” | zainstaluj Pythona z python.org, uruchom ponownie |
| „zsh: permission denied” | wpisz polecenie z `bash` na początku: `bash ~/UPS_POD/START_MAC.command` |
| W logu „Operation not permitted” | folder poza katalogiem domowym → przenieś do `~/UPS_POD`, menu 5 ponownie |
| Harmonogram nie startuje | sprawdź „Zezwalaj w tle” dla Pythona, menu 5 ponownie |
| CHECK się nie zmienia | Excel na Macu nie blokuje pliku; aplikacja rozpoznaje otwarty plik po `~$apka.xlsx` i nie zapisuje arkusza – zamknij Excel |

## Windows – krok po kroku

Na Windows aplikację uruchamiasz dwuklikiem, harmonogram to Harmonogram zadań, a folder może leżeć w dowolnym miejscu na dysku lokalnym.

1. **Python.** [python.org/downloads](https://www.python.org/downloads/) → „Download Python” → uruchom instalator → w pierwszym oknie zaznacz **Add python.exe to PATH** → Install Now. Nie instaluj Pythona ze Sklepu Microsoft. Sprawdzenie: Start → „cmd” → `py --version` → 3.9 lub wyżej.
2. **Odblokuj zip.** Prawym na `UPS_POD.zip` → Właściwości → na dole zaznacz **Odblokuj** → OK. Bez tego Windows ostrzega przy każdym uruchomieniu.
3. **Folder.** Prawym na zip → Wyodrębnij wszystkie → wskaż `C:\Users\<Twoja nazwa>` → Wyodrębnij. Wynik: `C:\Users\<Twoja nazwa>\UPS_POD`. Biurko i Dokumenty też działają (Windows nie blokuje tam programów w tle); unikaj tylko folderu OneDrive z plikami „tylko online”.
4. **Uruchomienie.** Dwuklik `START_WINDOWS.bat` → otwiera się czarne okno z menu. Pierwszy raz instalacja trwa 2–5 min. Jeśli pojawi się „System Windows ochronił ten komputer”: Więcej informacji → Uruchom mimo to.
5. **Klucze.** Menu **1** → wklej Client ID (Ctrl+V albo prawy przycisk myszy w oknie) → Enter → wklej Client Secret (nic nie widać podczas wklejania) → Enter. „Logowanie do UPS API działa.” = klucze poprawne.
6. **Test i pierwsze uruchomienie.** Menu **2** (jak czytać wynik – sekcja niżej), potem menu **3**.
7. **Harmonogram.** Menu **5** tworzy zadanie „UPS POD automat”. Działa w tle, bez okna. Wyłączenie: menu **6**.
8. **Test harmonogramu bez czekania do wtorku.** Start → „Harmonogram zadań” → Biblioteka Harmonogramu zadań → „UPS POD automat” → prawym → Uruchom. Kolumna „Wynik ostatniego uruchomienia”: 0x0 = poprawnie; szczegóły w `dane\logi\`.

| Harmonogram na Windows | Zachowanie |
| --- | --- |
| Mechanizm | Harmonogram zadań, zadanie „UPS POD automat” |
| Komputer uśpiony lub wyłączony o 15:00 | uruchomi się po włączeniu i zalogowaniu |
| Zasilanie z baterii | uruchamia się również na baterii |
| Warunek | użytkownik zalogowany |
| Log | `dane\logi\ups_pod_RRRR-MM.log` |

| Problem na Windows | Rozwiązanie |
| --- | --- |
| „Nie znaleziono Pythona 3” | zainstaluj z python.org z zaznaczonym „Add python.exe to PATH” |
| Zamiast Pythona otwiera się Sklep Microsoft | Ustawienia → Aplikacje → Zaawansowane ustawienia aplikacji → Aliasy wykonywania aplikacji (Windows 10: Aplikacje i funkcje → Aliasy wykonywania aplikacji) → wyłącz python.exe i python3.exe; zainstaluj z python.org |
| Ostrzeżenie przy każdym uruchomieniu .bat | zip nie został odblokowany → krok 2, rozpakuj ponownie |
| CHECK się nie zmienia | Excel na Windows blokuje otwarty plik; aplikacja czeka do 10 min, potem zapisuje wyniki w `dane\raporty\*.csv` – zamknij Excel, następne uruchomienie uzupełni arkusz |

## Test jednego trackingu – jak czytać wynik (Mac i Windows)

Test (menu 2) na przesyłce, która na ups.com ma status Doręczono, rozstrzyga, czy UPS API daje Twojemu kontu dokument POD – zrób go przed włączeniem harmonogramu.

| Wynik testu | Znaczenie |
| --- | --- |
| Dokument POD w API: TAK | aplikacja będzie pobierać PDF sama; testowy PDF leży w `dane/test/` – otwórz i porównaj z ups.com |
| Doręczono: TAK, Dokument POD w API: NIE | UPS nie udostępnia POD Twojemu kontu – sekcja „Ryzyko” niżej |
| Relacja konta z paczką: brak | UPS nie wiąże Twojego konta z tą przesyłką (pole `userRelation`) |
| Relacja: SHIPPER albo MYC_… | konto ma związek z przesyłką (nadawca albo UPS My Choice) |

Pełna odpowiedź UPS z testu zapisuje się w `dane/test/<tracking>_odpowiedz_ups.json` (Windows: `dane\test\`).

## Praca na co dzień

Dopisujesz wiersze tylko w arkuszu DATA; CHECK, statusy i PDF-y robi aplikacja.

- DATA: A = data `RRRR-MM-DD`, B = Shipment ID, C = trackingi rozdzielone `;` (działa też przecinek, nowa linia, numer ze spacjami `1Z 004 R31 …`). Powtórzenia są usuwane.
- CHECK odtwarza się przy każdym uruchomieniu w kolejności z DATA. Status i komentarz każdego trackingu zostają zachowane. Kolumn A–F w CHECK nie edytuj ręcznie.
- Tracking z PDF-em już na dysku nie jest ponownie odpytywany.
- Plik Excel otwarty o 15:00: aplikacja czeka do 10 min; potem pobiera PDF-y mimo to, wyniki zapisuje w `dane/raporty/wynik_….csv`, a CHECK uzupełnia przy następnym uruchomieniu.
- Przed każdym zapisem powstaje kopia arkusza w `dane/kopie_arkusza/` (ostatnie 15).

| Komentarz w F | Znaczenie | Co dalej |
| --- | --- | --- |
| Pobrano i zapisano (data): folder/plik.pdf [rodzaj] | PDF jest na dysku | nic |
| Niedoręczona – PDF zostanie pobrany po doręczeniu | status w D | aplikacja sprawdzi ponownie |
| Doręczono …, ale UPS API nie zwróciło dokumentu POD | doręczona, UPS nie dał POD | ponowi; jeśli trwale – sekcja „Ryzyko” |
| PDF niepobrany: UPS: Tracking Information Not Found | UPS nie zna numeru | sprawdź numer w DATA; UPS trzyma dane 120 dni |
| błąd UPS API: HTTP 500 … | awaria po stronie UPS (po 3 próbach) | ponowi przy następnym uruchomieniu |
| błąd zapisu PDF | problem z dyskiem lub silnikiem PDF | szczegóły w `dane/logi/` |

Pole „rodzaj” mówi, w jakiej postaci UPS oddał dokument: „PDF z UPS” (zapis 1:1) albo „POD z UPS (HTML) → PDF” (aplikacja zamienia stronę UPS na PDF bez zmiany treści).

## Ryzyko: czy UPS API odda POD dla przesyłek z konta Amazon

Statusy (kolumna D) będą działać; pobieranie PDF przez API zależy od tego, czy UPS uzna, że Twoje konto ma prawa do przesyłki – tego nie da się rozstrzygnąć bez testu na Twoich kluczach.

| Element | Treść | Siła |
| --- | --- | --- |
| Fakt (specyfikacja UPS) | `pod.content`: „This is considered sensitive data and may only be returned for a user that has rights to the package.” To samo dla obrazu podpisu. | silna |
| Fakt (format numeru) | w numerze 1Z znaki 3–8 to numer konta nadawcy: dla Twoich przesyłek `004R31` | silna |
| Fakt (Twoja obserwacja) | na ups.com dla doręczonej przesyłki PDF jest zawsze do pobrania | pierwotne źródło |
| Wniosek | strona ups.com i API mają osobne zasady dostępu; dostępność PDF na stronie nie dowodzi, że API odda go Twojemu kontu | umiarkowana |
| Niewiadoma | UPS nie definiuje w specyfikacji, co znaczy „prawa do przesyłki” | – |

Jeśli test (menu 2) pokaże „Dokument POD w API: NIE” przy doręczonej przesyłce:

1. Aplikacja dalej uzupełnia statusy i ponawia próby – nic nie trzeba wyłączać.
2. Zapytaj UPS (support na developer.ups.com lub opiekun konta), czy konto może dostać dostęp do POD dla przesyłek nadanych z Twojego adresu na koncie 004R31.
3. Hipoteza do sprawdzenia: UPS My Choice for Business. Specyfikacja wymienia relacje `MYC_BUS_OUTBOUND` i `MYC_BUS_INBOUND` – rejestracja adresu firmy może dać relację z wysyłkami. Niezweryfikowane.
4. Automatyzacja strony ups.com w przeglądarce (to, co robisz ręcznie) – technicznie możliwa, ale krucha i wymaga sprawdzenia warunków korzystania z ups.com. Nie jest zbudowana.
5. Ship24 (płatny dodatek POD) – wcześniejszy wątek projektu, odpowiedź supportu nieznana.

## Problemy wspólne i bezpieczeństwo kluczy

Pierwsze miejsce do sprawdzenia przy każdym problemie: `dane/logi/ups_pod_RRRR-MM.log`.

| Objaw (Mac i Windows) | Przyczyna | Rozwiązanie |
| --- | --- | --- |
| „UPS odrzucił logowanie (HTTP 401): 10401” | zły Client ID/Secret | menu 1, wklej ponownie; sprawdź, czy aplikacja w portalu UPS ma produkt Tracking |
| „Brak połączenia z UPS” | brak internetu w chwili uruchomienia | następne uruchomienie spróbuje ponownie |
| „Aplikacja już działa” | trwa uruchomienie z harmonogramu | odczekaj kilka minut |

Problemy typowe dla jednego systemu są w sekcjach „Mac – krok po kroku” i „Windows – krok po kroku”.

Klucze są zapisane w `dane/klucze_ups.json` (na Macu z prawami tylko dla Ciebie) – tak jak klucze w narzędziach typu AWS CLI. Nie wysyłaj folderu `dane` nikomu i nie wrzucaj go do udostępnianych folderów. Jeśli klucze wyciekną: developer.ups.com → My Apps → wygeneruj nowy Client Secret → menu 1.

### Źródła

- [UPS Tracking API – specyfikacja OpenAPI (Tracking.yaml)](https://github.com/UPS-API/api-documentation/blob/main/Tracking.yaml)
- [UPS OAuth Client Credentials – specyfikacja (OAuthClientCredentials.yaml)](https://github.com/UPS-API/api-documentation/blob/main/OAuthClientCredentials.yaml)
- [Instrukcja tworzenia aplikacji UPS (7bridges, 23.10.2025)](https://help.the7bridges.com/a-guide-for-customers-to-obtain-ups-tracking-api-keys)
