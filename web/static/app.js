/* UPS POD UI: API-only, no credentials in the browser. */
"use strict";
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const selected = new Set();
let scope = "all";
let offset = 0;
let total = 0;
let rows = [];
const PAGE_SIZE = 200;
const $ = (id) => document.getElementById(id);

async function requestJson(url, options = {}) {
  const headers = { "X-CSRF-Token": csrf, ...(options.headers || {}) };
  const response = await fetch(url, { credentials: "same-origin", ...options, headers });
  let payload;
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) throw new Error(payload.error || "Błąd żądania: HTTP " + response.status);
  return payload;
}

function notice(message, isError = false) {
  const el = $("notice");
  el.hidden = false;
  el.textContent = message;
  el.classList.toggle("error", isError);
}

function addCell(tr, text, className = "") {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = String(text ?? "");
  tr.appendChild(cell);
  return cell;
}

function statusInfo(row) {
  if (row.state === "ready") return ["POD pobrany", "ready", "Zarchiwizowano dokument POD"];
  if (row.state === "processing") return ["Pobieranie…", "processing", "Trwa komunikacja z UPS"];
  if (row.state === "retry") return ["Brak POD / ponowienie", "retry", row.error || "Nie udało się pobrać dokumentu"];
  return ["W kolejce", "queued", "Oczekuje na automatyczne przetworzenie"];
}

function renderRows() {
  const tbody = $("rows");
  tbody.replaceChildren();
  if (!rows.length) {
    const tr = document.createElement("tr");
    addCell(tr, "Brak numerów odpowiadających filtrowi.", "empty").colSpan = 6;
    tbody.append(tr);
  }
  for (const row of rows) {
    const tr = document.createElement("tr");
    const tickCell = document.createElement("td");
    const tick = document.createElement("input");
    tick.type = "checkbox";
    tick.checked = selected.has(row.tracking);
    tick.setAttribute("aria-label", "Zaznacz " + row.tracking);
    tick.addEventListener("change", () => {
      if (tick.checked) selected.add(row.tracking);
      else selected.delete(row.tracking);
      syncSelected();
    });
    tickCell.append(tick);
    tr.append(tickCell);
    addCell(tr, row.tracking, "track");
    const statusCell = document.createElement("td");
    const tag = document.createElement("span");
    const info = statusInfo(row);
    tag.className = "status " + info[1];
    tag.textContent = info[0];
    tag.title = info[2];
    statusCell.title = row.error || row.description || info[2];
    statusCell.append(tag);
    tr.append(statusCell);
    const last = row.last_attempt_at ? new Date(row.last_attempt_at).toLocaleString("pl-PL") : "—";
    const lastCell = addCell(tr, last);
    lastCell.title = row.state === "retry" && row.next_attempt_at
      ? "Kolejna próba: " + new Date(row.next_attempt_at).toLocaleString("pl-PL") : "";
    addCell(tr, row.attempts);
    const doc = document.createElement("td");
    if (row.state === "ready") {
      const link = document.createElement("a");
      link.className = "download-link";
      link.href = "/api/shipments/" + encodeURIComponent(row.tracking) + "/pdf";
      link.textContent = "Pobierz PDF";
      doc.append(link);
    } else {
      const dash = document.createElement("span");
      dash.className = "muted";
      dash.textContent = "—";
      doc.append(dash);
    }
    tr.append(doc);
    tbody.append(tr);
  }
  $("page-info").textContent = total
    ? "Pozycje " + (offset + 1) + "–" + (offset + rows.length) + " z " + total
    : "Brak pozycji";
  $("prev-page").disabled = offset === 0;
  $("next-page").disabled = offset + PAGE_SIZE >= total;
  $("select-page").checked = rows.length > 0 && rows.every((r) => selected.has(r.tracking));
  syncSelected();
}

function syncSelected() {
  const count = selected.size;
  $("download-selected").textContent = count
    ? "Pobierz zaznaczone ZIP (" + count + ")" : "Pobierz zaznaczone ZIP";
  $("retry-selected").textContent = count
    ? "Ponów zaznaczone (" + count + ")" : "Ponów zaznaczone";
  $("download-selected").disabled = count === 0;
  $("retry-selected").disabled = count === 0;
}

