# Gmail OAuth2 w UPS-POD — wymagania i stan

Stan po rewizji v9 (2026-11-08): w kodzie aplikacji jedyną aktywną
metodą wysyłania jest **Gmail API OAuth 2.0** z zakresem
`https://www.googleapis.com/auth/gmail.send`, oraz `openid` i `email`
potrzebnymi do identyfikacji nadawcy. Brak dostępu do odczytu, usuwania
i modyfikowania skrzynki.

Gotowy, oficjalny kod wykorzystywany w aplikacji:
- google-auth-oauthlib: https://github.com/googleapis/google-cloud-python/tree/main/packages/google-auth-oauthlib
- OAuth2 i klient HTTP: `google-auth` / `google.oauth2.credentials`
- Przesyłanie wiadomości: `POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send` z `raw` base64url.
- Dokumentacja: https://developers.google.com/workspace/gmail/api/auth/web-server
- Zakresy: https://developers.google.com/workspace/gmail/api/auth/scopes

OAuth2 wymaga **zarejestrowanego klienta** (identyfikator aplikacji
i — przy serwerowym przepływie — właściwe poświadczenia klienta),
także przy użyciu biblioteki GitHub. Istnieją dwie poprawne ścieżki:
(1) właściciel aplikacji / organizacja wystawia własnego klienta OAuth2
i konfiguruje na NAS w chronionym .env jako
`GOOGLE_OAUTH_CLIENT_ID` i `GOOGLE_OAUTH_CLIENT_SECRET`, albo
(2) korzysta się z zaufanego zarządzanego pośrednika OAuth,
który posiada zarejestrowanego klienta i jawnie obsługuje `gmail.send`.
**Nie można** prawidłowo uzyskać `gmail.send` z dowolnego konta Google
bez klienta OAuth. Nie wolno skopiować ID cudzego repozytorium,
używać hasła aplikacji ani udawać połączenia.

Wewnętrzna aplikacja dla kont Google Workspace może mieć ograniczoną
publikację tylko do domeny organizacji. Zewnętrzny projekt OAuth
korzystający z wrażliwego `gmail.send` może wymagać weryfikacji Google.

Po skonfigurowaniu zarejestrowanego klienta aplikacji operator widzi
jeden przycisk „Połącz Gmail przez Google”, a następnie ekran zgody
Google. Po zgłoszeniu zgody serwer przechowuje token odświeżania
w `/data/.gmail_oauth.json` z uprawnieniami 0600; panel nie
pokazuje pełnych tokenów. Połączenie można odłączyć; token można także
wycofać w Google. Formularze haseł aplikacji usunięto i usunięto
ich endpointy. Stare wartości pozostają na dysku tylko jako
nieużywana historyczna konfiguracja, dopóki właściciel ich nie usunie.

**Blokada produkcyjna:** stan serwera przed wdrożeniem v9:
brak zarejestrowanego klienta OAuth2 i brak podłączonego Gmaila.
Kod i UI mogą być wdrożone bez prawdziwej autoryzacji, ale funkcja
wysyłania i powiadomień pozostanie nieaktywna do czasu dostarczenia
wiarygodnego klienta OAuth.

Rozdzielenie wysyłki: zgłoszenie do UPS wyłącznie po kliknięciu
koperty; powiadomienia pracownika mogą działać automatycznie po
aktywnej autoryzacji i wprowadzeniu adresu odbiorcy w Konfiguracji.
