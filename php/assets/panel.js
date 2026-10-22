/* HELPTOA Bot - JS local do painel (Bootstrap 5, sem CDN). */
(function () {
  "use strict";

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function fmt(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }

  /* Resumo do multi-select + busca + todos/limpar */
  function refreshLabel(picker) {
    var boxes = picker.querySelectorAll(".ms-options input[type=checkbox]");
    var total = boxes.length, on = 0, last = "";
    boxes.forEach(function (b) {
      if (b.checked) { on++; last = b.getAttribute("data-label") || b.value; }
    });
    var el = picker.querySelector(".ms-label");
    if (!el) { return; }
    var vazio = picker.getAttribute("data-empty") || "Todos";
    if (on === 0) { el.textContent = vazio; }
    else if (on === 1) { el.textContent = last; }
    else if (on === total) { el.textContent = vazio + " (" + total + ")"; }
    else { el.textContent = on + " selecionados"; }
  }

  document.querySelectorAll(".ms-picker").forEach(function (picker) {
    picker.addEventListener("change", function () { refreshLabel(picker); });
    var search = picker.querySelector(".ms-search");
    if (search) {
      search.addEventListener("input", function () {
        var q = search.value.toLowerCase();
        picker.querySelectorAll(".ms-options [data-filter]").forEach(function (row) {
          row.style.display = (row.getAttribute("data-filter") || "").toLowerCase().indexOf(q) === -1 ? "none" : "";
        });
      });
    }
    var all = picker.querySelector(".ms-all"), none = picker.querySelector(".ms-none");
    if (all) {
      all.addEventListener("click", function () {
        picker.querySelectorAll(".ms-options .form-check").forEach(function (row) {
          if (row.style.display !== "none") { row.querySelector("input").checked = true; }
        });
        refreshLabel(picker);
      });
    }
    if (none) {
      none.addEventListener("click", function () {
        picker.querySelectorAll(".ms-options input[type=checkbox]").forEach(function (b) { b.checked = false; });
        refreshLabel(picker);
      });
    }
    refreshLabel(picker);
  });

  document.addEventListener("click", function (ev) {
    document.querySelectorAll("details.ms-picker[open]").forEach(function (d) {
      if (!d.contains(ev.target)) { d.open = false; }
    });
  });

  /* Atalhos de periodo: preenchem de/ate no mesmo form */
  document.querySelectorAll("[data-range]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var form = btn.closest("form");
      if (!form) { return; }
      var de = form.querySelector("[data-range-de]"), ate = form.querySelector("[data-range-ate]");
      if (!de || !ate) { return; }
      var dias = parseInt(btn.getAttribute("data-range"), 10) || 0;
      var hoje = new Date(), ini = new Date();
      ini.setDate(hoje.getDate() - dias);
      de.value = fmt(ini);
      ate.value = fmt(hoje);
    });
  });

  /* Filtro client-side de tabela: input[data-filter-table="#seletor"] */
  document.querySelectorAll("[data-filter-table]").forEach(function (input) {
    input.addEventListener("input", function () {
      var table = document.querySelector(input.getAttribute("data-filter-table"));
      if (!table) { return; }
      var q = input.value.toLowerCase();
      table.querySelectorAll("tbody tr").forEach(function (tr) {
        tr.style.display = tr.textContent.toLowerCase().indexOf(q) === -1 ? "none" : "";
      });
    });
  });

  /* Auto-refresh (data-auto-refresh no body, em segundos): recarrega a pagina
     sozinha, mas PAUSA enquanto a pessoa esta com foco em algum campo de
     formulario (ex.: digitando no filtro de Autor) e reinicia a contagem
     quando o foco sai. Evita apagar o que a pessoa estava digitando. */
  var autoRefreshSec = parseInt(document.body.getAttribute("data-auto-refresh") || "0", 10);
  if (autoRefreshSec > 0) {
    var autoRefreshTimer = null;
    var armAutoRefresh = function () {
      clearTimeout(autoRefreshTimer);
      autoRefreshTimer = setTimeout(function () { location.reload(); }, autoRefreshSec * 1000);
    };
    document.addEventListener("focusin", function (ev) {
      if (ev.target.closest("form")) { clearTimeout(autoRefreshTimer); }
    });
    document.addEventListener("focusout", function (ev) {
      if (ev.target.closest("form")) { armAutoRefresh(); }
    });
    armAutoRefresh();
  }

  /* Perguntas da opcao (Pedir info e transferir): adicionar / remover linhas */
  function renumberAskRows(tbody) {
    tbody.querySelectorAll("[data-ask-row]").forEach(function (tr, i) {
      var num = tr.querySelector("[data-ask-num]");
      if (num) { num.textContent = i + 1; }
    });
  }

  function clearAskRow(tr) {
    tr.querySelectorAll("input").forEach(function (i) { i.value = ""; });
    tr.querySelectorAll("select").forEach(function (s) { s.selectedIndex = 0; });
  }

  document.querySelectorAll("[data-ask-add]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var tbody = document.getElementById(btn.getAttribute("data-ask-add"));
      if (!tbody) { return; }
      var rows = tbody.querySelectorAll("[data-ask-row]");
      if (!rows.length) { return; }
      var clone = rows[rows.length - 1].cloneNode(true);
      clearAskRow(clone);
      tbody.appendChild(clone);
      renumberAskRows(tbody);
      var first = clone.querySelector("input[name=\"q_prompt[]\"]");
      if (first) { first.focus(); }
    });
  });

  document.querySelectorAll("[data-ask-del]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var tr = btn.closest("[data-ask-row]");
      var tbody = tr ? tr.parentElement : null;
      if (!tr || !tbody) { return; }
      if (tbody.querySelectorAll("[data-ask-row]").length <= 1) {
        clearAskRow(tr);
        return;
      }
      tbody.removeChild(tr);
      renumberAskRows(tbody);
    });
  });

  /* Janelas de turno/descanso (aba Times): adicionar/remover linha, bitmask de
     dias (7 checkboxes -> 1 input hidden) e fim do descanso = inicio + 1h. */
  function schRow(el) { return el.closest("[data-sch-row]"); }

  function schUpdateDays(checkbox) {
    var tr = schRow(checkbox);
    if (!tr) { return; }
    var mask = 0;
    tr.querySelectorAll("[data-sch-day]").forEach(function (b) {
      if (b.checked) { mask |= (1 << parseInt(b.getAttribute("data-sch-day"), 10)); }
    });
    var hidden = tr.querySelector("[data-sch-mask]");
    if (hidden) { hidden.value = mask || 127; }
  }

  function schSetAllDays(tr, checked) {
    tr.querySelectorAll("[data-sch-day]").forEach(function (b) { b.checked = checked; });
    schUpdateDays(tr.querySelector("[data-sch-day]") || tr);
  }

  function schAutoEnd(startEl) {
    var tr = schRow(startEl);
    if (!tr || !startEl.value) { return; }
    var kindSel = tr.querySelector("[data-sch-kind]");
    var endEl = tr.querySelector("[data-sch-end]");
    if (!kindSel || !endEl || kindSel.value !== "break" || endEl.value) { return; }
    var parts = startEl.value.split(":");
    var mins = (parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10) + 60) % 1440;
    var hh = String(Math.floor(mins / 60)).padStart(2, "0");
    var mm = String(mins % 60).padStart(2, "0");
    endEl.value = hh + ":" + mm;
  }

  function clearSchRow(tr) {
    var kindSel = tr.querySelector("[data-sch-kind]");
    if (kindSel) { kindSel.value = "shift"; }
    tr.querySelectorAll("input[type=time]").forEach(function (i) { i.value = ""; });
    schSetAllDays(tr, true);
  }

  document.addEventListener("change", function (ev) {
    if (ev.target.matches("[data-sch-day]")) { schUpdateDays(ev.target); }
  });
  document.addEventListener("input", function (ev) {
    if (ev.target.matches("[data-sch-start]")) { schAutoEnd(ev.target); }
  });
  document.querySelectorAll("[data-sch-allday]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var tr = schRow(btn);
      if (tr) { schSetAllDays(tr, true); }
    });
  });
  document.querySelectorAll("[data-sch-add]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var tbody = document.getElementById(btn.getAttribute("data-sch-add"));
      if (!tbody) { return; }
      var rows = tbody.querySelectorAll("[data-sch-row]");
      if (!rows.length) { return; }
      var clone = rows[rows.length - 1].cloneNode(true);
      clearSchRow(clone);
      tbody.appendChild(clone);
    });
  });
  document.querySelectorAll("[data-sch-del]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var tr = schRow(btn);
      var tbody = tr ? tr.parentElement : null;
      if (!tr || !tbody) { return; }
      if (tbody.querySelectorAll("[data-sch-row]").length <= 1) {
        clearSchRow(tr);
        return;
      }
      tbody.removeChild(tr);
    });
  });
})();