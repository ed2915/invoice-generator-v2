document.addEventListener("click", (event) => {
  const addButton = event.target.closest("[data-add-row]");
  if (addButton) {
    const kind = addButton.dataset.addRow;
    const template = document.getElementById(`${kind}-template`);
    const container = document.getElementById(`${kind}-rows`);
    if (template && container) container.appendChild(template.content.cloneNode(true));
  }

  const removeButton = event.target.closest(".remove-row");
  if (removeButton) removeButton.closest(".item-row")?.remove();

  const copyButton = event.target.closest("[data-copy-target]");
  if (copyButton) {
    const source = document.getElementById(copyButton.dataset.copyTarget);
    if (source) {
      navigator.clipboard.writeText(source.textContent.trim()).then(() => {
        copyButton.textContent = "Copied";
      });
    }
  }
});

