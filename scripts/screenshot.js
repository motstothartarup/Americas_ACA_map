// scripts/screenshot.js
// Renders docs/index.html to docs/aca_map.png in a fixed high-res size.

const path = require("path");
const fs = require("fs");
const puppeteer = require("puppeteer");

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

(async () => {
  const repoRoot = path.resolve(__dirname, "..");
  const input = path.join(repoRoot, "docs", "index.html");
  const output = path.join(repoRoot, "docs", "aca_map.png");

  if (!fs.existsSync(input)) {
    console.error("Missing docs/index.html. Run generate_map.py first.");
    process.exit(1);
  }

  // You can tweak these via env vars if you want.
  const WIDTH  = parseInt(process.env.PNG_W || "2400", 10);
  const HEIGHT = parseInt(process.env.PNG_H || "1600", 10);
  const SCALE  = parseFloat(process.env.PNG_SCALE || "2"); // deviceScaleFactor

  const browser = await puppeteer.launch({
    headless: true,
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--allow-file-access-from-files",
    ],
    defaultViewport: { width: WIDTH, height: HEIGHT, deviceScaleFactor: SCALE },
  });

  try {
    const page = await browser.newPage();
    const url = "file://" + input;

    await page.goto(url, { waitUntil: "load", timeout: 120000 });

    // Wait for Leaflet to mount
    await page.waitForSelector(".leaflet-container", { timeout: 60000 });

    // Wait until our map JS has produced at least one label and ACA_DB has a snapshot
    await page.waitForFunction(
      () =>
        window.ACA_DB &&
        window.ACA_DB.latest &&
        document.querySelectorAll(".iata-tt").length > 0,
      { timeout: 60000 }
    );

    // Ensure tiles are loaded (as best-effort)
    await page.waitForFunction(
      () => Array.from(document.querySelectorAll(".leaflet-tile"))
                 .every((img) => img.complete),
      { timeout: 60000 }
    );

    // Give stacks a moment to settle after zoom/fitBounds
    await sleep(600);

    // Nudge layout once more (helps some runners)
    await page.evaluate(() => window.dispatchEvent(new Event("resize")));
    await sleep(100);

    const mapEl = await page.$(".leaflet-container");
    if (!mapEl) throw new Error("Leaflet map element not found.");

    await mapEl.screenshot({ path: output });

    console.log("Wrote", output);
  } catch (e) {
    console.error("Screenshot failed:", e);
    process.exit(1);
  } finally {
    await browser.close();
  }
})();
