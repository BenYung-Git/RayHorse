/**
 * WEB Grid 鍵盤導覽（對齊 Windows grid_nav.py）：
 * - Enter → 下一欄（不進入編輯）
 * - 方向鍵 → 上下左右（移格後結束編輯）
 * - 雙擊／一般字元鍵 → 進入編輯（editable 時）
 * - 移格後派發 hppv:rowchange，供紀錄導覽同步明細
 */
(function () {
  function cellsOf(table) {
    // 略過補齊空白列（grid-pad），避免鍵盤導覽進入佔位列
    return Array.from(table.querySelectorAll("tbody tr:not(.grid-pad)")).map((tr) =>
      Array.from(tr.querySelectorAll("td"))
    );
  }

  function emitRowChange(table, td) {
    const tr = td && td.parentElement;
    if (!tr || !table) return;
    table.dispatchEvent(
      new CustomEvent("hppv:rowchange", {
        bubbles: true,
        detail: {
          table,
          tr,
          td,
          rowIndex: tr.rowIndex,
          id: tr.dataset.id || tr.dataset.horse || "",
        },
      })
    );
  }

  function focusCell(td, opts) {
    if (!td) return;
    const table = td.closest("table");
    const prevTr = table.querySelector("tr.grid-selected");
    table.querySelectorAll("td.grid-current").forEach((el) => el.classList.remove("grid-current"));
    table.querySelectorAll("tr.grid-selected").forEach((el) => el.classList.remove("grid-selected"));
    td.classList.add("grid-current");
    const tr = td.parentElement;
    if (tr) tr.classList.add("grid-selected");
    td.setAttribute("tabindex", "0");
    const wantEdit = !!(opts && opts.edit && table.dataset.gridEditable !== "0");
    if (wantEdit) {
      td.contentEditable = "true";
      td.focus();
      const range = document.createRange();
      range.selectNodeContents(td);
      range.collapse(false);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    } else {
      if (td.isContentEditable) td.contentEditable = "false";
      td.focus({ preventScroll: false });
    }
    td.scrollIntoView({ block: "nearest", inline: "nearest" });
    if (!prevTr || prevTr !== tr) emitRowChange(table, td);
  }

  function locate(table) {
    const grid = cellsOf(table);
    if (!grid.length) return null;
    let r = 0;
    let c = 0;
    const cur = table.querySelector("td.grid-current");
    if (cur) {
      outer: for (let i = 0; i < grid.length; i++) {
        for (let j = 0; j < grid[i].length; j++) {
          if (grid[i][j] === cur) {
            r = i;
            c = j;
            break outer;
          }
        }
      }
      if (cur.isContentEditable) cur.contentEditable = "false";
    }
    return { grid, r, c };
  }

  function move(table, dRow, dCol) {
    const loc = locate(table);
    if (!loc) return;
    const { grid, r, c } = loc;
    const nr = Math.max(0, Math.min(grid.length - 1, r + dRow));
    const nc = Math.max(0, Math.min(grid[nr].length - 1, c + dCol));
    focusCell(grid[nr][nc], { edit: false });
  }

  function onEnter(table) {
    const loc = locate(table);
    if (!loc) return;
    const { grid, r, c } = loc;
    if (c + 1 < grid[r].length) focusCell(grid[r][c + 1], { edit: false });
    else if (r + 1 < grid.length) focusCell(grid[r + 1][0], { edit: false });
  }

  function bindTable(table, opts) {
    if (!table) return;
    table.classList.add("hppv-grid");
    if (opts && opts.editable === false) table.dataset.gridEditable = "0";
    else if (!table.dataset.gridEditable) table.dataset.gridEditable = "1";

    if (table.dataset.gridNavBound !== "1") {
      table.dataset.gridNavBound = "1";
      table.setAttribute("tabindex", "0");
      table.addEventListener("click", (ev) => {
        const td = ev.target.closest("td");
        if (td && table.contains(td) && !td.closest("tr.grid-pad")) {
          focusCell(td, { edit: false });
        }
      });
      table.addEventListener("dblclick", (ev) => {
        const td = ev.target.closest("td");
        if (
          td &&
          table.contains(td) &&
          !td.closest("tr.grid-pad") &&
          table.dataset.gridEditable !== "0"
        ) {
          focusCell(td, { edit: true });
        }
      });
      table.addEventListener("keydown", (ev) => {
        const key = ev.key;
        if (key === "Enter") {
          ev.preventDefault();
          onEnter(table);
        } else if (key === "ArrowLeft") {
          ev.preventDefault();
          move(table, 0, -1);
        } else if (key === "ArrowRight") {
          ev.preventDefault();
          move(table, 0, 1);
        } else if (key === "ArrowUp") {
          ev.preventDefault();
          move(table, -1, 0);
        } else if (key === "ArrowDown") {
          ev.preventDefault();
          move(table, 1, 0);
        } else if (
          table.dataset.gridEditable !== "0" &&
          key.length === 1 &&
          !ev.ctrlKey &&
          !ev.metaKey &&
          !ev.altKey
        ) {
          const cur = table.querySelector("td.grid-current");
          if (cur) focusCell(cur, { edit: true });
        }
      });
    }

    // 重綁時保留目前格；opts.focus===false 時不自動聚焦（由呼叫端選列）
    if (opts && opts.focus === false) return;
    let cur = table.querySelector("tbody tr:not(.grid-pad) td.grid-current");
    if (!cur) {
      const selTr = table.querySelector("tbody tr.grid-selected:not(.grid-pad)");
      cur =
        (selTr && selTr.querySelector("td")) ||
        table.querySelector("tbody tr:not(.grid-pad) td");
      if (cur) focusCell(cur, { edit: false });
    }
  }

  window.HppvGridNav = {
    bind: bindTable,
    focusCell: focusCell,
    bindAll: function (root) {
      (root || document)
        .querySelectorAll("table[data-grid-nav], table.hppv-grid")
        .forEach((t) => {
          // data-no-grid-nav：自行處理編輯／儲存（例如網站設定 GRID）
          if (t.hasAttribute("data-no-grid-nav") || t.dataset.gridNav === "off") return;
          bindTable(t);
        });
    },
  };

  /* ── 多欄排序：點表頭＝主鍵；Shift+點＝疊加次要欄（最多 4）── */
  const MULTI_SORT_MAX = 4;
  const SORT_ARROW_RE = /\s*[↑↓]\d*\s*$/;

  function sortBaseLabel(th) {
    if (!th) return "";
    if (th.dataset.label) return th.dataset.label;
    const raw = String(th.childNodes[0] ? th.childNodes[0].textContent : th.textContent || "");
    const base = raw.replace(SORT_ARROW_RE, "").trim();
    th.dataset.label = base;
    return base;
  }

  function cellSortKey(text) {
    const s = String(text || "").trim().replace(/,/g, "");
    if (!s) return { empty: 1, num: 0, n: 0, s: "" };
    if (/^-?\d+(\.\d+)?$/.test(s)) return { empty: 0, num: 0, n: parseFloat(s), s: "" };
    return { empty: 0, num: 1, n: 0, s: s.toLowerCase() };
  }

  function cmpKeys(a, b) {
    if (a.empty !== b.empty) return a.empty - b.empty;
    if (a.num !== b.num) return a.num - b.num;
    if (a.num === 0) return a.n === b.n ? 0 : a.n < b.n ? -1 : 1;
    if (a.s < b.s) return -1;
    if (a.s > b.s) return 1;
    return 0;
  }

  function refreshSortHeaders(table) {
    const keys = table._hppvSortKeys || [];
    const ths = table.querySelectorAll("thead th");
    ths.forEach((th, col) => {
      const base = sortBaseLabel(th);
      th.classList.remove("sorted-asc", "sorted-desc");
      let label = base;
      const idx = keys.findIndex((k) => k.col === col);
      if (idx >= 0) {
        const asc = keys[idx].asc;
        label = `${base} ${asc ? "↑" : "↓"}${idx + 1}`;
        th.classList.add(asc ? "sorted-asc" : "sorted-desc");
      }
      // 保留欄寬拖曳把手
      const grip = th.querySelector(".col-resizer");
      if (grip) {
        th.childNodes.forEach((n) => {
          if (n !== grip) th.removeChild(n);
        });
        th.insertBefore(document.createTextNode(label), grip);
      } else {
        th.textContent = label;
        th.dataset.label = base;
      }
    });
  }

  function applyTableSort(table) {
    const keys = table._hppvSortKeys || [];
    const tb = table.tBodies[0];
    if (!tb || !keys.length) {
      refreshSortHeaders(table);
      return;
    }
    const dataRows = Array.from(tb.querySelectorAll("tr:not(.grid-pad)"));
    const padRows = Array.from(tb.querySelectorAll("tr.grid-pad"));
    dataRows.sort((ra, rb) => {
      for (const { col, asc } of keys) {
        const ta = ra.cells[col] ? ra.cells[col].textContent : "";
        const tbv = rb.cells[col] ? rb.cells[col].textContent : "";
        const c = cmpKeys(cellSortKey(ta), cellSortKey(tbv));
        if (c !== 0) return asc ? c : -c;
      }
      return 0;
    });
    dataRows.forEach((tr) => tb.appendChild(tr));
    padRows.forEach((tr) => tb.appendChild(tr));
    refreshSortHeaders(table);
  }

  function onSortHeaderClick(ev) {
    if (ev.target.closest(".col-resizer")) return;
    const th = ev.target.closest("th");
    const table = th && th.closest("table");
    if (!th || !table || !table.contains(th)) return;
    const thead = table.tHead;
    if (!thead || !thead.contains(th)) return;
    const col = th.cellIndex;
    if (col < 0) return;
    ev.preventDefault();
    let keys = Array.isArray(table._hppvSortKeys) ? table._hppvSortKeys.slice() : [];
    if (ev.shiftKey) {
      const i = keys.findIndex((k) => k.col === col);
      if (i >= 0) keys[i] = { col, asc: !keys[i].asc };
      else keys.push({ col, asc: true });
      keys = keys.slice(0, MULTI_SORT_MAX);
    } else if (keys.length && keys[0].col === col) {
      keys = [{ col, asc: !keys[0].asc }];
    } else {
      keys = [{ col, asc: true }];
    }
    table._hppvSortKeys = keys;
    applyTableSort(table);
  }

  function bindMultiSort(table) {
    if (!table || table.getAttribute("data-multi-sort") === "off") return;
    const ths = table.querySelectorAll("thead th");
    if (!ths.length) return;
    // 欄位數變更時重設排序鍵
    if (table._hppvSortColCount != null && table._hppvSortColCount !== ths.length) {
      table._hppvSortKeys = [];
    }
    table._hppvSortColCount = ths.length;
    ths.forEach((th) => {
      sortBaseLabel(th);
      th.style.cursor = "pointer";
      th.title = th.title || "點選排序；Shift+點選可多欄排序";
    });
    if (table.dataset.multiSortBound !== "1") {
      table.dataset.multiSortBound = "1";
      table.addEventListener("click", (ev) => {
        if (ev.target.closest("thead th")) onSortHeaderClick(ev);
      });
    }
    if (table._hppvSortKeys && table._hppvSortKeys.length) applyTableSort(table);
    else refreshSortHeaders(table);
  }

  window.HppvGridSort = {
    bind: bindMultiSort,
    apply: applyTableSort,
    setKeys: function (table, keys) {
      if (!table) return;
      table._hppvSortKeys = Array.isArray(keys) ? keys.slice(0, MULTI_SORT_MAX) : [];
      applyTableSort(table);
    },
  };

  document.addEventListener("DOMContentLoaded", () => window.HppvGridNav.bindAll());
})();
