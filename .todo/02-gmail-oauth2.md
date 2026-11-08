# 02-gmail-oauth2

Status: CZĘŚCIOWO ZREALIZOWANO — BLOKADA ZEWNĘTRZNA: brak zarejestrowanego klienta OAuth dla gmail.send

Sprawdzić weryfikowalny model OAuth 2.0 Gmail z minimalnym scope https://www.googleapis.com/auth/gmail.send, znaleźć utrzymywany kod open source. Usuń z głównego UI flow 16-znakowego hasła i GCP, zapewnij autoryzację i send-only, realną weryfikację autoryzacji. Nie ukrywać nieusuwalnych wymogów klienta OAuth; nie używać cudzych poświadczeń ani nie wprowadzać niebezpiecznych skrótów. Stany notice: brak zmiany = ostrzeżenie, przycisk save disabled bez danych.
\nKod: wysyłka tylko Gmail API OAuth2, UI bez haseł aplikacji i formularzy GCP, przycisk Połącz Gmail. Do ukończenia zadania wymagany zaufany klient OAuth (własny lub zarządzanego dostawcy) i rzeczywista zgoda użytkownika. Szczegóły: docs/UPS_POD_GMAIL_OAUTH2.md.\n