> **Archiwalne (nieaktualne od 8.11.2026).** UPS-POD korzysta obecnie z Gmail SMTP i hasła aplikacji, a nie z Gmail API OAuth2. Obowiązujące instrukcje: [UPS_POD_GMAIL_APP_PASSWORD_20261108.md](UPS_POD_GMAIL_APP_PASSWORD_20261108.md).

# DSM Gmail OAuth2 a UPS-POD — ustalenia z 2026-11-08

## Fakt bazowy

DSM już wysyła powiadomienia z Gmaila po interaktywnej autoryzacji OAuth2
(jak wskazał użytkownik). To potwierdza, że Synology udostępnia
własny **klient** OAuth2 Google dla **swojego** modułu powiadomień.

## Co jest dostępne oficjalnie

1. Synology DSM → Panel sterowania → Powiadomienia → E-mail →
   Gmail → Zaloguj. Synology prowadzi interaktywny OAuth2.
   Źródło Synology:
   https://kb.synology.com/en-au/DSM/tutorial/How_to_use_Gmail_SMTP_server_to_send_emails_for_DSM

2. DSM Developer Guide definiuje powiadomienia ze skryptów / pakietów:
   `/usr/syno/bin/synonotify <mail_string_key> <mail_string_custom_variables>`
   oraz `synodsmnotify`. Operują one na systemowych zdarzeniach
   i skonfigurowanych odbiorcach, a nie na ogólnym API do wysyłania
   dowolnego MIME jako osobny nadawca.
   https://help.synology.com/developer-guide/resource_acquisition/sysnotify.html

3. Synology `OAuth Service` z Centrum pakietów **jest serwerem OAuth2
   dla usług Synology**, a nie gotowym klientem do Gmail API.
   https://kb.synology.com/en-us/DSM/help/OAuthService/oauth_service_desc

## Dlaczego nie użyto tokenów DSM

Dostęp aplikacji UPS-POD do istniejących poświadczeń modułu
powiadomień DSM nie jest wspieranym oficjalnym API delegacji Google.
Bezpieczna autoryzacja `gmail.send` nie powstaje przez skopiowanie
tokenu OAuth innej aplikacji. DSM wysyła w swoim kontekście i z
ustawionego przez siebie konta. Użytkownik oczekuje **innej skrzynki
nadawczej UPS-POD**, która wymaga oddzielnej autoryzacji tej skrzynki.
Nie instalowano narzędzi z uprawnieniami root do odczytywania tokenów
Synology, nie kopiowano żadnych poświadczeń DSM.

W szczególności: `synonotify` może posłużyć do **powiadomień NAS**
o stanie aplikacji, ale nie zastępuje wysyłki swobodnego e-maila do UPS
w imieniu innego użytkownika Gmail.

## Wybór wdrożeniowy

Pozostawić implementację UPS-POD opartą na
`google-auth-oauthlib` i Gmail API:
`https://www.googleapis.com/auth/gmail.send`.
Utworzyć w organizacji klienta OAuth2 Web dla UPS-POD
lub skorzystać z jawnie zaufanego zarządzanego pośrednika OAuth.
Skonfigurować chronione zmienne po stronie NAS
`GOOGLE_OAUTH_CLIENT_ID` i `GOOGLE_OAUTH_CLIENT_SECRET` oraz
redirect `https://ups.xlap.pl/api/config/gmail/callback`.
Pracownik naciska „Połącz Gmail przez Google” i autoryzuje **inną
skrzynkę niż ta używana przez DSM**, a Gmail wysyła przez API.

Dokumentacja integracji:
`docs/UPS_POD_GMAIL_OAUTH2.md`.

**Stan:** badań i dokumentację zakończono; nie wykonano czynnej
autoryzacji nowej skrzynki Google (wymaga zarejestrowanego klienta i
interakcji właściciela konta). Nie wysyłano testowych wiadomości.
