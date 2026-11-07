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
  if (row.state === "retry" && row.error === "Przesyłka jeszcze niedoręczona.")
    return ["Oczekuje na doręczenie", "waiting", row.description || row.error];
  if (row.state === "retry") return ["Brak POD / ponowienie", "retry", row.error || "Nie udało się pobrać dokumentu"];
  return ["W kolejce", "queued", "Oczekuje na automatyczne przetworzenie"];
}

function renderRows() {
  const tbody = $("rows");
  tbody.replaceChildren();
  if (!rows.length) {
    const tr = document.createElement("tr");
    addCell(tr, "Brak numerów odpowiadających filtrowi.", "empty").colSpan = 7;
    tbody.append(tr);
  }
  for (const row of rows) {
    const tr = document.createElement("tr");
    const tickCell = document.createElement("td");
    const tick = document.createElement("input");
    tick.type = "checkbox";
    tick.checked = selected.has(row.tracking);
    tick.setAttribute("aria-label", "Zaznacz " + row.tracking);
    tick.addEventListener("click", (event) => {
      if (event.shiftKey && tick.checked) {
        const current = rows.findIndex((item) => item.tracking === row.tracking);
        const prior = rows.slice(0, current).map((item, index) =>
          selected.has(item.tracking) ? index : -1).filter((index) => index >= 0);
        if (prior.length) {
          for (let index = prior[prior.length - 1]; index <= current; index++)
            selected.add(rows[index].tracking);
        } else selected.add(row.tracking);
      } else if (tick.checked) selected.add(row.tracking);
      else selected.delete(row.tracking);
      renderRows();
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
    const upsCell = document.createElement("td");
    const upsTag = document.createElement("span");
    const upsText = row.ups_status || "Bez statusu";
    upsTag.textContent = upsText;
    const lowerStatus = upsText.toLocaleLowerCase("pl-PL");
    const upsKind = /doręczon|dostarczon|delivered/.test(lowerStatus) ? "ready"
      : /wyjątek|problem|błąd|zwrócon|opóźnion|exception|failed/.test(lowerStatus) ? "retry"
      : /w drodze|w tranzycie|transport|on the way|in transit/.test(lowerStatus) ? "processing"
      : "queued";
    upsTag.className = "status ups-status " + upsKind;
    upsTag.title = row.ups_status_code || row.description || "";
    upsCell.append(upsTag);
    tr.append(upsCell);
    const shipDate = addCell(tr, row.ship_date || "—", "ship-date");
    shipDate.title = {
      manual: "Data podana przez użytkownika / import FBA",
      ups_pickup_date: "Pole pickupDate zwrócone przez UPS (data odbioru/nadania wg przewoźnika)",
      ups_pickup_scan: "Data skanu odbioru przesyłki przez UPS",
      pod_shipped_or_billed: "Data «Shipped or Billed on» z dokumentu UPS; nie musi oznaczać odbioru fizycznego",
    }[row.ship_date_source] || "Data nieznana";
    const fbaCell = document.createElement("td");
    fbaCell.className = "fba-cell";
    const fbaValue = document.createElement("span");
    fbaValue.textContent = row.shipment_id || "Nieprzypisany";
    fbaValue.className = row.shipment_id ? "fba-id" : "muted";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "inline-edit";
    edit.textContent = "Edytuj";
    edit.setAttribute("aria-label", "Edytuj FBA i datę " + row.tracking);
    edit.addEventListener("click", () => void editFba(row));
    fbaCell.append(fbaValue, edit);
    tr.append(fbaCell);
    const doc = document.createElement("td");
    if (row.state === "ready") {
      const link = document.createElement("a");
      link.className = "download-link";
      link.href = "/api/shipments/" + encodeURIComponent(row.tracking) + "/pdf";
      link.textContent = "Pobierz PDF";
      doc.append(link);
    } else {
      const detail = document.createElement("div");
      detail.className = "document-details";
      const last = row.last_attempt_at
        ? new Date(row.last_attempt_at).toLocaleString("pl-PL") : "Jeszcze nie sprawdzano";
      const next = row.next_attempt_at && row.state === "retry"
        ? "; następna: " + new Date(row.next_attempt_at).toLocaleString("pl-PL") : "";
      detail.textContent = "Próby: " + row.attempts + " · ostatnia: " + last + next;
      if (row.error) {
        const err = document.createElement("span");
        err.className = row.error === "Przesyłka jeszcze niedoręczona." ? "muted" : "retry-description";
        err.textContent = row.error;
        doc.append(err);
      }
      doc.append(detail);
      const logs = document.createElement("button");
      logs.type = "button";
      logs.className = "inline-edit";
      logs.textContent = "Historia";
      logs.setAttribute("aria-label", "Historia operacji " + row.tracking);
      logs.addEventListener("click", () => {
        $("tab-logs").click();
        $("logs-tracking").value = row.tracking;
        void refreshLogs();
      });
      doc.append(logs);
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
  $("fba-selected").textContent = count
    ? "Przypisz FBA zaznaczonym (" + count + ")" : "Przypisz FBA zaznaczonym";
  $("fba-selected").disabled = count === 0;
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
    notice("Dodano " + result.added + ", istniejące: " + result.existing
      + ", przypisano FBA: " + result.assigned
      + ", odrzucone fragmenty: " + result.rejected_count
      + (result.rejected.length ? " (" + result.rejected.slice(0, 8).join(", ") + ")" : "")
      + ". Pobieranie odbywa się automatycznie.", result.rejected_count > 0);
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
async function editFba(row) {
  const shipment_id = window.prompt("Amazon FBA Shipment ID dla " + row.tracking, row.shipment_id || "");
  if (shipment_id === null) return;
  const replace = Boolean(row.shipment_id && row.shipment_id !== shipment_id.toUpperCase());
  if (replace && !window.confirm("Zmienić istniejące FBA dla " + row.tracking + "?")) return;
  try {
    await requestJson("/api/fba", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entries: [{ tracking: row.tracking, shipment_id }], replace }),
    });
    notice("Przypisano FBA dla " + row.tracking + ". Datę nadania pobiera UPS.");
    await refresh();
  } catch (error) { notice(error.message, true); }
}

$("fba-selected").addEventListener("click", async () => {
  const shipment_id = window.prompt("Shipment ID dla " + selected.size + " zaznaczonych numerów UPS:");
  if (shipment_id === null) return;
  const oldAssignments = rows.filter((row) => selected.has(row.tracking)
    && row.shipment_id && row.shipment_id !== shipment_id.toUpperCase());
  if (oldAssignments.length && !window.confirm("Niektóre numery mają już inny FBA. Zastąpić go dla zaznaczonych?")) return;
  try {
    const result = await requestJson("/api/fba", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entries: [...selected].map((tracking) => ({
        tracking, shipment_id
      })), replace: oldAssignments.length > 0 }),
    });
    notice("FBA przypisano do " + result.assigned + " numerów UPS.");
    await refresh();
  } catch (error) { notice(error.message, true); }
});

