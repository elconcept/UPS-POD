# UPS POD Desktop — zmiany z 2026-10-12

- Dodano `UPS_POD/gui.py`: GUI PySide6, w tym obsługę Excela, kluczy UPS, testów trackingów, pełnego pobierania POD, samego CHECK, harmonogramu i logów. Przetwarzanie odbywa się w wątku roboczym, aby nie blokować okna.
- Dodano `UPSPOD.spec`: PyInstaller `onedir` z zależnościami, driverem Playwright i Chromium headless shell; na Macu tworzy `.app`.
- Dodano `build/build_app.py`, uruchamiacze Windows/Mac i workflow `.github/workflows/build-desktop.yml` (Windows x64, macOS arm64, macOS Intel).
- W `UPS_POD/ups_pod.py`: obsługa zmiennej `UPS_POD_HOME` (dla binarki katalog zapisywalny `~/UPS_POD`), uruchamianie harmonogramu z binarki `run --auto` oraz awaryjny systemowy Chromium; po nieudanym starcie przeglądarki sprzątany jest proces Playwright.
- Dotychczasowa logika UPS API, kategorie statusów, format Excela, harmonogram wt/czw 15:00 i zasada „bez zastępczych PDF” pozostają zachowane.
- Dodano szybkie testy integracyjne `testy/test_desktop_integration.py`.

**Testy**: skrypty Python skompilowane składniowo; testy integracyjne — 3/3. Pełne e2e: **55/55 PASS** w Linux na mock_ups.py (2026-10-12), w tym HTML→PDF z Chromium, obsługa wyjątków, weryfikacja XLSX i harmonogram źródłowy. GUI Qt i binarki Windows/macOS nie były uruchomione z powodu braku PySide6/PyInstaller oraz docelowych systemów w bieżącym środowisku.
