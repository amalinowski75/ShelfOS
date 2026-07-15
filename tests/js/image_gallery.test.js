import { describe, it, expect } from "vitest";
import { loadPage, tick, componentImagesFixture } from "./harness.js";

const SCRIPTS = ["image_gallery.js"];

const feedImpl = (rows) => () =>
  Promise.resolve({ ok: true, json: async () => rows });

const IMAGES = [
  { id: 3, filename: "front.png", kind: "photo", notes: null },
  { id: 5, filename: "datasheet.pdf", kind: "datasheet", notes: null },
  { id: 8, filename: "back.JPG", kind: "photo", notes: null },
];

function thumbs(document) {
  return [...document.querySelectorAll(".component-thumb")];
}

describe("image_gallery.js", () => {
  it("renders a thumbnail only for image attachments", async () => {
    const { document } = loadPage(componentImagesFixture(), SCRIPTS, {
      fetchImpl: feedImpl(IMAGES),
    });
    await tick();

    const rendered = thumbs(document);
    // The PDF is skipped; both images (any case-extension) render.
    expect(rendered).toHaveLength(2);
    // Each thumbnail is a focusable <button> wrapping the image.
    expect(rendered.every((t) => t.tagName === "BUTTON")).toBe(true);
    expect(rendered.map((t) => t.querySelector("img").getAttribute("src"))).toEqual([
      "/api/attachments/3/download",
      "/api/attachments/8/download",
    ]);
  });

  it("opens the lightbox on the clicked image", async () => {
    const { window, document } = loadPage(componentImagesFixture(), SCRIPTS, {
      fetchImpl: feedImpl(IMAGES),
    });
    await tick();

    thumbs(document)[1].click(); // the second image (id 8)
    expect(window.HTMLDialogElement.prototype.showModal).toHaveBeenCalled();
    expect(document.querySelector(".lightbox-img").getAttribute("src")).toBe(
      "/api/attachments/8/download",
    );
  });

  it("steps to the next/previous image and wraps around", async () => {
    const { document } = loadPage(componentImagesFixture(), SCRIPTS, {
      fetchImpl: feedImpl(IMAGES),
    });
    await tick();
    const img = document.querySelector(".lightbox-img");

    thumbs(document)[0].click(); // id 3
    document.querySelector(".lightbox-next").click(); // id 8
    expect(img.getAttribute("src")).toBe("/api/attachments/8/download");
    document.querySelector(".lightbox-next").click(); // wraps to id 3
    expect(img.getAttribute("src")).toBe("/api/attachments/3/download");
    document.querySelector(".lightbox-prev").click(); // wraps back to id 8
    expect(img.getAttribute("src")).toBe("/api/attachments/8/download");
  });

  it("navigates with the arrow keys", async () => {
    const { window, document } = loadPage(componentImagesFixture(), SCRIPTS, {
      fetchImpl: feedImpl(IMAGES),
    });
    await tick();
    const dialog = document.getElementById("image-lightbox");
    const img = document.querySelector(".lightbox-img");

    thumbs(document)[0].click(); // id 3
    dialog.dispatchEvent(
      new window.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }),
    );
    expect(img.getAttribute("src")).toBe("/api/attachments/8/download");
    dialog.dispatchEvent(
      new window.KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }),
    );
    expect(img.getAttribute("src")).toBe("/api/attachments/3/download");
  });

  it("closes when the backdrop (the dialog itself) is clicked", async () => {
    const { window, document } = loadPage(componentImagesFixture(), SCRIPTS, {
      fetchImpl: feedImpl(IMAGES),
    });
    await tick();
    const dialog = document.getElementById("image-lightbox");
    thumbs(document)[0].click();

    dialog.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    expect(window.HTMLDialogElement.prototype.close).toHaveBeenCalled();
  });

  it("hides the nav arrows when there is a single image", async () => {
    const { document } = loadPage(componentImagesFixture(), SCRIPTS, {
      fetchImpl: feedImpl([IMAGES[0]]),
    });
    await tick();
    expect(thumbs(document)).toHaveLength(1);
    expect(document.querySelector(".lightbox-prev").hidden).toBe(true);
    expect(document.querySelector(".lightbox-next").hidden).toBe(true);
  });

  it("renders nothing when there are no image attachments", async () => {
    const { document } = loadPage(componentImagesFixture(), SCRIPTS, {
      fetchImpl: feedImpl([{ id: 5, filename: "datasheet.pdf", kind: "datasheet" }]),
    });
    await tick();
    expect(thumbs(document)).toHaveLength(0);
  });
});
