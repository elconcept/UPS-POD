# 16-gmail-app-passwords

Status: TODO

Zgodnie z najnowszym poleceniem zastąpić jedyną aktywną integrację Gmail OAuth2 wysyłką przez Gmail SMTP SSL 465 i 16-znakowe hasło aplikacji. Ekran konfiguracji: e-mail nadawcy, hasło aplikacji, Zapisz wyłączony gdy pusto, weryfikacja połączenia; backend zapisuje sekret w chronionym .env (bez ekspozycji), bezpieczne logi i brak tokenów w UI. Zgłoszenia UPS i automatyczne powiadomienia powinny korzystać z tej samej konfiguracji. OAuth przestać oferować, ale nie usuwać bez potrzeby danych użytkownika. Testy jednostkowe, E2E i brak prawdziwych maili podczas testów.
