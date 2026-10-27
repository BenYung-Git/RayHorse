/**
 * Racing 手機介面：底欄切換功能，卡片式顯示資料（共用 /api/app/*）。
 */
(function () {
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const state = {
    panel: "home",
    racecard: null,
    raceNo: null,
    results: null,
    footballScope: "hk",
    aiBusy: false,
  };

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setStatus(msg) {
    const el = $("#mStatus");
    if (!el) return;
    const text = msg || "";
    el.textContent = text;
    el.style.display = text ? "block" : "none";
  }

  function showPanel(name) {
    state.panel = name;
    $$(".m-panel").forEach((p) => p.classList.toggle("active", p.id === `panel-${name}`));
    $$(".m-nav button").forEach((b) =>
      b.classList.toggle("active", b.dataset.panel === name)
    );
    const titles = {
      home: "功能選單",
      racecard: "排位",
      results: "賽馬結果",
      horses: "馬匹／往績",
      more: "更多",
    };
    const t = $("#mTitle");
    if (t) t.textContent = titles[name] || "Racing";
    if (name === "racecard") loadRacecard();
    if (name === "results") loadResults();
    if (name === "horses" && !$("#horseList").dataset.loaded) loadHorses();
    if (name === "more") {
      const fb = $("#moreFootball");
      const ai = $("#moreAi");
      const hint = $("#moreHint");
      const showing = (fb && !fb.hidden) || (ai && !ai.hidden);
      if (hint) hint.hidden = !!showing;
    }
  }

  async function loadRacecard(opts) {
    const wrap = $("#raceList");
    const chips = $("#raceChips");
    const meeting = $("#racecardMeeting");
    setStatus("載入排位…");
    try {
      let q = "";
      if (opts && opts.date) {
        q = `?date=${encodeURIComponent(opts.date)}&course=${encodeURIComponent(opts.course || "")}`;
      } else if (meeting && meeting.value) {
        const [d, c] = meeting.value.split("|");
        q = `?date=${encodeURIComponent(d)}&course=${encodeURIComponent(c || "")}`;
      }
      const res = await fetch("/api/app/racecard" + q);
      const data = await res.json();
      state.racecard = data;
      const meetings = data.meetings || [];
      if (meeting && (opts && opts.refreshMeetings || meeting.options.length === 0)) {
        meeting.innerHTML = meetings
          .map((m) => {
            const v = `${m.race_date}|${m.racecourse}`;
            const cur = `${data.race_date}|${data.racecourse_code}`;
            return `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(m.label)}</option>`;
          })
          .join("");
      }
      const races = data.races || [];
      chips.innerHTML = races
        .map((r, i) => {
          const no = r.race_no;
          const active = String(state.raceNo || (races[0] && races[0].race_no)) === String(no);
          return `<button type="button" class="m-chip ${active ? "active" : ""}" data-race="${esc(no)}">第${esc(no)}場</button>`;
        })
        .join("");
      chips.querySelectorAll(".m-chip").forEach((b) =>
        b.addEventListener("click", () => {
          state.raceNo = b.dataset.race;
          renderRacecardRace();
          chips.querySelectorAll(".m-chip").forEach((x) =>
            x.classList.toggle("active", x === b)
          );
        })
      );
      if (!state.raceNo && races[0]) state.raceNo = races[0].race_no;
      renderRacecardRace();
      $("#racecardMeta").textContent =
        `${data.race_date || ""} ${data.racecourse || data.racecourse_code || ""} · 更新 ${data.last_update || "—"}`;
      setStatus("");
    } catch (err) {
      wrap.innerHTML = `<div class="m-empty">排位載入失敗：${esc(err)}</div>`;
      setStatus("載入失敗");
    }
  }

  function renderRacecardRace() {
    const data = state.racecard;
    const wrap = $("#raceList");
    if (!data) return;
    const race = (data.races || []).find((r) => String(r.race_no) === String(state.raceNo));
    if (!race) {
      wrap.innerHTML = `<div class="m-empty">沒有排位資料。請先在桌面／WEB 下載排位。</div>`;
      return;
    }
    const cols = data.columns || [];
    const nameIdx = cols.findIndex((c) => c === "馬名" || c === "馬匹名稱");
    const noIdx = cols.findIndex((c) => c === "馬號");
    const jIdx = cols.findIndex((c) => c === "騎師");
    const tIdx = cols.findIndex((c) => c === "練馬師");
    const drawIdx = cols.findIndex((c) => c === "檔位");
    const liveIdx = cols.findIndex((c) => c === "即時賠率");
    const predIdx = cols.findIndex((c) => c === "預測賠率");
    const rows = race.rows || [];
    if (!rows.length) {
      wrap.innerHTML = `<div class="m-empty">此場暫無出馬表。</div>`;
      return;
    }
    wrap.innerHTML = rows
      .map((row) => {
        const name = nameIdx >= 0 ? row[cols[nameIdx]] : "";
        const no = noIdx >= 0 ? row[cols[noIdx]] : "";
        const jockey = jIdx >= 0 ? row[cols[jIdx]] : "";
        const trainer = tIdx >= 0 ? row[cols[tIdx]] : "";
        const draw = drawIdx >= 0 ? row[cols[drawIdx]] : "";
        const live = liveIdx >= 0 ? row[cols[liveIdx]] : "";
        const pred = predIdx >= 0 ? row[cols[predIdx]] : "";
        return `<article class="m-card">
          <div class="m-card-hd">
            <strong>${esc(name || "—")}</strong>
            <span class="m-badge">${esc(no || "—")}</span>
          </div>
          <dl class="m-kv">
            <dt>檔位</dt><dd>${esc(draw || "—")}</dd>
            <dt>騎師</dt><dd>${esc(jockey || "—")}</dd>
            <dt>練馬師</dt><dd>${esc(trainer || "—")}</dd>
            <dt>即時賠率</dt><dd>${esc(live || "—")}</dd>
            <dt>預測賠率</dt><dd>${esc(pred || "—")}</dd>
          </dl>
        </article>`;
      })
      .join("");
  }

  async function loadResults() {
    const wrap = $("#resultsList");
    const meeting = $("#resultsMeeting");
    const raceSel = $("#resultsRace");
    setStatus("載入賽果…");
    try {
      let q = "";
      if (meeting && meeting.value) {
        const [d, c] = meeting.value.split("|");
        q = `?date=${encodeURIComponent(d)}&course=${encodeURIComponent(c || "")}`;
      }
      if (raceSel && raceSel.value && raceSel.value !== "all") {
        q += (q ? "&" : "?") + "race=" + encodeURIComponent(raceSel.value);
      }
      const res = await fetch("/api/app/results" + q);
      const data = await res.json();
      state.results = data;
      const meetings = data.meetings || [];
      if (meeting && meeting.options.length === 0) {
        meeting.innerHTML = meetings
          .map((m) => {
            const v = `${m.race_date}|${m.racecourse}`;
            const cur = `${data.race_date}|${data.racecourse_code}`;
            return `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(m.label)}</option>`;
          })
          .join("");
      }
      const nos = data.race_nos || [];
      const keep = raceSel.value || "all";
      raceSel.innerHTML =
        `<option value="all">全部場次</option>` +
        nos
          .map((n) => `<option value="${esc(n)}" ${String(keep) === String(n) ? "selected" : ""}>第${esc(n)}場</option>`)
          .join("");
      const rows = data.rows || [];
      $("#resultsMeta").textContent =
        `${data.race_date || ""} ${data.racecourse || ""} · ${rows.length} 筆`;
      if (!rows.length) {
        wrap.innerHTML = `<div class="m-empty">沒有賽果。請先下載賽馬結果。</div>`;
      } else {
        wrap.innerHTML = rows
          .map((r) => {
            const pos = r.finish_pos || "—";
            return `<article class="m-card">
              <div class="m-card-hd">
                <strong>${esc(r.name_zh || r.horse_id || "—")}</strong>
                <span class="m-badge ${String(pos) === "01" || String(pos) === "1" ? "warn" : ""}">${esc(pos)}</span>
              </div>
              <dl class="m-kv">
                <dt>場次</dt><dd>${esc(r.race_no || "—")}</dd>
                <dt>馬號</dt><dd>${esc(r.horse_no || "—")}</dd>
                <dt>騎師</dt><dd>${esc(r.jockey || "—")}</dd>
                <dt>練馬師</dt><dd>${esc(r.trainer || "—")}</dd>
                <dt>獨贏</dt><dd>${esc(r.win_odds || "—")}</dd>
                <dt>時間</dt><dd>${esc(r.time || "—")}</dd>
              </dl>
            </article>`;
          })
          .join("");
      }
      setStatus("");
    } catch (err) {
      wrap.innerHTML = `<div class="m-empty">賽果載入失敗：${esc(err)}</div>`;
      setStatus("載入失敗");
    }
  }

  async function loadHorses() {
    const wrap = $("#horseList");
    const q = ($("#horseQ") && $("#horseQ").value.trim()) || "";
    setStatus("搜尋馬匹…");
    try {
      const res = await fetch("/api/app/horses?q=" + encodeURIComponent(q));
      const data = await res.json();
      const rows = (data.rows || []).slice(0, 80);
      wrap.dataset.loaded = "1";
      $("#horseMeta").textContent = `顯示 ${rows.length} / ${(data.rows || []).length} 匹`;
      if (!rows.length) {
        wrap.innerHTML = `<div class="m-empty">找不到馬匹。</div>`;
      } else {
        wrap.innerHTML = rows
          .map((r) => {
            return `<article class="m-card" data-hid="${esc(r.horse_id)}">
              <div class="m-card-hd">
                <strong>${esc(r.name_zh || r.name_en || r.horse_id)}</strong>
                <span class="m-badge">${esc((r.status || "").slice(0, 4) || "—")}</span>
              </div>
              <dl class="m-kv">
                <dt>HorseId</dt><dd>${esc(r.horse_id || "—")}</dd>
                <dt>烙號</dt><dd>${esc(r.brand_no || "—")}</dd>
                <dt>英文名</dt><dd>${esc(r.name_en || "—")}</dd>
              </dl>
            </article>`;
          })
          .join("");
        wrap.querySelectorAll(".m-card[data-hid]").forEach((card) => {
          card.addEventListener("click", () => openHorseDetail(card.dataset.hid));
        });
      }
      setStatus("");
    } catch (err) {
      wrap.innerHTML = `<div class="m-empty">馬匹載入失敗：${esc(err)}</div>`;
      setStatus("載入失敗");
    }
  }

  async function openHorseDetail(hid) {
    if (!hid) return;
    setStatus("載入往績…");
    try {
      const res = await fetch("/api/app/horse/" + encodeURIComponent(hid));
      const data = await res.json();
      const horse = data.horse || {};
      const records = (data.races || []).slice(0, 12);
      const name = horse.name_zh || horse.name_en || hid;
      const box = $("#horseDetail");
      box.hidden = false;
      box.innerHTML = `<div class="m-card">
        <div class="m-card-hd"><strong>${esc(name)}</strong><span class="m-badge">往績</span></div>
        <p class="m-meta" style="color:var(--m-muted);margin:0 0 8px">${esc(horse.horse_id || "")} · ${esc(horse.brand_no || "")}</p>
        <div class="m-list" style="gap:8px;margin-top:8px">
          ${
            records.length
              ? records
                  .map((r) => {
                    return `<div style="border-top:1px solid var(--m-line);padding-top:6px">
                      <strong>${esc(r["日期"] || "")}</strong>
                      · 第${esc(r["場次"] || "")}場
                      · 名次 ${esc(r["名次"] || "—")}
                      · ${esc(r["騎師"] || "")}
                      · 獨贏 ${esc(r["獨贏賠率"] || "—")}
                    </div>`;
                  })
                  .join("")
              : "<div class='m-empty' style='background:#eef6f8;color:#355'>暫無往績</div>"
          }
        </div>
      </div>`;
      box.scrollIntoView({ behavior: "smooth", block: "start" });
      setStatus("");
    } catch (err) {
      setStatus("往績載入失敗");
      alert("往績載入失敗：" + err);
    }
  }

  async function loadFootball(scope) {
    state.footballScope = scope || state.footballScope || "hk";
    const wrap = $("#footballList");
    const meeting = $("#footballMeeting");
    setStatus("載入足球…");
    try {
      let q = `?scope=${encodeURIComponent(state.footballScope)}`;
      if (meeting && meeting.value) q += `&date=${encodeURIComponent(meeting.value)}`;
      const res = await fetch("/api/app/football" + q);
      const data = await res.json();
      const meetings = data.meetings || [];
      if (meeting) {
        meeting.innerHTML = meetings
          .map(
            (m) =>
              `<option value="${esc(m.race_date)}" ${m.race_date === data.race_date ? "selected" : ""}>${esc(m.label || m.race_date)}</option>`
          )
          .join("");
      }
      const rows = data.display_rows || data.epl_rows || [];
      $("#footballMeta").textContent =
        `${state.footballScope === "epl" ? "英超" : "香港足球"} · ${data.race_date || "—"} · ${rows.length} 場`;
      if (!rows.length) {
        wrap.innerHTML = `<div class="m-empty">沒有足球資料。請先下載足球賠率／賽果。</div>`;
      } else {
        wrap.innerHTML = rows
          .map((r) => {
            return `<article class="m-card">
              <div class="m-card-hd">
                <strong>${esc(r.home_team || "")} vs ${esc(r.away_team || "")}</strong>
                <span class="m-badge">${esc(r.ft_score || "vs")}</span>
              </div>
              <dl class="m-kv">
                <dt>聯賽</dt><dd>${esc(r.league || "—")}</dd>
                <dt>時間</dt><dd>${esc(r.kick_off_time || "—")}</dd>
                <dt>半場</dt><dd>${esc(r.ht_score || "—")}</dd>
                <dt>主／和／客</dt><dd>${esc(r.odds_home || "—")} / ${esc(r.odds_draw || "—")} / ${esc(r.odds_away || "—")}</dd>
              </dl>
            </article>`;
          })
          .join("");
      }
      setStatus("");
    } catch (err) {
      wrap.innerHTML = `<div class="m-empty">足球載入失敗：${esc(err)}</div>`;
      setStatus("載入失敗");
    }
  }

  async function sendAi() {
    if (state.aiBusy) return;
    const input = $("#aiQ");
    const q = (input.value || "").trim();
    if (!q) return;
    state.aiBusy = true;
    const log = $("#aiLog");
    log.insertAdjacentHTML("beforeend", `<div class="m-bubble user">${esc(q)}</div>`);
    input.value = "";
    setStatus("AI 處理中…");
    try {
      const res = await fetch("/api/app/ai", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q }),
      });
      const data = await res.json();
      log.insertAdjacentHTML(
        "beforeend",
        `<div class="m-bubble bot">${esc(data.text || "（無回覆）")}</div>`
      );
      const rows = data.rows || [];
      const cols = data.columns || [];
      if (rows.length && cols.length) {
        const preview = rows.slice(0, 30);
        $("#aiTable").innerHTML =
          `<div class="m-meta" style="color:var(--m-muted)">報表 ${rows.length} 列（手機預覽前 ${preview.length}）</div>` +
          preview
            .map((row) => {
              const title = row[0] || row[1] || "列";
              const bits = cols
                .slice(0, 6)
                .map((c, i) => `<dt>${esc(c)}</dt><dd>${esc(row[i] ?? "")}</dd>`)
                .join("");
              return `<article class="m-card"><div class="m-card-hd"><strong>${esc(title)}</strong></div><dl class="m-kv">${bits}</dl></article>`;
            })
            .join("");
      } else {
        $("#aiTable").innerHTML = "";
      }
      log.scrollTop = log.scrollHeight;
      setStatus("");
    } catch (err) {
      log.insertAdjacentHTML("beforeend", `<div class="m-bubble bot">錯誤：${esc(err)}</div>`);
      setStatus("AI 失敗");
    } finally {
      state.aiBusy = false;
    }
  }

  function bind() {
    $$(".m-nav button").forEach((b) =>
      b.addEventListener("click", () => showPanel(b.dataset.panel))
    );
    $$("[data-go]").forEach((b) =>
      b.addEventListener("click", () => {
        const go = b.dataset.go;
        if (go === "football_hk") {
          showPanel("more");
          $("#moreFootball").hidden = false;
          $("#moreAi").hidden = true;
          const hint = $("#moreHint");
          if (hint) hint.hidden = true;
          loadFootball("hk");
          return;
        }
        if (go === "football_epl") {
          showPanel("more");
          $("#moreFootball").hidden = false;
          $("#moreAi").hidden = true;
          const hint = $("#moreHint");
          if (hint) hint.hidden = true;
          loadFootball("epl");
          return;
        }
        if (go === "ai") {
          showPanel("more");
          $("#moreFootball").hidden = true;
          $("#moreAi").hidden = false;
          const hint = $("#moreHint");
          if (hint) hint.hidden = true;
          return;
        }
        showPanel(go);
      })
    );
    $("#racecardMeeting")?.addEventListener("change", () => {
      state.raceNo = null;
      loadRacecard();
    });
    $("#btnReloadRacecard")?.addEventListener("click", () =>
      loadRacecard({ refreshMeetings: true })
    );
    $("#resultsMeeting")?.addEventListener("change", () => {
      $("#resultsRace").value = "all";
      loadResults();
    });
    $("#resultsRace")?.addEventListener("change", () => loadResults());
    $("#btnReloadResults")?.addEventListener("click", () => loadResults());
    $("#btnSearchHorse")?.addEventListener("click", () => loadHorses());
    $("#horseQ")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        loadHorses();
      }
    });
    $("#footballMeeting")?.addEventListener("change", () => loadFootball());
    $("#btnFbHk")?.addEventListener("click", () => loadFootball("hk"));
    $("#btnFbEpl")?.addEventListener("click", () => loadFootball("epl"));
    $("#btnAiSend")?.addEventListener("click", () => sendAi());
  }

  document.addEventListener("DOMContentLoaded", () => {
    bind();
    showPanel("home");
  });
})();
