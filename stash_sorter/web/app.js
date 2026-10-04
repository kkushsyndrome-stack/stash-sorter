"use strict";
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem("ss." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("ss." + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};
async function api(path, body) {
  const opt = { headers: { "X-Token": window.TOKEN } };
  if (body !== undefined) { opt.method = "POST"; opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
const artUrl = (key, hd) => key ? `/art/${key.split("/").map(encodeURIComponent).join("/")}.png${hd ? "?hd=1" : ""}` : "";
const stars = n => "★".repeat(n) + "☆".repeat(3 - n);
const PAGE = { 1: "Inventory", 4: "Cube", 5: "Stash" };
const GRID = { 5: [10, 10], 1: [10, 4], 4: [3, 4] };
const QUALITIES = ["normal", "superior", "inferior", "magic", "rare", "crafted", "set", "unique", "runeword"];

let S = null, stashData = null, curPlan = null, allItems = null, grail = null, rulesState = null;
let kept = new Set(store.get("kept", [])), muleSel = null;

// ---------------------------------------------------------------- tooltip
const tip = $("#tip");
function tipHtml(it) {
  const a = it.assess;
  let h = `${it.art ? `<img src="${artUrl(it.art, true)}" style="max-height:110px;max-width:110px;display:block;margin:0 auto 6px">` : ""}`;
  h += `<div class="n q-${esc(it.quality)}">${esc(it.name)}</div>`;
  if (it.name !== it.base) h += `<div class="q-${esc(it.quality)}">${esc(it.base)}</div>`;
  if (it.stats && it.stats.length) h += `<div class="stats">${it.stats.map(esc).join("<br>")}</div>`;
  if (it.socketed && it.socketed.length) h += `<div class="small muted">Socketed with ${it.socketed.map(esc).join(", ")}</div>`;
  let meta = `item level <b>${it.ilvl}</b>${it.tier ? " · " + esc(it.tier) : ""}${it.category ? " · " + esc(S.categories[it.category] || it.category) : ""}${it.where ? "<br>" + esc(it.where) : ""}`;
  if (a) {
    meta += `<br><span class="stars">${stars(a.score)}</span> ${esc(a.tier)}` +
      (a.kind === "craft" ? ` · crafts at ilvl ${a.crafted_ilvl}` : a.alvl != null ? ` · affix level ${a.alvl}` : "");
    if (a.note) meta += `<br>${esc(a.note)}`;
    if (a.as_is) meta += `<br><b style="color:var(--ok)">${a.as_is === "keep" ? "Worth keeping as-is" : "Maybe worth keeping as-is"}</b>: ${esc(a.as_is_note)}`;
  }
  if (kept.has(it.key)) meta += `<br><span style="color:var(--accent)">⚑ kept in the stash</span>`;
  return h + `<div class="meta">${meta}</div>`;
}
function showTip(e, it) { tip.innerHTML = tipHtml(it); tip.style.display = "block"; moveTip(e); }
function moveTip(e) {
  const pad = 14, r = tip.getBoundingClientRect();
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + r.width > innerWidth) x = e.clientX - r.width - pad;
  if (y + r.height > innerHeight) y = Math.max(4, innerHeight - r.height - 4);
  tip.style.left = Math.max(4, x) + "px"; tip.style.top = Math.max(4, y) + "px";
}
const hideTip = () => tip.style.display = "none";
function hoverable(el, it) {
  el.addEventListener("mouseenter", e => showTip(e, it));
  el.addEventListener("mousemove", moveTip);
  el.addEventListener("mouseleave", hideTip);
}

// ---------------------------------------------------------------- inventory grids
function renderGrid(items, w, h, opts = {}) {
  const box = document.createElement("div");
  box.className = "inv";
  box.style.width = `calc(var(--cell) * ${w} + 2px)`; box.style.height = `calc(var(--cell) * ${h} + 2px)`;
  for (const it of items) {
    const d = document.createElement("div");
    d.className = `it q-${it.quality}` + (it.ethereal ? " eth" : "") + (opts.keepable && kept.has(it.key) ? " kept" : "") +
      (it._dim ? " dim" : "") + (it._new ? " new" : "");
    d.style.left = `calc(var(--cell) * ${it.x})`; d.style.top = `calc(var(--cell) * ${it.y})`;
    d.style.width = `calc(var(--cell) * ${it.w} - 1px)`; d.style.height = `calc(var(--cell) * ${it.h} - 1px)`;
    if (it.art && S.art) d.innerHTML = `<img src="${artUrl(it.art)}" alt="" loading="lazy">`;
    else d.textContent = it.w * it.h >= 2 ? (it.ilvl ?? "") : "";
    if (it.name) hoverable(d, it);
    if (opts.keepable) d.addEventListener("click", () => {
      kept.has(it.key) ? kept.delete(it.key) : kept.add(it.key);
      store.set("kept", [...kept]); d.classList.toggle("kept"); tip.innerHTML = tipHtml(it);
    });
    box.appendChild(d);
  }
  return box;
}
function gridBox(caption, items, page, opts) {
  const wrap = document.createElement("div"); wrap.className = "gridbox";
  wrap.innerHTML = `<div class="cap">${esc(caption)}</div>`;
  const [w, h] = GRID[page];
  wrap.appendChild(renderGrid(items, w, h, opts));
  return wrap;
}

// ---------------------------------------------------------------- overview
function defaultMules() {
  const lvl = +$("#muleLevel").value || 1, hint = $("#muleHint").checked;
  const sel = {};
  for (const c of S.characters) sel[c.name] = !c.blocked && (c.level <= lvl || (hint && c.name.toLowerCase().includes("mule")));
  return sel;
}
const selectedMules = () => Object.entries(muleSel).filter(([, v]) => v).map(([k]) => k);
function renderOverview() {
  const st = S.stashes.find(s => s.file === $("#stashSel").value) || S.stashes.find(s => s.file === S.default_stash);
  const mules = S.characters.filter(c => muleSel[c.name]);
  const gold = S.characters.reduce((a, c) => a + c.gold, 0) + S.stashes.reduce((a, s) => a + s.gold, 0);
  $("#ovCards").innerHTML = [
    ["Items in shared stash", st ? st.items : 0, st ? st.file : "no stash found"],
    ["Mules selected", mules.length, `${mules.reduce((a, c) => a + Math.max(0, 140 - c.cells), 0)} free cells (approx.)`],
    ["Characters", S.characters.length, `${S.characters.filter(c => c.blocked && c.reason.startsWith("save format")).length} need a login to upgrade`],
    ["Gold", gold.toLocaleString(), "characters + shared stashes"],
    ["Game data", S.art ? "Art ✓" : "No art", S.data_source],
  ].map(([l, v, sub]) => `<div class="stat"><div class="l">${esc(l)}</div><div class="v">${esc(v)}</div><div class="small muted">${esc(sub)}</div></div>`).join("") +
    `<div class="stat"><div class="l">Save folder</div><div class="mono small" style="margin-top:6px">${esc(S.save_dir)}</div></div>`;
  const rows = [...S.characters].sort((a, b) => (b.mule_ok - a.mule_ok) || a.name.localeCompare(b.name));
  $("#charTable").innerHTML = `<tr><th>Mule</th><th>Name</th><th>Class</th><th>Lvl</th><th>Era</th><th>Items</th><th>Fill</th><th>Mostly</th><th>Gold</th><th>Notes</th></tr>` +
    rows.map(c => `<tr class="click" data-n="${esc(c.name)}"><td><input type="checkbox" data-mule="${esc(c.name)}" ${muleSel[c.name] ? "checked" : ""} ${c.blocked ? "disabled" : ""}></td>
      <td>${esc(c.name)}</td><td>${esc(c.class)}</td><td>${c.level}</td><td class="small">${esc({ "Reign of the Warlock": "RotW" }[c.era] || c.era)}</td><td>${c.items}</td>
      <td style="min-width:90px"><div class="bar"><i style="width:${Math.min(100, c.cells / 140 * 100)}%"></i></div><span class="small muted">${c.cells}/140</span></td>
      <td class="small">${c.top_category ? esc(S.categories[c.top_category] || c.top_category) + ` <span class="muted">${Math.round(c.top_share * 100)}%</span>` : '<span class="muted">empty</span>'}</td>
      <td class="small">${c.gold ? c.gold.toLocaleString() : ""}</td>
      <td class="small">${c.blocked ? `<span class="pill warn">${esc(c.reason)}</span>` : ""}</td></tr>`).join("");
  $$("[data-mule]").forEach(cb => cb.addEventListener("click", e => {
    e.stopPropagation(); muleSel[cb.dataset.mule] = cb.checked; store.set("muleSel", muleSel); renderOverview();
  }));
  $$("#charTable tr.click").forEach(tr => tr.addEventListener("click", () => showChar(tr.dataset.n)));
}
async function showChar(name) {
  const c = await api(`/api/character?name=${encodeURIComponent(name)}`);
  const v = $("#charView"); v.style.display = "block";
  v.innerHTML = `<h2>${esc(c.name)} <span class="muted small">${esc(c.class)} · level ${c.level}${c.gold ? ` · ${c.gold.toLocaleString()} gold` : ""}${c.has_cube ? " · has Horadric Cube" : ""}</span></h2>`;
  const g = document.createElement("div"); g.className = "grids";
  for (const page of [5, 1].concat(c.has_cube ? [4] : [])) g.appendChild(gridBox(PAGE[page], c.items.filter(i => i.mode === 0 && i.page === page), page));
  v.appendChild(g);
  const eq = c.items.filter(i => i.mode !== 0);
  if (eq.length) {
    const p = document.createElement("p"); p.className = "small muted"; p.style.marginTop = "10px"; p.textContent = "Equipped / belt (never moved): ";
    eq.forEach((i, n) => { const s = document.createElement("span"); s.className = `q-${i.quality}`; s.textContent = i.name + (n < eq.length - 1 ? ", " : ""); hoverable(s, i); p.appendChild(s); });
    v.appendChild(p);
  }
  v.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------------------------------------------------------- shared stash
let stashTab = 0;
async function loadStash() {
  stashData = await api(`/api/stash?file=${encodeURIComponent($("#stashSel").value)}`);
  if (!stashData.tabs[stashTab] || stashData.tabs[stashTab].type === 2) stashTab = 0;
  let n = 0;
  $("#stashTabs").innerHTML = stashData.tabs.map((t, i) => t.type === 2 ? "" :
    `<button class="btn ${i === stashTab ? "active" : ""}" data-t="${i}">${t.type === 0 ? "Tab " + (++n) : "Stackables"} <span class="muted small">(${t.type === 1 ? t.items.filter(s => s.count).length : t.items.length})</span></button>`).join("");
  $$("#stashTabs button").forEach(b => b.addEventListener("click", () => { stashTab = +b.dataset.t; loadStash(); }));
  const other = S.stashes.find(s => s.file !== stashData.file && !s.modern && s.items && !s.hardcore);
  $("#stashNote").innerHTML = stashData.modern && other ? `<div class="notice">Your older (Resurrected-era) stash <b>${esc(other.file)}</b> still holds ${other.items} item(s). Use <b>Sort &amp; Distribute → Bring the old stash forward</b> to move them in here.</div>` : "";
  const t = stashData.tabs[stashTab], body = $("#stashBody");
  body.innerHTML = "";
  if (t.type === 1) {
    const groups = {};
    for (const s of t.items) (groups[s.category] = groups[s.category] || []).push(s);
    body.innerHTML = Object.entries(groups).map(([cat, list]) => `<h3>${esc(S.categories[cat] || cat)}</h3><div class="stacks" style="margin-bottom:12px">${list.map(s =>
      `<div class="${s.count ? "" : "zero"}">${s.art && S.art ? `<img src="${artUrl(s.art)}" alt="">` : ""}<span>${esc(s.name)}</span><span class="c">${s.count}</span></div>`).join("")}</div>`).join("");
  } else {
    body.appendChild(renderGrid(t.items, 10, 10, { keepable: true }));
    body.insertAdjacentHTML("beforeend", `<p class="small muted">${t.items.length} items · ${t.items.filter(i => kept.has(i.key)).length} kept · gold ${t.gold.toLocaleString()}</p>`);
  }
}

// ---------------------------------------------------------------- sort & distribute
function planOptions() {
  return {
    mode: $("input[name=mode]:checked").value, rename: $("#rename").value, use_stackables: $("#stackables").checked,
    stash_file: $("#stashSel").value, mules: selectedMules(), mule_max_level: +$("#muleLevel").value || 1,
    mule_name_hint: $("#muleHint").checked, limit: +$("#limit").value, create_mules: +$("#createMules").value, compact: true,
    keep_in_stash: $$("#keepCats input:checked").map(o => o.value), keep_items: [...kept], crafter_level: +$("#crafter").value || undefined,
  };
}
async function preview() {
  $("#planBtn").disabled = true; $("#planOut").innerHTML = `<div class="card muted">Working out a plan…</div>`;
  try { curPlan = await api("/api/plan", planOptions()); renderPlan(); }
  catch (e) { $("#planOut").innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; }
  finally { $("#planBtn").disabled = false; }
}
function renderPlan() {
  const p = curPlan, out = $("#planOut"), L = p.labels;
  const dests = Object.keys(p.by_dest);
  const actions = p.moves + p.merges.length + p.renames.length + p.new_mules.length;
  out.innerHTML = `<div class="card"><h2>Plan preview</h2>
    ${p.notes.map(n => `<div class="notice">${esc(n)}</div>`).join("")}
    <div class="grid-cards">
      <div class="stat"><div class="l">Items moving</div><div class="v">${p.moves}</div><div class="small muted">to ${dests.length} place(s)</div></div>
      <div class="stat"><div class="l">Stacked</div><div class="v">${p.merges.length}</div><div class="small muted">into the Stackables tab</div></div>
      <div class="stat"><div class="l">Mules renamed</div><div class="v">${p.renames.length}</div></div>
      <div class="stat"><div class="l">New mules</div><div class="v">${p.new_mules.length}</div></div>
      <div class="stat"><div class="l">No room</div><div class="v" style="color:${p.unplaced.length ? "var(--warn)" : "inherit"}">${p.unplaced.length}</div><div class="small muted">${p.unplaced.length ? "stay where they are" : "everything fits"}</div></div>
      <div class="stat"><div class="l">Files changed</div><div class="v">${p.files_touched}</div></div>
    </div>
    ${p.left_in_place ? `<div class="notice">${p.left_in_place} item(s) are on a mule of a different kind but there's no room elsewhere, so they stay put.</div>` : ""}
    ${p.emptied.length ? `<div class="notice ok"><b>${p.emptied.length} mule(s) will be empty afterwards:</b> ${p.emptied.map(esc).join(", ")}. After applying, you can delete them under <b>Clean Up</b> or keep them for later.</div>` : ""}
    ${p.unplaced.length ? `<div class="notice">Not enough room for ${p.unplaced.length} item(s): ${p.unplaced.slice(0, 12).map(u => esc(u.name)).join(", ")}${p.unplaced.length > 12 ? "…" : ""}. Make a few new level-1 characters (or let Stash Sorter create some) and preview again.</div>` : ""}
    <div class="row" style="margin-top:14px">
      <button class="btn danger" id="applyBtn" ${actions ? "" : "disabled"}>Apply this plan…</button>
      <span class="small muted">${S.game_running ? "⚠ Close Diablo II: Resurrected first." : "A full backup is made first."}</span>
    </div></div>`;
  if (p.new_mules.length) out.insertAdjacentHTML("beforeend", `<div class="card"><h2>New mules</h2><p class="small muted">Copies of an empty level-1 character (no items, gold or mercenary), given a new name. Experimental — check one in game before relying on them.</p><table><tr><th>Name</th><th>For</th><th>Copied from</th></tr>${p.new_mules.map(n => `<tr><td><b>${esc(n.name)}</b></td><td>${esc(L[n.category] || n.category)}</td><td class="small">${esc(n.template)}</td></tr>`).join("")}</table></div>`);
  if (p.renames.length) out.insertAdjacentHTML("beforeend", `<div class="card"><h2>Renames</h2><div class="table-wrap"><table><tr><th>Current name</th><th>New name</th><th>Holds</th></tr>${p.renames.map(r => `<tr><td>${esc(r.old)}</td><td><b>${esc(r.new)}</b></td><td class="small">${esc(L[r.category] || r.category)}</td></tr>`).join("")}</table></div></div>`);
  if (Object.keys(p.stack_counts).length) out.insertAdjacentHTML("beforeend", `<div class="card"><h2>Stackables tab</h2><div class="stacks">${Object.entries(p.stack_counts).map(([c, [a, b]]) => `<div><span class="mono">${esc(c)}</span><span class="c">${a} → ${b}</span></div>`).join("")}</div></div>`);
  if (dests.length) {
    const cards = document.createElement("div"); cards.className = "card";
    cards.innerHTML = `<h2>Where things go</h2><p class="small muted">Outlined items are arriving; faded ones are already there. Hover for details.</p>`;
    const grid = document.createElement("div"); grid.className = "mule-cards";
    const info = Object.fromEntries(p.mules.map(m => [m.name, m]));
    for (const name of dests.sort((a, b) => p.by_dest[b].length - p.by_dest[a].length)) {
      const list = p.by_dest[name], m = info[name] || {};
      const ren = p.renames.find(r => r.old === name);
      const c = document.createElement("div"); c.className = "stat";
      c.innerHTML = `<div><b>${esc(name)}</b>${ren ? ` → <b style="color:var(--accent)">${esc(ren.new)}</b>` : ""}${m.new ? ' <span class="pill ok">new</span>' : ""} <span class="pill">${esc(L[m.category] || m.category || "stash tab")}</span></div>
        <div class="small muted">+${list.length} item(s)${m.cells_total ? ` · ${m.cells_used}/${m.cells_total} cells after` : ""}</div>${m.cells_total ? `<div class="bar"><i style="width:${m.cells_used / m.cells_total * 100}%"></i></div>` : ""}`;
      const g = document.createElement("div"); g.className = "grids"; g.style.marginTop = "8px";
      for (const page of [5, 1, 4]) {
        const items = (p.existing[name] || []).filter(i => i.page === page).map(i => ({ ...i, _dim: true, where: "already here" }))
          .concat(list.filter(i => i.page === page).map(i => ({ ...i, _new: true, where: "from " + i.from })));
        if (items.length) g.appendChild(gridBox(m.cells_total ? PAGE[page] : "Stash tab", items, page));
      }
      c.appendChild(g);
      c.insertAdjacentHTML("beforeend", `<details style="margin-top:6px"><summary class="small">List items</summary><ul class="small">${list.map(i => `<li><span class="q-${esc(i.quality)}">${esc(i.name)}</span> <span class="muted">from ${esc(i.from)}</span></li>`).join("")}</ul></details>`);
      grid.appendChild(c);
    }
    cards.appendChild(grid); out.appendChild(cards);
  }
  if (p.skipped.length) out.insertAdjacentHTML("beforeend", `<div class="card"><details><summary>Characters not used (${p.skipped.length})</summary><ul class="small">${p.skipped.map(([n, r]) => `<li>${esc(n)} — <span class="muted">${esc(r)}</span></li>`).join("")}</ul></details></div>`);
  $("#applyBtn")?.addEventListener("click", confirmApply);
}
function confirmApply() {
  const p = curPlan;
  const what = [`move <b>${p.moves}</b> item(s)`, p.merges.length && `stack <b>${p.merges.length}</b>`,
    p.renames.length && `rename <b>${p.renames.length}</b> mule(s)`, p.new_mules.length && `create <b>${p.new_mules.length}</b> new mule(s)`].filter(Boolean).join(", ");
  modal(`<h2>Apply this plan?</h2><p>This will ${what}.</p>
    <div class="notice">Close Diablo II: Resurrected completely (not just to the menu) first — the game rewrites saves when it exits.</div>
    <p class="small muted">Stash Sorter zips your whole save folder, checks every rebuilt file (every item accounted for, nothing overlapping), then writes everything as one recoverable change. If any check fails, nothing is written.</p>
    <div class="row" style="justify-content:flex-end"><button class="btn" id="mCancel">Cancel</button><button class="btn danger" id="mGo">Apply</button></div>`);
  $("#mCancel").onclick = closeModal;
  $("#mGo").onclick = async () => {
    $("#mGo").disabled = true; $("#mGo").textContent = "Applying…";
    try {
      const r = await api("/api/apply", { plan_id: p.id });
      modal(`<h2>Done ✔</h2><p>${esc(r.log_lines.at(-1) || "")}</p><p class="small">Backup: <span class="mono">${esc(r.backup)}</span></p>
        <p class="small muted">Changed your mind? <b>Backups → Undo</b> reverses exactly this change.</p><div class="row" style="justify-content:flex-end"><button class="btn primary" id="mOk">OK</button></div>`);
      $("#mOk").onclick = closeModal;
      curPlan = null; $("#planOut").innerHTML = ""; allItems = null; grail = null; await refresh();
    } catch (e) {
      modal(`<h2>Nothing was changed</h2><div class="notice bad">${esc(e.message)}</div><div class="row" style="justify-content:flex-end"><button class="btn" id="mOk">OK</button></div>`);
      $("#mOk").onclick = closeModal;
    }
  };
}
function modal(html) { $("#modalBody").innerHTML = html; $("#modal").style.display = "flex"; }
function closeModal() { $("#modal").style.display = "none"; }

// ---------------------------------------------------------------- find
async function loadFind() {
  if (!allItems) { $("#findCount").textContent = "Loading every item…"; allItems = await api("/api/items"); }
  renderFind();
}
function renderFind() {
  const words = $("#q").value.toLowerCase().split(/\s+/).filter(Boolean), fq = $("#fq").value, fc = $("#fc").value, eq = $("#fEquipped").checked;
  const hits = allItems.filter(it => (eq || it.mode === 0) && (!fq || it.quality === fq) && (!fc || it.category === fc) &&
    words.every(w => (it._t ||= [it.name, it.base, it.where, ...(it.stats || [])].join(" ").toLowerCase()).includes(w)));
  $("#findCount").textContent = `${hits.length} of ${allItems.length} items${hits.length > 300 ? " (showing the first 300)" : ""}`;
  const out = $("#findOut"); out.innerHTML = "";
  for (const it of hits.slice(0, 300)) {
    const d = document.createElement("div"); d.className = "res";
    d.innerHTML = `<div class="pic">${it.art && S.art ? `<img src="${artUrl(it.art)}" alt="" loading="lazy">` : ""}</div>
      <div><div class="q-${esc(it.quality)}">${esc(it.name)}${it.count ? ` ×${it.count}` : ""}</div><div class="where">${esc(it.where)} · ilvl ${it.ilvl}</div>
      <div class="stats">${(it.stats || []).slice(0, 4).map(esc).join("<br>")}${(it.stats || []).length > 4 ? "<br>…" : ""}</div></div>`;
    hoverable(d, it); out.appendChild(d);
  }
}

// ---------------------------------------------------------------- collection
let grailKind = "uniques";
async function loadGrail() { if (!grail) grail = await api("/api/collection"); renderGrail(); }
function renderGrail() {
  const show = $("#gShow").value, q = $("#gq").value.toLowerCase();
  const list = grail[grailKind];
  const found = list.filter(x => x.found.length).length;
  $("#grailSummary").innerHTML = `<b>${found}</b> of ${list.length} ${grailKind === "runewords" ? "runewords made" : "found"} <div class="bar" style="max-width:420px"><i style="width:${found / list.length * 100}%"></i></div>`;
  const rows = list.filter(x => (show === "all" || (show === "found") === !!x.found.length) &&
    (!q || (x.name + " " + (x.base || "") + " " + (x.set || "")).toLowerCase().includes(q)));
  let lastSet = null;
  $("#grailOut").innerHTML = rows.map(x => {
    const head = grailKind === "sets" && x.set !== lastSet ? (lastSet = x.set, `<div style="grid-column:1/-1;margin-top:6px" class="q-set"><b>${esc(x.set)}</b></div>`) : "";
    const cls = grailKind === "sets" ? "q-set" : grailKind === "uniques" ? "q-unique" : "q-runeword";
    return head + `<div class="gi ${x.found.length ? "" : "missing"}" title="${esc(x.found.join("\n") || "not found yet")}">${x.art && S.art ? `<img src="${artUrl(x.art)}" alt="" loading="lazy">` : ""}
      <div><div class="n ${cls}">${esc(x.name)}${x.found.length > 1 ? ` <span class="muted">×${x.found.length}</span>` : ""}</div><div class="b">${esc(x.base || "")}${x.found.length ? " · " + esc(x.found[0]) : ""}</div></div></div>`;
  }).join("");
}

// ---------------------------------------------------------------- item levels
let sortCol = "score", sortDir = -1, assessRows = [];
async function loadAssess() {
  assessRows = await api(`/api/assess?crafter=${$("#crafter").value}&scope=${$("#scope").value}`);
  renderAssess();
}
function renderAssess() {
  const kind = $("#kind").value, min = +$("#minTier").value, q = $("#search").value.toLowerCase(), keepOnly = $("#keepOnly").checked;
  const rows = assessRows.filter(r => (!kind || r.assess.kind === kind) && r.assess.score >= min && (!keepOnly || r.assess.as_is) &&
    (!q || (r.name + " " + r.where + " " + r.base).toLowerCase().includes(q)));
  const val = r => ({ score: r.assess.score * 1000 + r.ilvl, ilvl: r.ilvl, name: r.name, where: r.where, asis: { keep: 2, maybe: 1 }[r.assess.as_is] || 0,
    alvl: r.assess.kind === "craft" ? r.assess.crafted_ilvl : (r.assess.alvl ?? r.assess.max_sockets ?? 0), kind: r.assess.kind })[sortCol];
  rows.sort((a, b) => { const x = val(a), y = val(b); return (x > y ? 1 : x < y ? -1 : 0) * sortDir; });
  $("#assessSummary").innerHTML = ["craft", "reroll", "base"].map(k => {
    const l = assessRows.filter(r => r.assess.kind === k);
    return `<span class="pill">${{ craft: "Craft bait", reroll: "Charms & jewels", base: "Bases" }[k]}: <b>${l.length}</b> · ${l.filter(r => r.assess.score === 3).length} top-tier</span>`;
  }).join("") + `<span class="pill ok">${assessRows.filter(r => r.assess.as_is === "keep").length} worth keeping as-is</span><span class="small muted">Showing ${rows.length}</span>`;
  const head = [["name", "Item"], ["where", "Where"], ["ilvl", "ilvl"], ["kind", "Kind"], ["alvl", "Result"], ["score", "Tier"], ["asis", "As-is"], ["", "What it means"]];
  $("#assessTable").innerHTML = `<tr>${head.map(([k, l]) => `<th ${k ? `data-k="${k}"` : ""}>${l}${sortCol === k ? (sortDir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr>` +
    rows.map((r, i) => {
      const a = r.assess;
      const result = a.kind === "craft" ? `crafts at ilvl ${a.crafted_ilvl}` : a.kind === "reroll" ? `affix lvl ${a.alvl}` : `${a.max_sockets} socket max`;
      const detail = (a.note ? `<div>${esc(a.note)}</div>` : "") +
        (a.unlocked?.length || a.locked?.length ? `<details><summary class="small">${a.unlocked.length} notable unlocked · ${a.locked.length} locked</summary>
        ${a.unlocked.length ? `<div class="small" style="color:var(--ok)">✔ ${a.unlocked.map(esc).join("<br>✔ ")}</div>` : ""}
        ${a.locked.length ? `<div class="small" style="color:var(--danger)">✘ ${a.locked.map(esc).join("<br>✘ ")}</div>` : ""}</details>` : "");
      return `<tr><td class="q-${esc(r.quality)}" data-i="${i}" style="display:flex;gap:8px;align-items:center">${r.art && S.art ? `<img src="${artUrl(r.art)}" style="width:28px;height:28px;object-fit:contain" loading="lazy">` : ""}<span>${esc(r.name)}<div class="small muted">${esc(r.base)}</div></span></td>
        <td class="small">${esc(r.where)}</td><td><b>${r.ilvl}</b></td>
        <td class="small">${{ craft: "Craft bait", reroll: "Reroll", base: "Base" }[a.kind]}</td><td class="small">${result}</td>
        <td><span class="stars">${stars(a.score)}</span><div class="small">${esc(a.tier)}</div></td>
        <td class="small">${a.as_is ? `<span class="pill ${a.as_is === "keep" ? "ok" : "warn"}" title="${esc(a.as_is_note)}">${a.as_is}</span>` : ""}</td><td class="small">${detail}</td></tr>`;
    }).join("");
  $$("#assessTable td[data-i]").forEach(td => hoverable(td, rows[+td.dataset.i]));
  $$("#assessTable th[data-k]").forEach(th => th.addEventListener("click", () => {
    if (sortCol === th.dataset.k) sortDir = -sortDir; else { sortCol = th.dataset.k; sortDir = ["name", "where"].includes(th.dataset.k) ? 1 : -1; }
    renderAssess();
  }));
}

// ---------------------------------------------------------------- clean up: duplicates & empty mules
let dupGroups = null, dupPicked = new Set(), emptyList = [], emptyPicked = new Set();
async function loadCleanup() {
  [dupGroups, emptyList] = await Promise.all([api("/api/duplicates"), api("/api/empty_mules")]);
  dupPicked = new Set([...dupPicked].filter(k => dupGroups.some(g => g.copies.some(c => c.key === k))));
  emptyPicked = new Set([...emptyPicked].filter(n => emptyList.some(m => m.name === n && m.empty)));
  renderDupes(); renderEmpty();
}
function renderDupes() {
  const kind = $("#dKind").value, q = $("#dq").value.toLowerCase();
  const groups = dupGroups.filter(g => (!kind || g.kind === kind) && (!q || g.name.toLowerCase().includes(q)));
  $("#dupSummary").textContent = `${dupGroups.length} items you have more than once · ${dupGroups.reduce((a, g) => a + g.copies.length - 1, 0)} extra copies`;
  const out = $("#dupOut"); out.innerHTML = "";
  for (const g of groups) {
    const box = document.createElement("div"); box.className = "dup";
    box.innerHTML = `<div class="head">${g.art && S.art ? `<img src="${artUrl(g.art)}" alt="">` : ""}<div><b class="q-${esc(g.quality)}">${esc(g.name)}</b>
      <div class="small muted">${esc(g.base)} · ${g.kind} · ${g.copies.length} copies</div></div></div>`;
    const grid = document.createElement("div"); grid.className = "copies";
    for (const c of g.copies) {
      const d = document.createElement("div");
      d.className = "copy" + (dupPicked.has(c.key) ? " sel" : "") + (c.deletable ? "" : " locked");
      d.innerHTML = `<label class="check"><input type="checkbox" ${dupPicked.has(c.key) ? "checked" : ""} ${c.deletable ? "" : "disabled"}>
        <b>${c.deletable ? "Delete this copy" : "Equipped — can't delete"}</b></label>
        <div class="where">${esc(c.where)} · ilvl ${c.ilvl}${c.ethereal ? " · ethereal" : ""}</div>
        <div class="stats">${(c.stats || []).map(esc).join("<br>")}</div>`;
      if (c.deletable) d.addEventListener("click", e => {
        if (e.target.tagName !== "INPUT") d.querySelector("input").checked = !d.querySelector("input").checked;
        d.querySelector("input").checked ? dupPicked.add(c.key) : dupPicked.delete(c.key);
        d.classList.toggle("sel", dupPicked.has(c.key)); updateDupBar(g);
      });
      grid.appendChild(d);
    }
    box.appendChild(grid); out.appendChild(box);
  }
  updateDupBar();
}
function updateDupBar(group) {
  if (group && group.copies.every(c => dupPicked.has(c.key)))
    $("#dupSel").innerHTML = `<span style="color:var(--warn)">You've ticked every copy of ${esc(group.name)} — that deletes all of them.</span> `;
  else $("#dupSel").textContent = dupPicked.size ? `${dupPicked.size} item(s) selected` : "";
  $("#dupDelete").disabled = !dupPicked.size;
}
function renderEmpty() {
  $("#emptyTable").innerHTML = `<tr><th></th><th>Mule</th><th>Class</th><th>Level</th><th>Status</th></tr>` + emptyList.map(m =>
    `<tr><td><input type="checkbox" data-em="${esc(m.name)}" ${m.empty ? "" : "disabled"} ${emptyPicked.has(m.name) ? "checked" : ""}></td>
     <td>${esc(m.name)}</td><td>${esc(m.class)}</td><td>${m.level}</td>
     <td class="small">${m.empty ? '<span class="pill ok">empty</span>' : `<span class="muted">${esc(m.reason)}</span>`}</td></tr>`).join("");
  $$("[data-em]").forEach(cb => cb.onchange = () => { cb.checked ? emptyPicked.add(cb.dataset.em) : emptyPicked.delete(cb.dataset.em); updateEmptyBar(); });
  updateEmptyBar();
}
function updateEmptyBar() {
  const n = emptyList.filter(m => m.empty).length;
  $("#emptySel").textContent = `${n} empty mule(s)` + (emptyPicked.size ? ` · ${emptyPicked.size} selected` : "");
  $("#emptyDelete").disabled = !emptyPicked.size;
}
async function confirmDelete(planPath, body, what) {
  let p;
  try { p = await api(planPath, body); } catch (e) { modal(`<h2>Can't do that</h2><div class="notice bad">${esc(e.message)}</div><div class="row" style="justify-content:flex-end"><button class="btn" id="mOk">OK</button></div>`); $("#mOk").onclick = closeModal; return; }
  const list = p.deletions.map(d => `<li>${esc(d.name)} <span class="muted">— ${esc(d.where)}</span></li>`).concat(
    p.delete_chars.map(d => `<li><b>${esc(d.name)}</b> <span class="muted">(level ${d.level}, ${esc(d.file)} and its side files)</span></li>`)).join("");
  modal(`<h2>Delete ${what}?</h2><ul class="small" style="max-height:300px;overflow:auto">${list}</ul>
    <div class="notice">Close Diablo II: Resurrected completely first. Your whole save folder is backed up before anything is deleted, and <b>Backups → Undo</b> brings it all back.</div>
    <label class="check"><input type="checkbox" id="mSure"> I understand these will be deleted from my saves</label>
    <div class="row" style="justify-content:flex-end;margin-top:12px"><button class="btn" id="mCancel">Cancel</button><button class="btn danger" id="mGo" disabled>Delete</button></div>`);
  $("#mSure").onchange = () => { $("#mGo").disabled = !$("#mSure").checked; };
  $("#mCancel").onclick = closeModal;
  $("#mGo").onclick = async () => {
    $("#mGo").disabled = true; $("#mGo").textContent = "Deleting…";
    try {
      const r = await api("/api/apply", { plan_id: p.id });
      modal(`<h2>Done ✔</h2><p>${esc(r.log_lines.at(-1) || "")}</p><p class="small">Backup: <span class="mono">${esc(r.backup)}</span></p><div class="row" style="justify-content:flex-end"><button class="btn primary" id="mOk">OK</button></div>`);
      dupPicked.clear(); emptyPicked.clear(); allItems = null; grail = null;
      await refresh();
    } catch (e) {
      modal(`<h2>Nothing was changed</h2><div class="notice bad">${esc(e.message)}</div><div class="row" style="justify-content:flex-end"><button class="btn" id="mOk">OK</button></div>`);
    }
    $("#mOk").onclick = closeModal;
  };
}
$("#dKind").onchange = renderDupes; $("#dq").oninput = renderDupes;
$("#dupClear").onclick = () => { dupPicked.clear(); renderDupes(); };
$("#dupDelete").onclick = () => confirmDelete("/api/plan_delete_items", { keys: [...dupPicked] }, `${dupPicked.size} item(s)`);
$("#emptyDelete").onclick = () => confirmDelete("/api/plan_delete_mules", { names: [...emptyPicked] }, `${emptyPicked.size} mule(s)`);

// ---------------------------------------------------------------- sorting rules
async function loadRules() { rulesState = await api("/api/rules"); renderRules(); }
function renderRules(msg) {
  const r = rulesState.rules;
  $("#rulesPath").textContent = rulesState.path + (rulesState.custom ? "" : " (using defaults)");
  $("#rulesHelp").textContent = r._help || rulesState.defaults._help || "";
  $("#rulesJson").value = JSON.stringify(r, null, 2);
  $("#rulesTable").innerHTML = `<tr><th>On</th><th>Order</th><th>Category</th><th>Mule name start</th><th>Conditions</th></tr>` +
    r.categories.map((c, i) => `<tr><td><input type="checkbox" data-on="${i}" ${c.enabled === false ? "" : "checked"}></td>
      <td style="white-space:nowrap"><button class="btn small" data-up="${i}" ${i ? "" : "disabled"}>▲</button> <button class="btn small" data-down="${i}" ${i < r.categories.length - 1 ? "" : "disabled"}>▼</button></td>
      <td><input type="text" data-label="${i}" value="${esc(c.label || c.key)}"><div class="rules-key">${esc(c.key)}${c.sort ? " · sort by " + esc(c.sort) : ""}${c.share_with ? " · overflow → " + esc(c.share_with.join(", ")) : ""}</div></td>
      <td><input type="text" data-stem="${i}" value="${esc(c.stem || "")}" maxlength="13"></td>
      <td class="small mono">${esc(JSON.stringify(c.match))}</td></tr>`).join("");
  $$("[data-on]").forEach(el => el.onchange = () => { r.categories[+el.dataset.on].enabled = el.checked; });
  $$("[data-label]").forEach(el => el.oninput = () => { r.categories[+el.dataset.label].label = el.value; });
  $$("[data-stem]").forEach(el => el.oninput = () => { r.categories[+el.dataset.stem].stem = el.value; });
  const swap = (i, j) => { const c = r.categories; [c[i], c[j]] = [c[j], c[i]]; renderRules(); };
  $$("[data-up]").forEach(el => el.onclick = () => swap(+el.dataset.up, +el.dataset.up - 1));
  $$("[data-down]").forEach(el => el.onclick = () => swap(+el.dataset.down, +el.dataset.down + 1));
  $("#rulesMsg").innerHTML = msg || "";
}

// ---------------------------------------------------------------- backups
async function loadBackups() {
  const list = await api("/api/backups");
  $("#backupDir").textContent = S.backup_dir;
  $("#backupTable").innerHTML = `<tr><th>Taken</th><th>What happened next</th><th>Size</th><th></th></tr>` + (list.length ? list.map(b =>
    `<tr><td class="small">${esc(b.time.replace("T", " "))}<div class="mono">${esc(b.file)}</div></td><td class="small">${esc(b.summary || "—")}${b.log && !b.can_undo ? `<div class="muted">${esc(b.undo_reason)}</div>` : ""}</td>
     <td class="small">${(b.size / 1024).toFixed(0)} KB</td>
     <td style="white-space:nowrap">${b.can_undo ? `<button class="btn" data-u="${esc(b.log)}">Undo</button> ` : ""}<button class="btn" data-r="${esc(b.file)}">Restore…</button></td></tr>`).join("")
    : `<tr><td colspan="4" class="muted">No backups yet — one is made automatically the first time you apply a plan.</td></tr>`);
  const confirm = (title, text, go) => {
    modal(`<h2>${title}</h2><p>${text}</p><div class="notice">Close Diablo II: Resurrected completely first.</div>
      <div class="row" style="justify-content:flex-end"><button class="btn" id="mCancel">Cancel</button><button class="btn danger" id="mGo">${title.split(" ")[0]}</button></div>`);
    $("#mCancel").onclick = closeModal;
    $("#mGo").onclick = async () => {
      try { await go(); modal(`<h2>Done ✔</h2><div class="row" style="justify-content:flex-end"><button class="btn primary" id="mOk">OK</button></div>`); allItems = null; grail = null; await refresh(); }
      catch (e) { modal(`<h2>Nothing was changed</h2><div class="notice bad">${esc(e.message)}</div><div class="row" style="justify-content:flex-end"><button class="btn" id="mOk">OK</button></div>`); }
      $("#mOk").onclick = closeModal;
    };
  };
  $$("[data-u]").forEach(b => b.onclick = () => confirm("Undo this change?", "The files it changed are put back and anything it created is removed. The current state is backed up first.", () => api("/api/undo", { log: b.dataset.u })));
  $$("[data-r]").forEach(b => b.onclick = () => confirm(`Restore ${esc(b.dataset.r)}?`, "Your whole save folder goes back to exactly how it was in this backup (anything since is lost, but backed up first).", () => api("/api/restore", { file: b.dataset.r })));
}

// ---------------------------------------------------------------- wiring
function renderBanner() {
  const b = $("#banner");
  if (S.journal) {
    b.innerHTML = `<div class="notice bad"><b>A previous change was interrupted.</b> Your saves may be half-updated. Roll back to the backup taken just before it: <button class="btn" id="recoverBtn">Roll back now</button></div>`;
    $("#recoverBtn").onclick = async () => { await api("/api/recover", {}); await refresh(); };
  } else if (S.errors.length) {
    b.innerHTML = `<div class="notice bad">Some files could not be read and will be left alone:<br>${S.errors.map(e => esc(e[0] + ": " + e[1])).join("<br>")}</div>`;
  } else b.innerHTML = "";
}
async function refresh() {
  S = await api("/api/state");
  $("#gameStatus").innerHTML = S.game_running ? `<span class="dot" style="background:var(--danger)"></span>Game is running — close it before applying` : `<span class="dot" style="background:var(--ok)"></span>Game closed`;
  const sel = $("#stashSel"), prev = sel.value || store.get("stash", S.default_stash);
  sel.innerHTML = S.stashes.map(s => `<option value="${esc(s.file)}">${esc(s.file)}${s.modern ? " (RotW)" : ""} — ${s.items} items</option>`).join("");
  sel.value = S.stashes.some(s => s.file === prev) ? prev : S.default_stash;
  if (!$("#crafter").value) $("#crafter").value = store.get("crafter", S.crafter_level || 99);
  muleSel = store.get("muleSel", null);
  if (!muleSel || Object.keys(muleSel).some(n => !S.characters.find(c => c.name === n))) muleSel = defaultMules();
  const def = defaultMules();
  for (const c of S.characters) if (!(c.name in muleSel)) muleSel[c.name] = def[c.name];
  const keepSel = new Set(store.get("keepCats", []));
  $("#keepCats").innerHTML = S.category_order.map(k => `<label><input type="checkbox" value="${k}" ${keepSel.has(k) ? "checked" : ""}>${esc(S.categories[k])}</label>`).join("");
  const keepSum = () => { const c = $$("#keepCats input:checked"); $("#keepSummary").textContent = c.length ? c.map(i => S.categories[i.value]).join(", ") : "Nothing — move everything"; store.set("keepCats", c.map(i => i.value)); };
  $$("#keepCats input").forEach(i => i.onchange = keepSum); keepSum();
  $("#fc").innerHTML = `<option value="">Any</option>` + S.category_order.map(k => `<option value="${k}">${esc(S.categories[k])}</option>`).join("");
  renderBanner(); renderOverview();
  await loadStash();
  const active = $("nav button.active").dataset.s;
  if (active !== "overview" && active !== "stash") await show(active);
}
async function show(name) {
  const b = $(`#nav button[data-s="${name}"]`) || $("#nav button");
  $$("#nav button").forEach(x => x.classList.toggle("active", x === b));
  $$("main section").forEach(s => s.classList.toggle("active", s.id === b.dataset.s));
  if (location.hash.slice(1).split("-")[0] !== b.dataset.s) history.replaceState(null, "", "#" + b.dataset.s);
  hideTip();
  const loaders = { assess: loadAssess, backups: loadBackups, find: loadFind, grail: loadGrail, rules: loadRules, cleanup: loadCleanup };
  if (loaders[b.dataset.s]) await loaders[b.dataset.s]();
}
$$("#nav button").forEach(b => b.addEventListener("click", () => show(b.dataset.s)));
$("#reloadBtn").onclick = async () => { await api("/api/reload", {}); curPlan = null; allItems = null; grail = null; $("#planOut").innerHTML = ""; await refresh(); };
$("#planBtn").onclick = preview;
$("#stashSel").onchange = () => { store.set("stash", $("#stashSel").value); stashTab = 0; renderOverview(); loadStash(); };
$("#muleLevel").onchange = $("#muleHint").onchange = () => { muleSel = defaultMules(); store.set("muleSel", muleSel); renderOverview(); };
$("#resetMules").onclick = () => { muleSel = defaultMules(); store.set("muleSel", muleSel); renderOverview(); };
$("#crafter").onchange = () => { store.set("crafter", +$("#crafter").value); allItems = null; loadAssess(); };
$("#scope").onchange = loadAssess;
["#kind", "#minTier", "#keepOnly"].forEach(s => $(s).onchange = renderAssess);
$("#search").oninput = renderAssess;
$("#q").oninput = renderFind; $("#fq").onchange = $("#fc").onchange = $("#fEquipped").onchange = renderFind;
$("#fq").innerHTML = `<option value="">Any</option>` + QUALITIES.map(q => `<option value="${q}">${q}</option>`).join("");
$$("#grailTabs button").forEach(b => b.onclick = () => { grailKind = b.dataset.g; $$("#grailTabs button").forEach(x => x.classList.toggle("active", x === b)); renderGrail(); });
$("#gShow").onchange = renderGrail; $("#gq").oninput = renderGrail;
$("#rulesSave").onclick = async () => {
  try { rulesState = await api("/api/rules", { rules: rulesState.rules }); allItems = null; renderRules(`<span style="color:var(--ok)">Saved ✔</span>`); await refresh(); }
  catch (e) { renderRules(`<span style="color:var(--danger)">${esc(e.message)}</span>`); }
};
$("#rulesReset").onclick = async () => { rulesState = await api("/api/rules/reset", {}); allItems = null; renderRules(`<span style="color:var(--ok)">Back to defaults ✔</span>`); await refresh(); };
$("#rulesJsonApply").onclick = () => {
  try { rulesState.rules = JSON.parse($("#rulesJson").value); renderRules(`<span class="muted">Loaded — press Save rules to keep it.</span>`); }
  catch (e) { $("#rulesMsg").innerHTML = `<span style="color:var(--danger)">Not valid JSON: ${esc(e.message)}</span>`; }
};
$("#modal").addEventListener("click", e => { if (e.target.id === "modal") closeModal(); });
refresh().then(async () => {
  const [sec, action, mode] = location.hash.slice(1).split("-");
  if (sec) await show(sec);
  if (sec === "sort" && action === "preview") {  // previews never change anything
    const radio = mode && $(`input[name=mode][value="${mode}"]`);
    if (radio) radio.checked = true;
    await preview();
  }
}).catch(e => { $("#banner").innerHTML = `<div class="notice bad">${esc(e.message)}</div>`; });
