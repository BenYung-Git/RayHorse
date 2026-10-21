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

  document.addEventListener("DOMContentLoaded", () => window.HppvGridNav.bindAll());
})();