async function refresh() {
  const params = new URLSearchParams({
    scope,
    q: $("query").value.trim(),
    limit: String(PAGE_SIZE),
    offset: String(offset),
  });
  try {
    const payload = await requestJson("/api/shipments?" + params);
    rows = payload.rows;
    total = payload.total;
    $("count-total").textContent = payload.stats.total;
    $("count-ready").textContent = payload.stats.ready;
    $("count-active").textContent = payload.stats.queued + payload.stats.processing;
    $("count-retry").textContent = payload.stats.retry;
    $("refresh-status").textContent = "Aktualizacja: " + new Date().toLocaleTimeString("pl-PL");
    renderRows();
  } catch (error) {
    $("refresh-status").textContent = "Problem z połączeniem";
    notice(error.message, true);
  }
}

$("intake-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const raw = $("trackings").value;
  if (!raw.trim()) { notice("Wklej przynajmniej jeden numer.", true); return; }
  const button = $("add-btn");
  button.disabled = true;
  try {
    const result = await requestJson("/api/shipments", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ trackings: raw }),
    });
    notice("Dodano " + result.added + ", istniejące pominięto: " + result.existing
      + ", niepoprawnych: " + result.rejected.length + ". Pobieranie trwa w tle.");
    $("trackings").value = "";
    offset = 0;
    await refresh();
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});

document.querySelectorAll("[data-scope]").forEach((button) => {
  button.addEventListener("click", async () => {
    scope = button.dataset.scope;
    offset = 0;
    document.querySelectorAll("[data-scope]").forEach((b) =>
      b.classList.toggle("active", b === button));
    await refresh();
  });
});

let searchTimeout;
$("query").addEventListener("input", () => {
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(() => { offset = 0; void refresh(); }, 300);
});
$("prev-page").addEventListener("click", () => { offset = Math.max(0, offset - PAGE_SIZE); void refresh(); });
$("next-page").addEventListener("click", () => { offset += PAGE_SIZE; void refresh(); });
$("select-page").addEventListener("change", (event) => {
  for (const row of rows) {
    if (event.target.checked) selected.add(row.tracking);
    else selected.delete(row.tracking);
  }
  renderRows();
});

async function downloadZip(all) {
  try {
    const payload = all ? { all: true } : { trackings: [...selected] };
    const response = await fetch("/api/zip", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || "HTTP " + response.status);
    }
    const blob = await response.blob();
    const link = document.createElement("a");
    const url = URL.createObjectURL(blob);
    link.href = url;
    link.download = "ups-pod-potwierdzenia.zip";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    notice("Przygotowano archiwum ZIP.");
  } catch (error) { notice(error.message, true); }
}

$("download-selected").addEventListener("click", () => void downloadZip(false));
$("download-all").addEventListener("click", () => void downloadZip(true));
$("retry-selected").addEventListener("click", async () => {
  try {
    const result = await requestJson("/api/retry", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ trackings: [...selected] }),
    });
    notice("Ponownie skierowano do kolejki: " + result.requeued);
    await refresh();
  } catch (error) { notice(error.message, true); }
});
void refresh();
setInterval(() => void refresh(), 7000);

let logsBusy = false;
let logBefore = 0;
async function refreshLogs(older = false) {
  if (logsBusy || $("logs-panel").hidden) return;
  logsBusy = true;
  try {
    const data = await requestJson("/api/logs" + (older && logBefore ? "?before=" + logBefore : ""));
    if (!older) $("log-rows").replaceChildren();
    for (const row of data.items) {
      const tr = document.createElement("tr");
      for (const value of [row.created_at, row.actor, row.operation, row.tracking, row.http_status ?? "", row.description]) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.append(td);
      }
      $("log-rows").append(tr);
    }
    if (data.items.length) logBefore = data.items[data.items.length - 1].id;
    $("logs-status").textContent = "Aktualizacja " + new Date().toLocaleTimeString();
    $("logs-older").disabled = data.items.length < 200;
  } catch (error) { $("logs-status").textContent = error.message; }
  finally { logsBusy = false; }
}
$("tab-logs").addEventListener("click", () => {
  $("shipments-panel").hidden = true; $("logs-panel").hidden = false;
  void refreshLogs();
});
$("tab-shipments").addEventListener("click", () => {
  $("shipments-panel").hidden = false; $("logs-panel").hidden = true;
});
$("logs-older").addEventListener("click", () => void refreshLogs(true));
setInterval(() => void refreshLogs(), 7000);
