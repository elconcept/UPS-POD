# UPS POD Desktop

Aplikacja Qt (PySide6) na Windows i macOS do automatycznego sprawdzania statusów przesyłek UPS oraz pobierania dowodów doręczenia (POD) z UPS Tracking API. Zachowuje układ arkuszy `DATA` i `CHECK`.

## Szybki start

Pobierz najnowszy ZIP z zakładki **Releases**:
- `UPSPOD-Windows-x64.zip` – cały folder aplikacji Windows (`UPSPOD.exe` oraz `_internal/`).
- `UPSPOD-macOS-arm64.zip` – Mac Apple Silicon.
- `UPSPOD-macOS-x64.zip` – Mac Intel.

Wskaż swój lokalny plik `apka.xlsx` w GUI. Excel, historia POD i klucze UPS **nie są dołączane do wydań**. Dane programu pozostają w katalogu domowym `~/UPS_POD` lub `%USERPROFILE%\UPS_POD`.

## CI/CD

Workflow `.github/workflows/build-desktop.yml` uruchamia się automatycznie **po każdym pushu nowego commita na `main`**, albo ręcznie z GitHub Actions. Najpierw uruchamia testy symulatora UPS, potem buduje trzy systemowe paczki na natywnych runnerach, sprawdza uruchomienie CLI i publikuje **jeden GitHub Release dla SHA commita** (tag `desktop-<pełne_SHA>`). Ponowienie workflow dla tego samego SHA nie tworzy duplikatu Release ani nie zmienia już opublikowanych plików. Jeśli jakikolwiek build lub test zawiedzie, Release nie jest publikowany.

Sekrety UPS nie są używane przez CI i **nie wolno ich commitować**. Do publikowania Release wystarcza wbudowany `GITHUB_TOKEN`, z uprawnieniem `contents: write` wyłącznie w zadaniu `release`. Artefakty są niesygnowane i nienotaryzowane; macOS Gatekeeper/Windows SmartScreen mogą wyświetlać ostrzeżenia. Prywatne Actions zużywają przydział minut konta GitHub.

Pełna instrukcja: [INSTRUKCJA_DESKTOP.md](INSTRUKCJA_DESKTOP.md). Workflow publikuje wydanie z każdego commita wypchniętego na `main`.

## Testy lokalne

```bash
python -m pip install -r UPS_POD/requirements.txt
python -m unittest -v testy.test_desktop_integration
python testy/test_e2e.py
```

Testy end-to-end korzystają z symulowanego UPS API, a nie z prawdziwego konta UPS. Konwersja HTML na PDF wymaga Chromium. Budowanie binarki: `python -m pip install -r requirements-desktop.txt` i `python build/build_app.py` (na systemie docelowym).
