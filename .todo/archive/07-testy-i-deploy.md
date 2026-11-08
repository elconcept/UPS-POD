# 07-testy-i-deploy

Status: ZREALIZOWANO — 49 testów lokalnych, rzeczywiste UPS API E2E i GitHub Actions po ponowieniu zakończone sukcesem; produkcja 6304770 running/healthy, healthz HTTP 200

Dodać/naprawić testy, kontrolę wizualną desktop+mobile light/dark, CI z live UPS API, wdrożyć na NAS, potwierdzić commit, zdrowie kontenera, kolumnę API, zachowanie danych. Po zakończeniu zarchiwizować taski ukończone, otwarte zostawić w .todo.

Dowód: https://github.com/elconcept/UPS-POD/actions/runs/42443937932 ; kontener ups-pod-web na Synology: running/healthy, rewizja 63047709195cc1e0c46f0b903f77a0a0ca4d56e5, mount /volume1/data/UPS/POD.