$("fba-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = $("fba-rows").value.trim();
  const button = $("fba-import-btn");
  const status = $("fba-notice");
  status.hidden = false;
  status.classList.remove("error");
  if (!text) { status.textContent = "Wklej tabelę z Excela."; status.classList.add("error"); return; }
  button.disabled = true;
  try {
    const result = await requestJson("/api/fba", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    status.textContent = "Rozpoznano UPS: " + result.submitted_unique
      + ". Nowe: " + result.added + ". Przypisano FBA: " + result.assigned
      + ". Odrzucone fragmenty: " + result.rejected_count
      + (result.rejected.length ? " — " + result.rejected.slice(0, 8).join(", ") : "");
    status.classList.toggle("error", result.rejected_count > 0);
    $("fba-rows").value = "";
    await refresh();
  } catch (error) { status.textContent = error.message; status.classList.add("error"); }
  finally { button.disabled = false; }
});

void refresh();
setInterval(() => void refresh(), 7000);

let logsBusy = false;
let logBefore = 0;
let logScope = "all";
async function refreshLogs(older = false) {
  if (logsBusy || $("logs-panel").hidden) return;
  logsBusy = true;
  try {
    const params = new URLSearchParams();
    if (older && logBefore) params.set("before", String(logBefore));
    if ($("logs-tracking").value.trim()) params.set("tracking", $("logs-tracking").value.trim());
    if (logScope === "problems") params.set("levels", "WARNING,ERROR");
    else if (logScope !== "all") params.set("levels", logScope);
    const data = await requestJson("/api/logs?" + params);
    if (!older) $("log-rows").replaceChildren();
    for (const row of data.items) {
      const tr = document.createElement("tr");
      const level = ["INFO", "SUCCESS", "WARNING", "ERROR"].includes(row.level) ? row.level : "INFO";
      tr.className = "log-row level-" + level.toLowerCase()
        + " op-" + String(row.operation || "").replace(/[^a-z0-9_-]/gi, "").toLowerCase();
      for (const value of [new Date(row.created_at).toLocaleString("pl-PL"), level, row.actor,
        row.operation, row.tracking, row.http_status ?? "", row.description]) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.append(td);
      }
      $("log-rows").append(tr);
    }
    if (data.items.length) logBefore = data.items[data.items.length - 1].id;
    $("logs-status").textContent = "Aktualizacja " + new Date().toLocaleTimeString("pl-PL") + " · filtr: " + logScope;
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
$("logs-tracking").addEventListener("input", () => { logBefore = 0; void refreshLogs(false); });
setInterval(() => void refreshLogs(), 7000);

document.querySelectorAll("[data-log-scope]").forEach((button) => {
  button.addEventListener("click", () => {
    logScope = button.dataset.logScope;
    logBefore = 0;
    document.querySelectorAll("[data-log-scope]").forEach((b) =>
      b.classList.toggle("active", b === button));
    void refreshLogs(false);
  });
});
$("show-tracking-fields").addEventListener("click", async () => {
  const pre = $("tracking-fields-list");
  try {
    const data = await requestJson("/api/tracking-fields");
    pre.textContent = data.paths.length
      ? data.paths.join("\n")
      : "Brak zapisanych kluczy. Pola zostaną zarejestrowane przy kolejnym odczycie UPS API.";
  } catch (error) { pre.textContent = error.message; }
});
