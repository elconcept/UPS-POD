#!/bin/bash
# UPS POD – uruchomienie na macOS. Pierwszy raz instaluje biblioteki.
cd "$(dirname "$0")" || exit 1

PY=""
for c in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
         /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && \
     "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    PY="$c"
    break
  fi
done

if [ -z "$PY" ]; then
  echo
  echo "Nie znaleziono Pythona 3.9 lub nowszego."
  echo "Zainstaluj go ze strony https://www.python.org/downloads/ i uruchom ten plik ponownie."
  echo
  read -r -p "Naciśnij Enter, aby zamknąć…" _
  exit 1
fi

"$PY" ups_pod.py start
