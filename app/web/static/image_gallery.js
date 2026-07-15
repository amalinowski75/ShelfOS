// Header image gallery for the component-detail page (§10). Renders right-aligned
// thumbnails of the component's image attachments; clicking one opens a
// full-size lightbox with prev/next navigation. Read-only: view-only, no gating
// needed (download is a GET). Images are detected by filename extension, so the
// attachment "kind" doesn't have to be "photo".
//
// No server-side thumbnailing exists, so a thumbnail is the full image scaled by
// CSS — fine for the modest counts expected; a resize step is a backlog item.

const IMAGE_EXTENSION = /\.(png|jpe?g|gif|webp|bmp|avif)$/i;

const gallery = document.getElementById("component-images");
const dialog = document.getElementById("image-lightbox");
if (gallery && dialog) {
  const entityType = gallery.dataset.entityType;
  const entityId = gallery.dataset.entityId;
  const feed =
    `/api/attachments?entity_type=${encodeURIComponent(entityType)}` +
    `&entity_id=${encodeURIComponent(entityId)}`;

  const lightboxImg = dialog.querySelector(".lightbox-img");
  const prevBtn = dialog.querySelector(".lightbox-prev");
  const nextBtn = dialog.querySelector(".lightbox-next");

  const downloadUrl = (image) => `/api/attachments/${image.id}/download`;
  const thumbnailUrl = (image) => `/api/attachments/${image.id}/thumbnail`;

  let images = [];
  let current = 0;

  function show(index) {
    if (!images.length) return;
    current = (index + images.length) % images.length;
    const image = images[current];
    lightboxImg.src = downloadUrl(image);
    lightboxImg.alt = image.filename; // .alt is a property assignment — safe
  }

  function open(index) {
    if (!images.length) return;
    show(index);
    dialog.showModal();
  }

  function render() {
    gallery.replaceChildren();
    // Prev/next only make sense with more than one image.
    const multiple = images.length > 1;
    prevBtn.hidden = !multiple;
    nextBtn.hidden = !multiple;
    images.forEach((image, index) => {
      // A real <button> so the thumbnail is focusable and Enter/Space-operable.
      const button = document.createElement("button");
      button.type = "button";
      button.className = "component-thumb";
      button.title = image.filename;
      button.addEventListener("click", () => open(index));
      const thumb = document.createElement("img");
      thumb.src = thumbnailUrl(image); // small server-generated thumbnail
      thumb.alt = image.filename;
      thumb.loading = "lazy";
      button.appendChild(thumb);
      gallery.appendChild(button);
    });
  }

  async function load() {
    // The gallery is a nicety — the attachments panel below already surfaces
    // load failures — so warn for debugging but don't add error chrome here.
    // Shares the panel's single feed fetch (fetchAttachmentList, shared.js).
    try {
      const rows = await fetchAttachmentList(feed);
      images = rows.filter((row) => IMAGE_EXTENSION.test(row.filename));
      render();
    } catch (err) {
      console.warn("component images: could not load", err);
    }
  }

  prevBtn.addEventListener("click", () => show(current - 1));
  nextBtn.addEventListener("click", () => show(current + 1));
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft") show(current - 1);
    else if (event.key === "ArrowRight") show(current + 1);
  });
  // Click on the backdrop (the dialog itself, outside the image) closes it.
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });

  load();
}
