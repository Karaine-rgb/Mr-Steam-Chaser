// Headless browser test of the Discovery panel, run against the app in MOCK
// mode (ODDS_API_MOCK=1, so no credits are ever spent).
//   BASE_URL=http://localhost:8077 SHOT_DIR=/some/dir node tests/ui/discovery_ui.cjs
// Exits non-zero on the first failure. Called by tests/test_ui.py.
"use strict";
const { chromium } = require("playwright");

const BASE = process.env.BASE_URL || "http://localhost:8077";
const SHOTS = process.env.SHOT_DIR || "";
const WIDTHS = [360, 390, 768, 1024, 1280, 1920];
// one popular league per width, so each width has fresh matches to add
const LEAGUES = ["soccer_epl", "soccer_germany_bundesliga", "soccer_spain_la_liga", "soccer_italy_serie_a",
                 "soccer_france_ligue_one", "soccer_efl_champ"];
const failures = [];

function check(cond, msg) {
  if (!cond) failures.push(msg);
  return cond;
}

async function overflow(page, scope) {
  // Anything wider than the viewport, or poking out to the right of it.
  return page.evaluate((scope) => {
    const vw = document.documentElement.clientWidth;
    const out = [];
    if (document.documentElement.scrollWidth > vw + 1) out.push(`page scrollWidth ${document.documentElement.scrollWidth} > ${vw}`);
    const root = scope ? document.querySelector(scope) : document.body;
    for (const el of root.querySelectorAll("*")) {
      const r = el.getBoundingClientRect();
      if (!r.width || getComputedStyle(el).visibility === "hidden") continue;
      // ignore things clipped inside a scrolling list
      let clipped = false;
      for (let p = el.parentElement; p && p !== root.parentElement; p = p.parentElement) {
        const ox = getComputedStyle(p).overflowX;
        if (ox !== "visible") { const pr = p.getBoundingClientRect(); if (r.right <= pr.right + 1) { clipped = true; break; } }
      }
      if (!clipped && r.right > vw + 1) out.push(`${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}.${el.className} right=${Math.round(r.right)}`);
      if (el.scrollWidth > el.clientWidth + 1 && ["auto", "scroll"].includes(getComputedStyle(el).overflowX)) {
        out.push(`${el.tagName.toLowerCase()}#${el.id} scrolls sideways (${el.scrollWidth} > ${el.clientWidth})`);
      }
    }
    return out.slice(0, 8);
  }, scope);
}

async function run() {
  const browser = await chromium.launch();
  const errors = [];
  for (const [i, width] of WIDTHS.entries()) {
    const league = LEAGUES[i];
    const ctx = await browser.newContext({ viewport: { width, height: width < 800 ? 780 : 900 } });
    const page = await ctx.newPage();
    // External fonts are optional and may be unreachable in a sandbox.
    await page.route(/fonts\.(googleapis|gstatic)\.com/, (r) => r.abort());
    page.on("pageerror", (e) => errors.push(`${width}px pageerror: ${e.message}`));
    page.on("console", (m) => { if (m.type() === "error" && !/fonts\.g|ERR_FAILED/.test(m.text())) errors.push(`${width}px console: ${m.text()}`); });

    await page.goto(BASE + "/", { waitUntil: "networkidle" });
    await page.waitForFunction(() => /credits \d+\/\d+/.test(document.getElementById("credit-pill").textContent));
    check((await overflow(page)).length === 0, `${width}px main page overflow: ${JSON.stringify(await overflow(page))}`);
    check(await page.isVisible("#mock-pill"), `${width}px mock pill not shown`);

    // Discovery
    await page.click("#btn-discovery");
    await page.waitForSelector("#popular-chips .chip");
    await page.evaluate(() => { localStorage.clear(); });
    const chip = page.locator(`#popular-chips .chip[data-league="${league}"]`);
    if ((await chip.getAttribute("aria-pressed")) !== "true") await chip.click();
    await page.waitForSelector('#match-list input[data-event]');
    const nMatches = await page.locator("#match-list input[data-event]").count();
    check(nMatches > 0, `${width}px no ${league} matches listed`);

    // pick 3 matches
    const boxes = page.locator("#match-list input[data-event]:not(:disabled)");
    for (let i = 0; i < 3; i++) await boxes.nth(i).check();
    await page.waitForFunction(() => /3 matches in 1 league/.test(document.getElementById("cost-box").textContent));
    const cost1 = await page.textContent("#cost-box");
    check(/Each refresh costs 1 credit/.test(cost1), `${width}px expected 1 credit per refresh, got: ${cost1}`);
    check(/3 matches in 1 league/.test(cost1), `${width}px expected "3 matches in 1 league": ${cost1}`);

    // opt into totals: cost doubles
    await page.check('input.mk[value="totals"]');
    await page.waitForFunction(() => /Each refresh costs 2 credits/.test(document.getElementById("cost-box").textContent));
    await page.uncheck('input.mk[value="totals"]');
    await page.waitForFunction(() => /Each refresh costs 1 credit/.test(document.getElementById("cost-box").textContent));

    // team filter narrows the list
    const firstTeam = (await page.locator("#match-list .item .txt").first().textContent()).split(" v ")[0].trim();
    await page.fill("#team-search", firstTeam);
    const filtered = await page.locator("#match-list input[data-event]").count();
    check(filtered >= 1 && filtered < nMatches, `${width}px team filter did not narrow (${filtered}/${nMatches})`);
    await page.fill("#team-search", "");

    // paid-only bookmakers cannot be ticked
    await page.fill("#book-search", "bet365");
    check(await page.locator('#book-groups input[data-book="bet365_au"]').isDisabled(), `${width}px bet365_au should be disabled`);
    await page.fill("#book-search", "");

    const discOverflow = await overflow(page, "#modal-discovery");
    check(discOverflow.length === 0, `${width}px discovery overflow: ${JSON.stringify(discOverflow)}`);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/discovery-${width}.png`, fullPage: false });

    // add to a round named after the width (each width its own round)
    await page.fill("#round-label", `UI test ${width}`);
    await page.click("#btn-add-round");
    await page.waitForFunction(() => /Added \d+ match/.test(document.getElementById("add-status").textContent));
    const status = await page.textContent("#add-status");
    check(/Added 3 matches/.test(status), `${width}px add status: ${status}`);

    // added matches are now marked as taken
    await page.waitForSelector("#match-list .tag-in");
    await page.keyboard.press("Escape");
    await page.waitForSelector(`text=UI test ${width}`);
    check((await overflow(page)).length === 0, `${width}px overflow after adding: ${JSON.stringify(await overflow(page))}`);

    // credit modal
    await page.click("#credit-pill");
    await page.waitForSelector("#credit-stats .stat");
    await page.waitForTimeout(450); // let the open animation finish before measuring and the screenshot
    check((await overflow(page, "#modal-credits")).length === 0, `${width}px credits modal overflow`);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/credits-${width}.png` });
    await page.click("#btn-credits-close");
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/home-${width}.png`, fullPage: true });
    await ctx.close();
  }
  await browser.close();
  failures.push(...errors);
  if (failures.length) {
    console.error("UI FAILURES:\n" + failures.join("\n"));
    process.exit(1);
  }
  console.log(`UI OK at ${WIDTHS.join(", ")} px`);
}

run().catch((e) => { console.error(e); process.exit(2); });
