"use strict";

const $ = (id) => document.getElementById(id);
const MAX_CAPTION = 2200;

let selectedFile = null;
let eventSource = null;

function log(kind, message) {
  if (!message) return;
  const line = document.createElement("div");
  line.className = `k-${kind}`;
  line.textContent = message;
  $("log").appendChild(line);
  $("log").scrollTop = $("log").scrollHeight;
}

function normalizeTags(raw) {
  const seen = new Set();
  return raw
    .split(/[,\s]+/)
    .map((part) => part.replace(/^#+/, "").replace(/[^0-9A-Za-z_\u00c0-\uffff]/g, ""))
    .filter((tag) => {
      if (!tag || seen.has(tag.toLowerCase())) return false;
      seen.add(tag.toLowerCase());
      return true;
    });
}

function renderCaption() {
  const title = $("title").value.trim();
  const tags = normalizeTags($("hashtags").value);
  const preview = $("caption-preview");
  preview.textContent = "";
  if (title) preview.appendChild(document.createTextNode(title + (tags.length ? " " : "")));
  tags.forEach((tag, index) => {
    const span = document.createElement("span");
    span.className = "tag";
    span.textContent = `#${tag}`;
    preview.appendChild(span);
    if (index < tags.length - 1) preview.appendChild(document.createTextNode(" "));
  });

  const length = (title + (tags.length ? " " : "") + tags.map((t) => `#${t}`).join(" ")).length;
  $("count").textContent = `${length} / ${MAX_CAPTION}`;
  $("count").style.color = length > MAX_CAPTION ? "var(--accent2)" : "var(--muted)";
}

function setFile(file) {
  selectedFile = file;
  if (!file) {
    $("file-info").textContent = "";
    $("submit").disabled = true;
    return;
  }
  const mib = (file.size / 1048576).toFixed(1);
  $("file-info").textContent = `${file.name} — ${mib} MiB`;
  $("submit").disabled = false;
}

let apiTokenPresent = false;

// The API backend posts the moment the file lands on TikTok's servers, so there
// is no staged state a dry run could stop at.
function syncBackend() {
  const isApi = $("backend").value === "api";
  $("dry_run").disabled = isApi;
  if (isApi) $("dry_run").checked = false;
  const banner = $("cookie-banner");
  if (isApi && !apiTokenPresent) {
    banner.classList.remove("hidden");
    banner.textContent = "No API token yet. Run: tthq api-login --client-key ... --client-secret ...";
  }
  syncButton();
}

function syncButton() {
  const uploading = $("upload_after_encode").checked;
  const dry = $("dry_run").checked;
  const skip = $("skip_encode").checked;
  let label = skip ? "Upload as-is" : "Encode";
  if (uploading) label = dry ? `${skip ? "Stage" : "Encode + stage"} (dry run)` : `${skip ? "Upload" : "Encode + post"}`;
  $("submit").textContent = label;
}

async function loadOptions() {
  const response = await fetch("/api/options");
  const data = await response.json();

  data.resolutions.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value === 1080 ? "1080p (recommended)" : `${value}p`;
    $("resolution").appendChild(option);
  });
  data.qualities.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    option.selected = value === "max";
    $("quality").appendChild(option);
  });

  const visibilityLabels = {
    public: "Everyone (public)",
    friends: "Friends only",
    private: "Only me (private)",
  };
  const keep = document.createElement("option");
  keep.value = "";
  keep.textContent = "Leave TikTok's default";
  $("visibility").appendChild(keep);
  data.visibilities.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = visibilityLabels[value] || value;
    $("visibility").appendChild(option);
  });

  apiTokenPresent = Boolean(data.api_token_present);
  if (!data.cookies_present && !apiTokenPresent) {
    const banner = $("cookie-banner");
    banner.classList.remove("hidden");
    banner.textContent =
      "No cookie file configured, so uploading is disabled. Restart with: tthq serve --cookies path/to/cookies.txt";
    $("upload_after_encode").disabled = true;
  }
  syncBackend();
  syncButton();
}

function streamJob(id) {
  if (eventSource) eventSource.close();
  eventSource = new EventSource(`/api/jobs/${id}/events`);
  eventSource.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.kind === "heartbeat") return;
    if (data.kind === "closed") {
      eventSource.close();
      $("submit").disabled = false;
      return;
    }
    log(data.kind, data.message);
    if (data.output) {
      const link = $("download");
      link.href = `/api/jobs/${id}/download`;
      link.classList.remove("hidden");
    }
    if (data.screenshots) {
      data.screenshots.forEach((path) => log("info", `screenshot: ${path}`));
    }
  };
  eventSource.onerror = () => {
    eventSource.close();
    $("submit").disabled = false;
  };
}

async function submit() {
  if (!selectedFile) return;
  $("submit").disabled = true;
  $("log").textContent = "";
  $("download").classList.add("hidden");

  const form = new FormData();
  form.append("video", selectedFile);
  form.append("title", $("title").value);
  form.append("hashtags", $("hashtags").value);
  form.append("resolution", $("resolution").value);
  form.append("quality", $("quality").value);
  form.append("fps", $("fps").value);
  form.append("fit", $("fit").value);
  form.append("orientation", $("orientation").value);
  form.append("denoise", $("denoise").checked);
  form.append("two_pass", $("two_pass").checked);
  form.append("skip_encode", $("skip_encode").checked);
  form.append("upload_after_encode", $("upload_after_encode").checked);
  form.append("dry_run", $("dry_run").checked);
  form.append("visibility", $("visibility").value);
  form.append("backend", $("backend").value);

  log("info", `Uploading ${selectedFile.name} to the local server...`);
  const response = await fetch("/api/jobs", { method: "POST", body: form });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }));
    log("error", typeof detail.detail === "string" ? detail.detail : JSON.stringify(detail));
    $("submit").disabled = false;
    return;
  }
  const job = await response.json();
  log("info", `Job ${job.id} started.`);
  streamJob(job.id);
}

$("drop").addEventListener("click", () => $("file").click());
$("drop").addEventListener("dragover", (event) => {
  event.preventDefault();
  $("drop").classList.add("over");
});
$("drop").addEventListener("dragleave", () => $("drop").classList.remove("over"));
$("drop").addEventListener("drop", (event) => {
  event.preventDefault();
  $("drop").classList.remove("over");
  if (event.dataTransfer.files.length) setFile(event.dataTransfer.files[0]);
});
$("file").addEventListener("change", (event) => {
  if (event.target.files.length) setFile(event.target.files[0]);
});
$("title").addEventListener("input", renderCaption);
$("hashtags").addEventListener("input", renderCaption);
["upload_after_encode", "dry_run", "skip_encode"].forEach((id) =>
  $(id).addEventListener("change", syncButton)
);
$("backend").addEventListener("change", syncBackend);
$("submit").addEventListener("click", submit);

renderCaption();
loadOptions();
