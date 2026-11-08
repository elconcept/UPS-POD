# UPS-POD — Gmail SMTP / hasło aplikacji, 8 listopada 2026 r.

Od tej wersji jedynym aktywnym sposobem wysyłania e-maili z UPS-POD jest
`smtplib.SMTP_SSL('smtp.gmail.com', 465)` i osobne **16-znakowe hasło aplikacji Google**
— nie hasło logowania na konto Google. Nie jest wymagane Google Cloud Console,
klient OAuth, Gmail API ani scope `gmail.send`.

## Uruchomienie

1. Na koncie nadawcy włącz weryfikację dwuetapową i, o ile konto ma taką
   możliwość, wygeneruj osobne hasło dla UPS-POD:
   https://myaccount.google.com/apppasswords.
2. UPS-POD → Konfiguracja → Gmail: wpisz adres skrzynki oraz hasło aplikacji.
   Spacje z wyświetlonego przez Google hasła są normalizowane przy zapisie.
3. Kliknij „Zapisz Gmail”, następnie „Sprawdź logowanie”.
   Sprawdzenie otwiera połączenie SSL/TLS na port 465 i uwierzytelnia SMTP,
   ale **nie wysyła wiadomości**.
4. Po pomyślnym sprawdzeniu może działać przycisk koperty wysyłający ręczne
   zgłoszenia do UPS. Powiadomienia pracownika o spóźnionych POD i przejściu
   opóźniona → doręczona wysyłają się automatycznie wyłącznie, gdy dodatkowo
   skonfigurowano jego adres e-mail.

Jeżeli nie widzisz „Hasła aplikacji” w ustawieniach konta Google,
możliwe są ograniczenia organizacji Workspace, ochrona zaawansowana albo
polityka wymuszająca klucze bezpieczeństwa. W takim przypadku Google może
nie dopuszczać tej metody, mimo poprawnego kodu aplikacji. Nie należy używać
zwykłego hasła Google.

## Bezpieczeństwo i zgodność

- Hasło jest przechowywane w chronionym `/volume2/docker/ups-pod/data/.env`
  (wewnątrz kontenera: `/data/.env`); wartości nie są zwracane do API ani
  cookies czy HTML. Przy zapisie nie loguje się hasła.
- Znacznik potwierdzenia konfiguracji w SQLite
  `verified_gmail_smtp` wiąże dokładny e-mail i hasło z udanym testem
  uwierzytelnienia. Zmiana adresu lub hasła wyłącza wysyłkę do ponownego testu.
- SMTP ma 20-sekundowy limit i zweryfikowany TLS. Nie istnieją automatyczne
  ponowienia niejednoznacznych wiadomości; stan `uncertain` wymaga weryfikacji
  folderu „Wysłane”, żeby nie wysłać duplikatu.
- Endpointy OAuth2 w obecnej aplikacji są wyłączone.
  Dane historyczne OAuth w archiwum pozostają nietknięte, ale nie są używane.
- Domyślna polityka Google traktuje hasła aplikacji jako mniej bezpieczny
  mechanizm niż OAuth2; wybór App Password nastąpił zgodnie z decyzją operatora.

Źródła Google:
https://support.google.com/mail/answer/185833
https://knowledge.workspace.google.com/admin/security/how-2-step-verification-works-with-legacy-apps

## Testy i dane użytkownika

Testy używają sztucznych wartości i lokalnych obiektów SMTP; nie pobierają
rzeczywistych haseł ani nie wysyłają wiadomości do Google lub UPS.
Nowe hasło musi zostać wprowadzone przez operatora w panelu Konfiguracja.
