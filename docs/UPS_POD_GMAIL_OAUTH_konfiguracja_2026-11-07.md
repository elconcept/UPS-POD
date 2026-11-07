# Konfiguracja UPS API i Gmail OAuth w UPS-POD

Aktualizacja: 7 listopada 2026.

## Zakładka Konfiguracja

Trzeci przycisk w prawym górnym rogu pozwala:
- wyświetlić **częściowo zamaskowane** UPS Client ID i UPS Client Secret (bez wysyłania pełnych wartości do przeglądarki);
- edytować i zapisać nowe dane UPS w trwałym pliku .env na Synology;
- wprowadzić Google OAuth Web Client ID i Client Secret, uruchomić logowanie Google, wyświetlić zatwierdzony adres nadawcy i odłączyć konto.

Puste pola wpisywania nowych danych oznaczają „pozostaw istniejące”, nie „wyczyść”. Operacje zmiany konfiguracji wymagają sesji, poprawnego Origin i tokenu CSRF. Dostęp do samej aplikacji nadal ogranicza Cloudflare Access.

## Synology: .env, uprawnienia, kopia zapasowa

Plik widoczny jako:
- /volume2/docker/ups-pod/.env

jest symbolicznym odnośnikiem do:
- /volume2/docker/ups-pod/data/.env (wewnątrz kontenera: /data/.env)

Podczas migracji skopiowano dotychczasowe dane UPS bez zmiany wartości i zachowano wersję przed migracją w:
- /volume2/docker/ups-pod/.env.pre-web-config-20261107.bak

Dane są zapisane z właścicielem UID 1031 (upspodsvc), uprawnieniami 0600. Katalog data ma uprawnienia 0700. Aktualizacja odbywa się atomowo przez plik tymczasowy i os.replace, z blokadą równoległych zapisów, bez dodawania sekretów do GitHub.

Worker UPS odczytuje bieżące UPS Client ID / Secret przed kolejną operacją i odbudowuje klienta, gdy dane się zmienią. Nie trzeba ręcznie edytować pliku ani przebudowywać obrazu przy zmianie samych danych UPS.

## Połączenie z Gmail — konfiguracja operatora

Wykorzystujemy bibliotekę Google z GitHub:
https://github.com/googleapis/google-auth-library-python-oauthlib
Wersja: google-auth-oauthlib 1.5.0

Kroki:
1. W Google Cloud Console utwórz projekt i włącz Gmail API.
2. Skonfiguruj ekran zgody OAuth, w razie potrzeby dodaj konto nadawcy do użytkowników testowych.
3. Utwórz klienta OAuth 2.0 typu **Web application**.
4. Dodaj autoryzowany redirect URI dokładnie: https://ups.xlap.pl/api/config/gmail/callback
5. W zakładce Konfiguracja aplikacji zapisz Google OAuth Client ID i Client Secret.
6. Kliknij „Połącz z kontem Google”, zaloguj się na właściwy Gmail / Google Workspace i zaakceptuj zakres wysyłania.
7. Po powrocie zobaczysz zweryfikowany adres konta Gmail. To adres From używany przy wysyłce zgłoszeń.

Minimalne uprawnienie Gmail: https://www.googleapis.com/auth/gmail.send, dodatkowo openid email tylko do identyfikacji zatwierdzonego nadawcy. Nie prosimy o czytanie poczty ani modyfikowanie wiadomości. W zależności od sposobu publikacji projektu Google może wymagać weryfikacji OAuth, a w trybie Testing tokeny odświeżania mogą mieć ograniczoną trwałość.

Google zwraca token odświeżania po zgodzie offline; zapisujemy go w /volume2/docker/ups-pod/data/.gmail_oauth.json z uprawnieniami 0600. Nie trafia do .env, HTML, cookies, odpowiedzi API ani logów. Klient OAuth (Client ID i Client Secret) znajduje się w chronionym .env.

Przycisk zgłoszenia UPS wysyła po *ręcznym kliknięciu* wiadomość przez oficjalne Gmail API messages.send. Wysyłka nie odbywa się automatycznie po terminie dostawy. Dotychczasowe TLS SMTP pozostaje opcjonalną metodą awaryjną; po skutecznym połączeniu Gmail ma pierwszeństwo.

W razie błędu połączenia lub niepewnego wyniku wysyłki stan w bazie support_cases jest oznaczany do ręcznej weryfikacji, by nie wysyłać zduplikowanych żądań. Dopiero po otrzymaniu potwierdzenia od UPS można uznać sprawę za obsłużoną.

## Pobieranie ZIP dla mieszanego zaznaczenia

Jedne checkboxy pozostają dostępne do operacji FBA i ponawiania (także dla przesyłek bez POD). Przycisk „Pobierz ZIP” jest aktywny wyłącznie wtedy, gdy co najmniej jedna zaznaczona przesyłka ma gotowy plik PDF. Podaje liczbę dostępnych PDF i liczbę wybranych rekordów. Pobierane są tylko dokumenty gotowe; komunikat po pobraniu informuje o pominiętych przesyłkach bez POD, bez ukrywania błędów w panelu importu.

## Data doręczenia

Kolumna „Data doręczenia” pokazuje samą datę, bez przedrostka „Plan”. Znaczenie daty nadal jest dostępne w tooltipie: DEL potwierdzona, SDD planowana, RDD zmieniona. Opcjonalna kolumna „Typ daty UPS” może zostać włączona w menu „Kolumny”.

Źródła:
- Google OAuth dla aplikacji web: https://developers.google.com/identity/protocols/oauth2/web-server
- Gmail API users.messages.send: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send
- Gmail API zakresy: https://developers.google.com/workspace/gmail/api/auth/scopes
- Biblioteka OAuth: https://pypi.org/project/google-auth-oauthlib/1.5.0/
