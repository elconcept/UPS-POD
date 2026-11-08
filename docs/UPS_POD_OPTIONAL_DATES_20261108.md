# UPS-POD — formatowanie dat API i zmiany nazw kolumn, 8 listopada 2026 r.

- `ship_date`: nagłówek **Nadanie** (poprzednio „Data nadania”).
- `delivery_date`: nagłówek **Doręczenie** (poprzednio „Data doręczenia”).
- `ups.pickup_date`: **Data przejęcia przez UPS**, domyślnie niewidoczna,
  wybierana w panelu KOLUMNY.
- `ups.activity_date`: **Ostatni skan** (poprzednio „Data ostatniego skanu”),
  domyślnie niewidoczna, wybierana w panelu KOLUMNY.
- `ups.activity_gmt_date`: etykieta **Ostatni skan (UTC)**.

Projektor frontendu przekształca źródłowe daty UPS `YYYYMMDD` lub
`YYYY-MM-DD` do `YYYY-MM-DD`. Daty są walidowane kalendarzowo i mają
styl typograficzny zgodny z kolumną Nadanie. Przechowywane surowe odpowiedzi
UPS i `tracking_extras` nie są przepisywane; ta sama wersja jest prezentowana
dla historycznych rekordów.

Projektor serwera umożliwia filtrowanie i sortowanie tych opcjonalnych kolumn
po jednolitym formacie ISO. Dotychczas zapisane preferencje widoczności
w cookies pozostają zachowane; użytkownik, który świadomie włączył dodatkową
kolumnę, nie traci swojej decyzji. Przy pierwszym wejściu dodatkowe daty są
niewidoczne.
