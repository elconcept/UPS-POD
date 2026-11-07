# Import arkuszy Google Sheets — plan POC

**Status: niewdrożony; zatwierdzona propozycja architektury.**

Etap 1, bez Google Cloud: import XLSX/ODS (python-calamine), CSV/TSV (csv) lub wklejonego zakresu z Google Sheets. Parser przegląda wszystkie komórki każdego wiersza, szuka FBA[A-Z0-9]+ oraz wszystkich numerów 1Z[A-Z0-9]{16}, także kilku połączonych dowolnymi separatorami. Jeden FBA w wierszu przypisuje się do wszystkich znalezionych UPS. Duplikaty są usuwane, konflikty FBA trafiają do podglądu importu. Zatwierdzenie po dry-run, atomowy zapis w SQLite i raport audytowy; brak automatycznego zastępowania istniejących FBA.

Etap 2, opcjonalny: bezpośredni odczyt prywatnych arkuszy przez Google Sheets API v4 values.batchGet z zakresem spreadsheets.readonly i wyborem zakładki/wierszy. Używany jest ten sam parser i dry-run. Dostęp do prywatnych Sheets wymaga oddzielnej autoryzacji API; Cloudflare Google login i Gmail SMTP nie zapewniają dostępu do arkuszy. Rekomendacja: wdrożyć etap 1 w pierwszej kolejności.

Testy akceptacyjne: wiele numerów UPS w jednej komórce, FBA i UPS w dowolnych komórkach, różne separatory, niejednoznaczne wiersze, duplikaty, XLSX/ODS/CSV/TSV, brak formuł wykonywanych na serwerze, bezpieczne limity plików, testy E2E i brak nadpisania istniejących przypisań.
