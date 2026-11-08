# 16-gmail-app-passwords

Status: ZREALIZOWANO — wdrożono 81d433702df2c9dbe8260ffe649aa2de674e4015; GitHub Actions 42510486416 i 42510482040 zakończone sukcesem; Synology running/healthy; HTTP 200

Zgodnie z najnowszym poleceniem zastąpić jedyną aktywną integrację Gmail OAuth2 wysyłką przez Gmail SMTP SSL 465 i 16-znakowe hasło aplikacji. Ekran konfiguracji: e-mail nadawcy, hasło aplikacji, Zapisz wyłączony gdy pusto, weryfikacja połączenia; backend zapisuje sekret w chronionym .env (bez ekspozycji), bezpieczne logi i brak tokenów w UI. Zgłoszenia UPS i automatyczne powiadomienia powinny korzystać z tej samej konfiguracji. OAuth przestać oferować, ale nie usuwać bez potrzeby danych użytkownika. Testy jednostkowe, E2E i brak prawdziwych maili podczas testów.
