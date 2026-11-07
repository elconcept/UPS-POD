# UPS POD Web — WDT / Proof of Delivery

This web interface supplements the existing desktop code; it reuses UPS_POD/ups_pod.py for UPS OAuth2 client_credentials, status checking and original POD PDFs.

## Production installation (Synology DS1821+)
Location: /volume2/docker/ups-pod. The browser origin is https://ups.xlap.pl and is guarded by Cloudflare Access reusable policy laptopshop.pl. The application exposes **no host port**. Only the cloudflared connector shares Docker network cf_ups with it.

1. Fill /volume2/docker/ups-pod/.env (UPS_CLIENT_ID and UPS_CLIENT_SECRET). Never commit this file.
2. In Docker Compose directory: docker compose up -d --build.
3. Sign in at https://ups.xlap.pl with authorized Cloudflare Access identity.
4. Add numbers to DATA, import XLSX, use 'Sprawdź i pobierz POD', then download PDFs/Excel.
5. The separate scheduler checks Tuesday/Thursday at 15:00 Europe/Warsaw.

Files persist in /volume2/docker/ups-pod/data:
- apka.xlsx: original DATA/CHECK structure
- POD/: downloaded PDFs
- dane/kopie_arkusza/: XLSX backups
- dane/logi/: UPS scan logs
- ustawienia.ini: editable configuration

No actual UPS credentials are stored in the application data directory; they are supplied from NAS-local .env via environment variables. No GitHub Actions or repo release contains secrets.

## Important
Do an actual test of a delivered UPS tracking number after entering the credentials. Tracking API product access alone does not guarantee returned POD content; UPS requires permission to the package. POD HTML is rendered to PDF via Chromium; the source HTML is currently not separately archived. The tool is not a substitute for validating the complete WDT evidence package against invoices and shipment particulars.

## Security
Cloudflare Access is required upstream; POST requests use CSRF tokens and strict same-origin/Host validation. Containers have no published host ports. Do not add this app to a shared Docker network. Back up the data directory. Rotate the secret if exposed.
