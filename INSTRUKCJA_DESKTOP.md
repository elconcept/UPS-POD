# UPS POD Desktop (Qt) — Windows i macOS

## Co dodano

- Graficzne okno Qt 6 (PySide6): klucze UPS, wskazanie Excela, pobranie POD, odświeżenie CHECK, test trackingu, harmonogram, folder PDF, logi i podgląd do 250 statusów.
- PyInstaller tworzy aplikację bez potrzeby instalacji Pythona na komputerze użytkownika.
- Do paczki **wchodzą** Python, PySide6, openpyxl, requests, Playwright z driverem Node i Chromium headless shell (PDF z HTML). Do działania UPS API nadal potrzebny jest internet i prawidłowe klucze.
- Binarna aplikacja z `run --auto` obsługuje ten sam harmonogram (wtorek i czwartek, 15:00). Nie jest potrzebny `pythonw.exe` ani plik `ups_pod.py` po stronie użytkownika.
- **Excel i klucze są poza binarką:** w folderze domowym `~/UPS_POD` (`C:\Users\<użytkownik>\UPS_POD` na Windows). Jeśli Excel jest w innym miejscu, wskaż go z GUI. Niczego z istniejącego `DATA`, `CHECK` i `POD` nie nadpisuj podczas wdrażania. Przy pierwszym uruchomieniu aplikacja kopiuje tylko domyślny `ustawienia.ini`, jeśli go nie ma. Plik `apka.xlsx` nie jest dołączany do publicznych binarek, bo zawiera dane przesyłek.

## Kompilacja lokalna (potrzebna tylko osobie budującej wydanie)

Kompiluje się **oddzielnie na każdym systemie** (PyInstaller nie jest kompilatorem krzyżowym):

- **Windows x64:** zainstaluj Python 3.12; dwuklik `BUILD_WINDOWS.bat` → `dist\UPSPOD\UPSPOD.exe` wraz z podfolderem `_internal`. Kopiuj **cały folder** `dist\UPSPOD` — samo `.exe` nie wystarczy.
- **macOS (Apple Silicon lub Intel):** Python 3.12; uruchom `bash BUILD_MAC.command` → `dist/UPSPOD.app`. Kopiuj cały `.app` (nie tylko plik z `Contents/MacOS`). Buduj na odpowiedniej architekturze.

Budowanie instaluje pakiety z `requirements-desktop.txt`, pobiera Chromium headless shell i dołącza je do aplikacji. To duża paczka (setki MB rozpakowana). Binarki nie zawierają Client ID, Client Secret ani historii przesyłek.

## Automatyczne kompilowanie — GitHub Actions

W repozytorium skopiuj **całą strukturę projektu** wraz z `.github/workflows/build-desktop.yml`. W GitHub otwórz **Actions → UPS POD desktop binaries → Run workflow**. Uruchomienie buduje trzy archiwa: Windows x64, macOS arm64 (Apple Silicon) i macOS x64 (Intel). Pobierz archiwum z sekcji **Artifacts** danego uruchomienia. Dla prywatnego repozytorium sprawdź dostępne minuty i miejsce w GitHub Actions.

> Te automatyczne buildy trzeba jeszcze faktycznie uruchomić na runnerach Windows/macOS. Wydania są niesygnowane i nienotaryzowane — Windows SmartScreen / macOS Gatekeeper może wyświetlić ostrzeżenie. W środowisku firmowym właściwe jest podpisanie kodu (Apple Developer ID + notarization i certyfikat Authenticode dla Windows).

## Pierwsze uruchomienie binarki

1. **Mac:** przenieś `UPSPOD.app` do `~/UPS_POD` (zachowaj istniejący tam Excel i PDF-y). **Windows:** rozpakuj cały folder `UPSPOD` w dogodnym miejscu. Dane są w `~/UPS_POD`, niezależnie od miejsca binarki.
2. Uruchom przez dwuklik. W oknie wybierz plik `apka.xlsx`, jeśli nie znajduje się w `~/UPS_POD`.
3. Uzupełnij Client ID i Client Secret w **Zapisz klucze** → **Sprawdź API** (sekret nie jest wypisywany w dzienniku aplikacji).
4. **Test jednego trackingu** sprawdza, czy UPS API daje Twojemu kontu prawo do POD; dopiero wtedy **Pobierz POD i zaktualizuj CHECK**.
5. Włącz harmonogram. Mac wymaga zalogowanej sesji i nie powinien być wyłączony o 15:00; Windows również wymaga zalogowanego użytkownika. Szczegóły: wcześniejsza instrukcja UPS POD.

## Istotne ograniczenia

- Realne konto UPS API i harmonogram systemowy nie były testowane w tym środowisku. Samo zbudowanie binarki nie rozwiązuje niepewności dotyczącej udostępniania POD dla konta nadawcy Amazon `004R31`.
- PDF-y generowane na podstawie danych ze strony ups.com w starej paczce nie są tym samym co PDF-y zwrócone przez API UPS.
- CLI `ups_pod.py` pozostaje kompatybilne (dla trybu źródłowego). W binarce jego odpowiednikiem jest `UPSPOD.exe run --auto` / `UPSPOD.app/Contents/MacOS/UPSPOD run --auto`.
- Wbudowany Chromium odpowiada za HTML→PDF, nie za automatyczne pobieranie przez stronę ups.com. Nadal korzystamy tylko z istniejącego UPS API.
- Dane `~/UPS_POD/dane/klucze_ups.json` pozostają plikiem z sekretami; zabezpiecz folder przed innymi użytkownikami i nie umieszczaj go w udostępnionym repozytorium.

## Wyniki testów (2026-10-12)

- **55/55 PASS**: `python testy/test_e2e.py` w środowisku Linux, UPS API symulowane, realne Chromium systemowe.
- **3/3 PASS**: `python -m unittest -v testy.test_desktop_integration`.
- Oryginalny Excel `UPS_POD/apka.xlsx` identyczny bajtowo z plikiem wejściowym (SHA-256: `1d27d8f9a2c6a527ac52441990f5b2ec8ea962537b12db6c02941ca76a1af534`).
- **Niewykonane:** budowa binarek Windows/macOS i uruchomienie okna Qt na tych platformach (brak środowiska OS oraz możliwości pobrania PySide6/PyInstaller tutaj). Workflow `build-desktop.yml` tworzy dopiero te pakiety i sprawdza uruchomienie trybu CLI.
