# UPS-POD: paski okresów, narzędzia tabeli i Cloudflare Access (2026-11-07)

## Podsumowanie zmian UI

- Statystyki „Bez POD według miesiąca nadania” zawierają 12 ostatnich miesięcy kalendarzowych oraz bieżący rok. Każda liczba jest przyciskiem filtrowania.
- Kliknięcie przycisku ustawia okres i zakres „Bez POD”; czyści niezależne wyszukiwanie tekstowe i filtry kolumn, aby wyświetlone rekordy odpowiadały licznikowi. Drugie kliknięcie aktywnego okresu wyłącza filtr daty.
- Backend GET /api/shipments akceptuje month=RRRR-MM, month=RRRR i month=all. Data: ship_date (z UPS) lub created_at, jeżeli data nadania nie została jeszcze pobrana.
- Pasek operacji jest podzielony na trzy grupy: ZIP / XLSX / FBA, PONÓW API, WYCZYŚĆ FILTRY / DOMYŚLNY WIDOK / KOLUMNY. Usunięto przycisk pobrania *wszystkich* PDF w ZIP. Eksport przez zaznaczone checkboxy pozostaje.
- WYCZYŚĆ FILTRY zeruje zakres, okres, wyszukiwanie tekstowe i filtry kolumn. DOMYŚLNY WIDOK przywraca kolejność, szerokości i widoczność kolumn oraz domyślne sortowanie według daty nadania.
- W kolumnie Dokument dla oczekujących przesyłek ikona to przekreślony PDF z symbolem zegara; kliknięcie otwiera logi danej przesyłki.
- W menu KOLUMNY przeniesiono Wagę do grupy dodatkowych danych. Techniczne ścieżki UPS API są w title/tooltip, a nie w samych opisach. Nazwy kolumn „Okno do/od” zastąpiono znaczącymi opisami „Koniec/Początek przedziału doręczenia”.
- Po odświeżeniu przyciski miesięczne zachowują tożsamość DOM i fokus; dostępne są aria-label i aria-pressed. Responsywność: 13 przycisków obok siebie na szerokim ekranie, siatka 7/4 kolumn na wąskim ekranie.
- Filtry, widoczność kolumn, szerokości, kolejność i zakres okresu nadal są zapisywane w cookie.

## Cloudflare Access dla ups.xlap.pl

Aplikacja: UPS POD WDT, identyfikator bd30339b-65d9-4afb-bba8-18e63f3fe270.

Jedyny dopuszczony login Identity Provider: Google Workspace, identyfikator 87b4360f-82d3-41cb-bbb9-b205aa97bd82. allowed_idps zawiera wyłącznie ten identyfikator; auto_redirect_to_identity=false, więc Cloudflare prezentuje przycisk logowania zamiast natychmiastowego przekierowania. OTP nadal istnieje globalnie na koncie Cloudflare dla innych usług, ale nie jest dostępny dla tej aplikacji.

Dozwolone alternatywy (logiczna alternatywa między politykami, warunki łączne wewnątrz każdej):
1. ElectroSmart – Google Workspace: include email_domain electrosmart.pl, require login_method Google, polityka 76a95bc0-ebf4-49b9-b8c3-2689476865eb.
2. Laptopshop – Google Workspace (domena): include email_domain laptopshop.pl, require login_method Google, polityka e0c7600b-e005-45f3-a56e-a8da62845c05.

Z aplikacji UPS usunięto powiązanie wcześniejszej polityki wielokrotnego użytku laptopshop.pl (id 01d5095b-f8ab-4027-b7db-a5d1b8d40af3), ponieważ w UPS jej include dopuszczało sam Google login_method, a nie tylko domenę. Nie skasowano tej reguły z innych aplikacji (zmiana globalna wymagałaby odrębnego audytu).

Niezalogowany request https://ups.xlap.pl/ przekierowuje na laptopshop.cloudflareaccess.com, strona zawiera opcję Google i nie ma formularza e-mail PIN w wykrytym HTML. Nie wykonano rzeczywistego logowania firmowego użytkownika.
