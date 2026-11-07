# UPS-POD — motywy, kolumny, zgłoszenia do UPS

Aktualizacja: 7 listopada 2026.

## Interfejs

- Pierwsze wejście: Light Mode. Przełącznik Dark/Light w prawym górnym rogu. Wybór zapisywany w cookie ups_pod_theme.
- Kolumny: przycisk Kolumny, domyślnie ukryta Waga, 16 dodatkowych pól UPS Tracking API, kolejność DnD lub Alt + strzałki, szerokość krawędzią lub strzałkami i cookies dla widoczności, kolejności, filtrów oraz rozmiarów.
- Odczyt pól UPS: tylko allowlista bez wrażliwych danych odbiorcy, adresów, podpisu, zdjęć i oryginalnych POD. Sortowanie/filtrowanie odbywa się po stronie SQLite.
- Status UPS pokazuje currentStatus.description zamiast ogólnego typu X. Tooltip pokazuje simplifiedTextDescription i kod zdarzenia. Worker domyślnie prosi o lokalizację en_US.
- Kolejka: zielony=POD gotowy, szary=oczekuje, niebieski=przetwarzanie, pomarańczowy=termin minął bez doręczenia, czerwony=błąd.

## Zgłoszenie UPS w kolumnie Dokument

Przycisk Zgłoś UPS pojawia się wyłącznie przy przekroczonej dacie planowanej lub zmienionej (SDD/RDD), bez statusu D/Delivered i bez POD. Kliknięcie wysyła żądanie wyjaśnienia przez skonfigurowane TLS SMTP; nic nie dzieje się bez kliknięcia. Treść NIE twierdzi, że przesyłka już dotarła: prosi UPS o sprawdzenie skanów, ustalenie rzeczywistego doręczenia i wydanie POD, a dopiero przy potwierdzeniu — o poprawienie trackingu.

SQLite support_cases zapobiega duplikatom nawet przy kliknięciach równoległych. Status sent oznacza przyjęcie przez serwer SMTP, a nie założenie sprawy w UPS. pending/uncertain nie są ponawiane automatycznie (w razie problemu sprawdzić skrzynkę nadawczą). Dane odbiorcy i treść emaila nie trafiają do tabeli audytu.

## Konfiguracja poczty na Synology

W chronionym pliku /volume2/docker/ups-pod/.env należy dodać:

    UPS_SUPPORT_SMTP_HOST=smtp.firmowy.example
    UPS_SUPPORT_SMTP_PORT=587
    UPS_SUPPORT_SMTP_SECURITY=starttls
    UPS_SUPPORT_SMTP_FROM=tracking@firma.example
    UPS_SUPPORT_SMTP_USER=login
    UPS_SUPPORT_SMTP_PASSWORD=haslo-aplikacji
    UPS_SUPPORT_TO=customer.service@ups.com

Możliwy jest SMTPS na porcie 465 z UPS_SUPPORT_SMTP_SECURITY=ssl. W firmowym przekaźniku SMTP można pominąć login/hasło, ale TLS nadal jest wymagany. Adres recipient musi należeć do ups.com i nie jest przyjmowany z przeglądarki. customer.service@ups.com to adres z oficjalnych materiałów UPS, ale należy potwierdzić z UPS Polska, czy skrzynka obsługuje lokalne zgłoszenia trackingowe. Lokalny kontakt UPS: https://www.ups.com/pl/pl/support/contact-us.

Bez hosta/nadawcy SMTP endpoint zwraca HTTP 503 SMTP_NOT_CONFIGURED. Brak wysyłki nie jest zgłaszany jako sukces. Po zmianie .env zrestartuj i odtwórz kontener Compose.

## Kody i ograniczenia

- sent: wiadomość przyjęta przez serwer SMTP, nie UPS.
- pending: wysyłanie rozpoczęte, nie ponawiaj.
- uncertain: odpowiedź SMTP niejednoznaczna, nie wysyłaj kolejnej.
- SMTP_NOT_CONFIGURED: brak SMTP, bez próby wysyłki.
- INELIGIBLE: doręczono lub termin jeszcze nie minął.
- ALREADY_SENT: istnieje wcześniejsze zgłoszenie lub próba.

Dla 1Z004R316805433991 rzeczywista odpowiedź UPS en_US podała Delivery Rescheduled oraz Your delivery will be rescheduled.; kod skanu MF, nowy termin RDD 20261119. Do tej daty nie należy kwalifikować przesyłki jako przeterminowanej.

Źródła:
https://github.com/UPS-API/api-documentation/blob/main/Tracking.yaml
https://developer.ups.com/api/reference/tracking/appendix
https://www.ups.com/pl/pl/support/contact-us
https://www.ups.com/pl/pl/support/shipping-support/legal-terms-conditions/end-user-agreement
