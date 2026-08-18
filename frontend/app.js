"use strict";

// Vanilla JS client for the published API contract only (NFR-006):
// - all dynamic text rendered via textContent, never innerHTML
// - thumbnails loaded only through the /api/thumb proxy
// - status polled ~1s; the file is collected via fetch on completion so a
//   collect failure renders its typed message (never a raw JSON dump)
// - typed errors: the {code, message} body drives the message shown; the
//   transient codes RATE_LIMITED and SERVER_BUSY get a "Try again" affordance
// - download button disabled while this client has an active job

const form = document.getElementById("inspect-form");
const urlInput = document.getElementById("url-input");
const inspectButton = document.getElementById("inspect-button");
const errorArea = document.getElementById("error-area");
const errorMessage = document.getElementById("error-message");
const retryButton = document.getElementById("retry-button");
const videoCard = document.getElementById("video-card");
const thumbnail = document.getElementById("thumbnail");
const videoTitle = document.getElementById("video-title");
const videoDuration = document.getElementById("video-duration");
const resolutionSelect = document.getElementById("resolution-select");
const downloadButton = document.getElementById("download-button");
const jobArea = document.getElementById("job-area");
const jobState = document.getElementById("job-state");
const progressTrack = document.getElementById("progress-track");
const progressFill = document.getElementById("progress-fill");

const POLL_INTERVAL_MS = 1000;
const RETRYABLE_CODES = new Set(["RATE_LIMITED", "SERVER_BUSY"]);

let currentInfo = null;
let pollTimer = null;
let retryAction = null;

function showError(message, action) {
  errorMessage.textContent = message;
  retryAction = action || null;
  retryButton.classList.toggle("hidden", retryAction === null);
  errorArea.classList.remove("hidden");
}

// Shows the typed error; transient server-side conditions get a retry button.
function showTypedError(error, action) {
  showError(
    error.message,
    RETRYABLE_CODES.has(error.code) ? action : null
  );
}

function clearError() {
  errorMessage.textContent = "";
  retryAction = null;
  retryButton.classList.add("hidden");
  errorArea.classList.add("hidden");
}

retryButton.addEventListener("click", () => {
  const action = retryAction;
  clearError();
  if (action) action();
});

async function readError(response) {
  try {
    const body = await response.json();
    if (body && typeof body.message === "string") {
      return { code: String(body.code || ""), message: body.message };
    }
  } catch (_) { /* non-JSON body */ }
  return { code: "", message: "Something went wrong. Please try again." };
}

function formatDuration(totalSeconds) {
  const s = Math.round(totalSeconds);
  const minutes = Math.floor(s / 60);
  const seconds = String(s % 60).padStart(2, "0");
  return minutes + ":" + seconds;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  await inspect();
});

async function inspect() {
  clearError();
  videoCard.classList.add("hidden");
  jobArea.classList.add("hidden");
  inspectButton.disabled = true;
  try {
    const response = await fetch(
      "/api/info?url=" + encodeURIComponent(urlInput.value.trim())
    );
    if (!response.ok) {
      showTypedError(await readError(response), inspect);
      return;
    }
    renderInfo(await response.json());
  } catch (_) {
    showError("Could not reach the server. Check your connection.");
  } finally {
    inspectButton.disabled = false;
  }
}

function renderInfo(info) {
  currentInfo = info;
  videoTitle.textContent = info.title;
  videoDuration.textContent = "Duration: " + formatDuration(info.duration_seconds);

  // Native aspect ratio via the CSSOM — Reels render portrait, no letterboxing.
  thumbnail.style.aspectRatio = info.width + " / " + info.height;
  thumbnail.src = info.thumbnail_url; // always the /api/thumb proxy URL

  resolutionSelect.replaceChildren();
  for (const option of info.resolutions) {
    const el = document.createElement("option");
    el.value = String(option.height);
    el.textContent = option.label;
    resolutionSelect.appendChild(el);
  }
  downloadButton.disabled = false;
  videoCard.classList.remove("hidden");
}

downloadButton.addEventListener("click", startDownload);

async function startDownload() {
  if (!currentInfo) return;
  clearError();
  downloadButton.disabled = true;
  try {
    const response = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: urlInput.value.trim(),
        resolution: Number(resolutionSelect.value),
      }),
    });
    if (!response.ok) {
      showTypedError(await readError(response), startDownload);
      downloadButton.disabled = false;
      return;
    }
    const accepted = await response.json();
    startPolling(accepted.job_id);
  } catch (_) {
    showError("Could not reach the server. Check your connection.");
    downloadButton.disabled = false;
  }
}

function setProgress(value) {
  progressFill.style.width = value + "%";
  progressTrack.setAttribute("aria-valuenow", String(value));
}

function startPolling(jobId) {
  jobArea.classList.remove("hidden");
  jobState.textContent = "Queued…";
  setProgress(0);

  pollTimer = setInterval(async () => {
    let status;
    try {
      const response = await fetch("/api/status/" + encodeURIComponent(jobId));
      if (!response.ok) {
        finishJob(await readError(response));
        return;
      }
      status = await response.json();
    } catch (_) {
      return; // transient poll failure — keep polling
    }

    setProgress(status.progress);

    if (status.state === "queued") {
      jobState.textContent =
        status.queue_position != null
          ? "Queued — position " + status.queue_position
          : "Queued…";
    } else if (status.state === "running") {
      jobState.textContent = "Downloading… " + status.progress + "%";
    } else if (status.state === "completed") {
      jobState.textContent = "Done — your download is starting.";
      finishJob(null);
      collectFile(jobId);
    } else if (status.state === "failed") {
      finishJob(
        status.error || { code: "", message: "The download failed." }
      );
    }
  }, POLL_INTERVAL_MS);
}

function parseFilename(disposition) {
  if (!disposition) return null;
  const extended = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (extended) {
    try {
      return decodeURIComponent(extended[1]);
    } catch (_) { /* fall through to the plain form */ }
  }
  const plain = disposition.match(/filename="?([^";]+)"?/i);
  return plain ? plain[1] : null;
}

async function collectFile(jobId) {
  // Collect via fetch so a failure here renders its typed {code, message}
  // instead of navigating the page to a raw error body. The MP4 is buffered
  // as a Blob — fine for this app's typical short-video sizes.
  try {
    const response = await fetch("/api/file/" + encodeURIComponent(jobId));
    if (!response.ok) {
      showTypedError(await readError(response), () => collectFile(jobId));
      return;
    }
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download =
      parseFilename(response.headers.get("content-disposition")) || "video.mp4";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(objectUrl);
  } catch (_) {
    showError("Could not reach the server. Check your connection.");
  }
}

function finishJob(error) {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  downloadButton.disabled = false;
  if (error) {
    jobArea.classList.add("hidden");
    showTypedError(error, startDownload);
  }
}
