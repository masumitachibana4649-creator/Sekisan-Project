const addRoomButton = document.querySelector("[data-add-room]");
const roomTable = document.querySelector("[data-room-table]");
const template = document.querySelector("#room-row-template");
const estimateForm = document.querySelector("[data-estimate-form]");
const loadingOverlay = document.querySelector("[data-loading-overlay]");
const pdfInput = document.querySelector("[data-pdf-input]");
const pdfPreview = document.querySelector("[data-pdf-preview]");

if (addRoomButton && roomTable && template) {
  addRoomButton.addEventListener("click", () => {
    roomTable.append(template.content.cloneNode(true));
    const lastRow = roomTable.querySelector(".room-line:last-child input");
    if (lastRow) lastRow.focus();
  });
}

if (estimateForm && loadingOverlay) {
  estimateForm.addEventListener("submit", () => {
    loadingOverlay.classList.add("is-visible");
    loadingOverlay.setAttribute("aria-hidden", "false");

    const submitButton = estimateForm.querySelector('button[type="submit"]');
    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = "計算中";
    }
  });
}

if (pdfInput && pdfPreview) {
  let previewUrl = "";
  pdfInput.addEventListener("change", () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl);

    const file = pdfInput.files && pdfInput.files[0];
    if (!file) {
      pdfPreview.classList.add("is-hidden");
      pdfPreview.removeAttribute("href");
      previewUrl = "";
      return;
    }

    previewUrl = URL.createObjectURL(file);
    pdfPreview.href = previewUrl;
    pdfPreview.classList.remove("is-hidden");
  });
}
