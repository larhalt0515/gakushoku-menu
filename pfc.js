(() => {
  "use strict";

  if (document.querySelector(".pfc-guide")) return;

  const budgetBar = document.querySelector(".budget-bar");
  if (!budgetBar) return;

  const DAILY_KCAL_PER_KG = 30;
  const MEAL_ENERGY_RATIO = 0.35;
  const PFC_RATIOS = { protein: 0.15, fat: 0.25, carb: 0.60 };
  const PFC_MIN_RATIO = 0.80;
  const LOW_BMI_WARNING = 18.5;
  const PFC_FIELDS = ["energy", "protein", "fat", "carb"];
  const SIZE_ORDER = { "小": 0, "並": 1, "中": 2, "大": 3 };
  const SIZE_RICE_DELTA = { "小": [-100, -24], "並": [0, 0], "中": [0, 0], "大": [150, 36] };
  const MAIN = ["主菜", "丼", "麺"];
  const CARB = ["丼", "麺", "ご飯"];
  const SOLO_NG = ["汁物", "小鉢", "サラダ", "ご飯", "デザート"];
  const SOUP_MISO = /(?:味噌|みそ|ミソ)(?:汁|しる|シル|じる|ジル)/;
  const SOUP_TONJIRU = /(?:(?:豚|ぶた|ブタ)(?:汁|しる|シル|じる|ジル)|(?:とん|トン)(?:汁|しる|シル|じる|ジル))/;
  let pfcTarget = null;
  let pfcInputError = "";
  let pfcLowBmiBlocked = false;

  function roundHalfToEven(value) {
    const lower = Math.floor(value);
    const fraction = value - lower;
    const tieTolerance = Number.EPSILON * Math.max(1, Math.abs(value)) * 4;
    if (Math.abs(fraction - 0.5) <= tieTolerance) {
      return lower % 2 === 0 ? lower : lower + 1;
    }
    return fraction < 0.5 ? lower : lower + 1;
  }

  function calculateTarget(height, weight) {
    const mealKcal = Math.max(1, roundHalfToEven(
      weight * DAILY_KCAL_PER_KG * MEAL_ENERGY_RATIO,
    ));
    return {
      height,
      weight,
      bmi: weight / ((height / 100) ** 2),
      mealKcal,
      protein: mealKcal * PFC_RATIOS.protein / 4,
      fat: mealKcal * PFC_RATIOS.fat / 9,
      carb: mealKcal * PFC_RATIOS.carb / 4,
    };
  }

  function hasCompleteNutrition(item) {
    return PFC_FIELDS.every((field) => item[field] != null);
  }
  
  function sumNutrition(combo, field) {
    let total = 0;
    for (const dish of combo) {
      if (dish[field] == null) return null;
      total += Number(dish[field]);
    }
    return total;
  }
  function nutritionValue(value) {
    return value == null ? null : Number(value);
  }

  function pfcMetrics(item, target) {
    const complete = hasCompleteNutrition(item);
    if (!target) {
      return { pfcComplete: complete, pfcError: null, pfcFit: null, pfcAdequate: null };
    }
    if (!complete) {
      return { pfcComplete: false, pfcError: null, pfcFit: null, pfcAdequate: false };
    }
    const expected = [target.protein, target.fat, target.carb];
    const actual = [item.protein, item.fat, item.carb];
    const macroError = actual.reduce(
      (sum, value, index) => sum + Math.abs(value - expected[index]) / Math.max(expected[index], 1),
      0,
    ) / expected.length;
    const energyError = Math.abs(item.energy - target.mealKcal) / Math.max(target.mealKcal, 1);
    const error = macroError * 0.75 + energyError * 0.25;
    const required = [target.mealKcal, ...expected];
    const values = [item.energy, ...actual];
    const pfcAdequate = required.every(
      (value, index) => value > 0 && values[index] >= value * PFC_MIN_RATIO,
    );
    return {
      pfcComplete: true,
      pfcError: error,
      pfcFit: Math.max(0, (1 - Math.min(error, 1)) * 100),
      pfcAdequate,
    };
  }

  function expandPfcSizes(dishes, onlySize) {
    const output = [];
    for (const dish of dishes) {
      const sizes = dish.sizes && Object.keys(dish.sizes).length ? dish.sizes : { "並": dish.price };
      const multi = Object.keys(sizes).length > 1;
      for (const [size, price] of Object.entries(sizes).sort(
        (a, b) => (SIZE_ORDER[a[0]] ?? 9) - (SIZE_ORDER[b[0]] ?? 9),
      )) {
        if (onlySize && multi && size !== onlySize) continue;
        const [energyDelta, carbDelta] = SIZE_RICE_DELTA[size] || [0, 0];
        const energy = nutritionValue(dish.energy);
        const carb = nutritionValue(dish.carb);
        output.push({
          name: multi ? `${dish.name}(${size})` : dish.name,
          price: Math.round(price),
          energy: multi && energy != null ? Math.max(0, energy + energyDelta) : energy,
          protein: nutritionValue(dish.protein),
          fat: nutritionValue(dish.fat),
          carb: multi && carb != null ? Math.max(0, carb + carbDelta) : carb,
          category: dish.category,
          base: dish.name,
        });
      }
    }
    return output;
  }

  function hasMisoTonjiruPair(combo) {
    const names = combo.map((dish) => String(dish.name || "").replace(/\s+/g, ""));
    const misoIndexes = names
      .map((name, index) => (SOUP_MISO.test(name) ? index : -1))
      .filter((index) => index >= 0);
    const tonjiruIndexes = names
      .map((name, index) => (SOUP_TONJIRU.test(name) ? index : -1))
      .filter((index) => index >= 0);
    return misoIndexes.some((misoIndex) => tonjiruIndexes.some((tonjiruIndex) => misoIndex !== tonjiruIndex));
  }

  function suggestPfcCombos(dishes, budget, onlySize, mode, topN = 3, maxItems = 4, rankTarget = pfcTarget) {
    const variants = expandPfcSizes(dishes, onlySize);
    const all = [];
    for (let size = 1; size <= Math.min(variants.length, maxItems); size += 1) {
      for (const combo of combinationsPfc(variants, size)) {
        const bases = combo.map((dish) => dish.base);
        if (new Set(bases).size !== bases.length) continue;
        if (hasMisoTonjiruPair(combo)) continue;
        const price = combo.reduce((sum, dish) => sum + dish.price, 0);
        if (price > budget) continue;
        if (combo.filter((dish) => CARB.includes(dish.category)).length > 1) continue;
        if (size === 1 && SOLO_NG.includes(combo[0].category)) continue;
        const item = {
          combo,
          price,
          diff: budget - price,
          energy: sumNutrition(combo, "energy"),
          protein: sumNutrition(combo, "protein"),
          fat: sumNutrition(combo, "fat"),
          carb: sumNutrition(combo, "carb"),
          balanced: combo.some((dish) => MAIN.includes(dish.category)),
          hasCarb: combo.some((dish) => CARB.includes(dish.category)),
        };
        Object.assign(item, pfcMetrics(item, rankTarget));
        if ((mode === "lowcal" || mode === "cospa") && !item.balanced) continue;
        all.push(item);
      }
    }

    const score = (item) => {
      if (mode === "protein") return item.protein || 0;
      if (mode === "lowcal") return item.energy > 0 ? (item.protein || 0) / item.energy : 0;
      if (mode === "cospa") return item.price > 0 ? ((item.energy || 0) + (item.protein || 0) * 4) / item.price : 0;
      if (mode === "pfc" && !rankTarget) return (item.protein || 0) * 4 - (item.fat || 0);
      return -item.diff;
    };

    all.sort((a, b) => {
      if (mode === "pfc" && rankTarget) {
        if (a.pfcAdequate !== b.pfcAdequate) return Number(b.pfcAdequate) - Number(a.pfcAdequate);
        if (a.pfcComplete !== b.pfcComplete) return Number(b.pfcComplete) - Number(a.pfcComplete);
        if (a.balanced !== b.balanced) return Number(b.balanced) - Number(a.balanced);
        if (a.pfcComplete && b.pfcComplete && a.pfcError !== b.pfcError) {
          return a.pfcError - b.pfcError;
        }
      }
      return score(b) - score(a) || a.diff - b.diff || (Number(b.balanced) - Number(a.balanced));
    });
    let ranked = all;
    if (mode === "pfc" && rankTarget) {
      const balanced = all.filter((item) => item.balanced);
      if (balanced.length) ranked = balanced;
    }
    return ranked.slice(0, topN);
  }

  function combinationsPfc(items, size) {
    const result = [];
    function visit(start, picked) {
      if (picked.length === size) {
        result.push(picked.slice());
        return;
      }
      for (let index = start; index <= items.length - (size - picked.length); index += 1) {
        picked.push(items[index]);
        visit(index + 1, picked);
        picked.pop();
      }
    }
    visit(0, []);
    return result;
  }

  function comboHtmlPfc(dishes, budget, onlySize, rice, dessert, mode) {
    if (!dishes.length) return "";
    const picks = [rice, dessert].filter(Boolean);
    const pickPrice = picks.reduce((sum, dish) => sum + dish.price, 0);
    const picked = picks.map((dish) => ({
      name: dish.name,
      price: dish.price,
      energy: nutritionValue(dish.energy),
      protein: nutritionValue(dish.protein),
      fat: nutritionValue(dish.fat),
      carb: nutritionValue(dish.carb),
      category: dish.category,
      base: dish.base || dish.name,
    }));
    const fixedNutrition = Object.fromEntries(
      PFC_FIELDS.map((field) => [field, sumNutrition(picked, field)]),
    );
    const residualTarget = pfcTarget && PFC_FIELDS.every((field) => fixedNutrition[field] != null)
      ? {
        ...pfcTarget,
        mealKcal: pfcTarget.mealKcal - fixedNutrition.energy,
        protein: pfcTarget.protein - fixedNutrition.protein,
        fat: pfcTarget.fat - fixedNutrition.fat,
        carb: pfcTarget.carb - fixedNutrition.carb,
      }
      : pfcTarget;
    let candidates = [];
    if (pickPrice <= budget) {
      let pool = dishes;
      if (rice) pool = pool.filter((dish) => !CARB.includes(dish.category));
      if (dessert) pool = pool.filter((dish) => dish.category !== "デザート");
      candidates = suggestPfcCombos(pool, budget - pickPrice, onlySize, mode, 3, 4, residualTarget).map((best) => {
        const combo = [...picked, ...best.combo];
        const item = {
          combo,
          price: best.price + pickPrice,
          diff: budget - best.price - pickPrice,
          energy: sumNutrition(combo, "energy"),
          protein: sumNutrition(combo, "protein"),
          fat: sumNutrition(combo, "fat"),
          carb: sumNutrition(combo, "carb"),
          balanced: combo.some((dish) => MAIN.includes(dish.category)),
          hasCarb: combo.some((dish) => CARB.includes(dish.category)),
        };
        return Object.assign(item, pfcMetrics(item, pfcTarget));
      });
    }

    if (pfcTarget && mode === "pfc") {
      candidates.sort((a, b) => {
        if (a.pfcAdequate !== b.pfcAdequate) return Number(b.pfcAdequate) - Number(a.pfcAdequate);
        if (a.pfcComplete !== b.pfcComplete) return Number(b.pfcComplete) - Number(a.pfcComplete);
        if (a.balanced !== b.balanced) return Number(b.balanced) - Number(a.balanced);
        return (a.pfcError ?? Infinity) - (b.pfcError ?? Infinity) || a.diff - b.diff;
      });
    }

    let body;
    if (!candidates.length) {
      body = `<div class="combo-empty">¥${budget}以内の組み合わせが見つからなかった〜！予算や指定を変えてみて</div>`;
    } else {
      body = candidates.map((item, index) => {
        const names = item.combo.map((dish) => `${dish.name}(${dish.category})`).join(" + ");
        const badges = [];
        const showPfc = pfcTarget && mode === "pfc";
        if (item.balanced) badges.push("⭐主菜あり");
        if (item.hasCarb) badges.push("🍚炭水化物あり");
        if (showPfc && item.pfcComplete) {
          badges.push(`📐PFCフィット ${item.pfcFit.toFixed(0)}%`);
          if (!item.pfcAdequate) badges.push("⚠️目安未達");
        }
        if (showPfc && !item.pfcComplete) badges.push("📐PFC判定不可");
        const nutrition = showPfc && !item.pfcComplete
          ? "栄養値不足（PFC比較対象外）"
          : `${item.energy == null ? "-" : item.energy}kcal / P${item.protein == null ? "-" : item.protein.toFixed(1)} F${item.fat == null ? "-" : item.fat.toFixed(1)} C${item.carb == null ? "-" : item.carb.toFixed(1)}`;
        return `<div class="combo-card"><div class="combo-rank">#${index + 1}</div>`
          + `<div class="combo-names">${names}</div>`
          + `<div class="combo-stats"><b>¥${item.price}</b> <span class="diff">(残¥${item.diff})</span> / ${nutrition}</div>`
          + `<div class="combo-badge">${badges.join(" ") || "🍃軽め"}</div></div>`;
      }).join("");
    }
    const sizeTag = onlySize ? `（🍚${onlySize}）` : "";
    const riceTag = rice ? `（${rice.name}）` : "";
    const dessertTag = dessert ? `（🍰${dessert.name}）` : "";
    const labels = { protein: "💪高タンパク", lowcal: "🥗低カロリー", cospa: "💰コスパ", pfc: "⚖️PFCバランス" };
    const modeLabel = labels[mode] || "🎫 予算スレスレ";
    return `<div class="sec-h">${modeLabel}最適化 TOP3（¥<span class="bv">${budget}</span>）${sizeTag}${riceTag}${dessertTag}</div>${body}`;
  }

  window.suggestCombos = suggestPfcCombos;
  window.comboHtml = comboHtmlPfc;

  const style = document.createElement("style");
  style.textContent = `
    .pfc-guide { max-width: 880px; margin: var(--space-4) auto var(--space-6); padding: var(--space-4); background: var(--color-bg-subtle); border: 1px solid var(--color-border-base); border-radius: var(--radius-lg); }
    .pfc-guide-head { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: var(--space-4); align-items: end; }
    .pfc-guide h2, .pfc-guide h3 { font-size: var(--font-size-lg); font-weight: var(--font-weight-bold); }
    .pfc-guide h2 { margin-bottom: var(--space-1); }
    .pfc-kicker { margin-bottom: var(--space-1); color: var(--color-accent-base); font-size: var(--font-size-xs); font-weight: var(--font-weight-bold); letter-spacing: .1em; }
    .pfc-copy, .pfc-note, .pfc-footnote { color: var(--color-fg-muted); font-size: var(--font-size-xs); line-height: var(--font-line-relaxed); }
    .pfc-copy { max-width: 560px; }
    .pfc-form { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: end; }
    .pfc-field { display: grid; gap: 3px; color: var(--color-fg-muted); font-size: var(--font-size-xs); font-weight: var(--font-weight-medium); }
    .pfc-field input { width: 92px; padding: var(--space-2); border: 1px solid var(--color-border-base); border-radius: var(--radius-sm); background: var(--color-bg-base); color: var(--color-fg-base); font: inherit; font-weight: var(--font-weight-bold); }
    .pfc-button { padding: var(--space-2) var(--space-3); border: 1px solid var(--color-accent-base); border-radius: var(--radius-sm); background: var(--color-accent-base); color: #fff; cursor: pointer; font: inherit; font-weight: var(--font-weight-bold); }
    .pfc-button:hover { background: var(--color-accent-hover); }
    .pfc-guide input:focus-visible, .pfc-guide button:focus-visible, .pfc-guide summary:focus-visible { outline: 3px solid var(--color-accent-base); outline-offset: 2px; }
    .pfc-summary { margin-top: var(--space-4); padding: var(--space-3); border: 1px solid var(--color-border-base); border-radius: var(--radius-md); background: var(--color-bg-base); }
    .pfc-summary[data-ready="false"] { color: var(--color-fg-muted); }
    .pfc-summary-title { margin-bottom: var(--space-2); font-weight: var(--font-weight-bold); }
    .pfc-summary-error { color: var(--color-danger-base); }
    .pfc-stats { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: var(--space-2); }
    .pfc-stat { padding-top: var(--space-2); border-top: 2px solid var(--color-border-strong); }
    .pfc-stat strong { display: block; font-size: var(--font-size-lg); line-height: var(--font-line-tight); }
    .pfc-stat span { color: var(--color-fg-muted); font-size: var(--font-size-xs); }
    .pfc-reference { margin-top: var(--space-4); }
    .pfc-reference summary { cursor: pointer; color: var(--color-accent-base); font-size: var(--font-size-sm); font-weight: var(--font-weight-bold); }
    .pfc-table-wrap { overflow-x: auto; margin-top: var(--space-2); border: 1px solid var(--color-border-base); border-radius: var(--radius-md); }
    table.pfc-table { width: 100%; min-width: 560px; border-collapse: collapse; font-size: var(--font-size-xs); background: var(--color-bg-base); }
    .pfc-table caption { padding: var(--space-2); color: var(--color-fg-muted); caption-side: top; text-align: left; }
    .pfc-table th, .pfc-table td { padding: var(--space-2); border-top: 1px solid var(--color-border-base); text-align: right; white-space: nowrap; }
    .pfc-table th { background: var(--color-bg-elevated); color: var(--color-fg-muted); font-weight: var(--font-weight-medium); }
    .pfc-table th:first-child, .pfc-table td:first-child { text-align: left; }
    .pfc-table tbody tr:hover td { background: var(--color-bg-elevated); }
    .pfc-p { color: var(--color-danger-base); font-weight: var(--font-weight-bold); }
    .pfc-f { color: var(--color-accent-base); font-weight: var(--font-weight-bold); }
    .pfc-c { color: var(--color-success-base); font-weight: var(--font-weight-bold); }
    .pfc-note { margin-top: var(--space-2); }
    .pfc-soup-note { margin-top: var(--space-3); padding: var(--space-2) var(--space-3); border-left: 3px solid var(--color-accent-base); background: var(--color-bg-elevated); color: var(--color-fg-muted); font-size: var(--font-size-xs); }
    .pfc-footnote { margin-top: var(--space-2); }
    .pfc-footnote + .pfc-footnote { margin-top: var(--space-1); }
    @media (max-width: 640px) {
      .pfc-guide-head { grid-template-columns: 1fr; align-items: start; }
      .pfc-form { align-items: stretch; }
      .pfc-field { flex: 1 1 120px; }
      .pfc-field input, .pfc-button { width: 100%; }
      .pfc-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .pfc-stat:last-child { grid-column: span 2; }
    }
    @media (prefers-reduced-motion: reduce) {
      .pfc-button { transition: none; }
    }
  `;
  document.head.appendChild(style);

  const guide = document.createElement("section");
  guide.className = "pfc-guide";
  guide.setAttribute("aria-labelledby", "pfc-guide-title");
  guide.innerHTML = `
    <div class="pfc-guide-head">
      <div>
        <div class="pfc-kicker">PFC GUIDE</div>
        <h2 id="pfc-guide-title">身長・体重から1食の目安</h2>
        <p class="pfc-copy">入力した体重をもとに、学食で使える1食分のカロリーとPFCを簡易計算します。BMIが18.5以上のときだけ目安として採用し、入力値は保存・送信しません。</p>
      </div>
      <form class="pfc-form" id="pfc-form">
        <label class="pfc-field" for="pfc-height">身長（cm）<input id="pfc-height" type="number" min="100" max="230" step="0.1" placeholder="170"></label>
        <label class="pfc-field" for="pfc-weight">体重（kg）<input id="pfc-weight" type="number" min="25" max="250" step="0.1" placeholder="65"></label>
        <button class="pfc-button" type="submit">目安を計算</button>
      </form>
    </div>
    <div class="pfc-summary" id="pfc-summary" data-ready="false" aria-live="polite"></div>
    <div class="pfc-soup-note"><strong>汁物はどちらか1つ。</strong> 味噌汁と豚汁は同時選択をおすすめしません。既存のおすすめ候補からも除外します。</div>
    <details class="pfc-reference" open>
      <summary>体重別のPFC早見表</summary>
      <div class="pfc-table-wrap">
        <table class="pfc-table">
          <caption>体重 × 30kcal/日 × 昼食35%、P15%・F25%・C60%（エネルギー比）</caption>
          <thead><tr><th scope="col">体重</th><th scope="col">1食 kcal</th><th scope="col">P タンパク質</th><th scope="col">F 脂質</th><th scope="col">C 炭水化物</th></tr></thead>
          <tbody id="pfc-reference-body"></tbody>
        </table>
      </div>
    </details>
    <p class="pfc-footnote">早見表を含む値は、年齢・性別・活動量を含まない注文比較用の簡易目安です。医療・減量用の指示ではありません。</p>
    <p class="pfc-footnote">PFCフィットは充足率ではなく目安への近さです。カロリー・P・F・Cのどれかが80%未満なら「目安未達」と表示します。</p>
    <p class="pfc-footnote">料理の栄養値が不足している場合は、PFCを0として扱わず比較対象外にします。</p>
  `;
  budgetBar.insertAdjacentElement("afterend", guide);

  const form = document.getElementById("pfc-form");
  const heightInput = document.getElementById("pfc-height");
  const weightInput = document.getElementById("pfc-weight");
  const summary = document.getElementById("pfc-summary");
  const referenceBody = document.getElementById("pfc-reference-body");
  const modeSelect = document.getElementById("omode");

  function format(value) {
    return Number(value).toFixed(1);
  }

  function updatePfcModeLock(blocked) {
    const pfcOption = modeSelect?.querySelector('option[value="pfc"]');
    if (pfcOption) pfcOption.disabled = blocked;
    if (blocked && modeSelect?.value === "pfc") modeSelect.value = "";
  }
  function renderReference() {
    if (pfcLowBmiBlocked) {
      referenceBody.innerHTML = `<tr><td colspan="5">BMI 18.5未満の入力では、体重連動の早見表を表示しません。</td></tr>`;
      return;
    }
    const weights = [40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90];
    referenceBody.innerHTML = weights.map((weight) => {
      const target = calculateTarget(170, weight);
      return `<tr><td><strong>${weight}kg</strong></td><td>${target.mealKcal}</td>`
        + `<td class="pfc-p">${format(target.protein)}g</td>`
        + `<td class="pfc-f">${format(target.fat)}g</td>`
        + `<td class="pfc-c">${format(target.carb)}g</td></tr>`;
    }).join("");
  }
  function renderTarget() {
    summary.dataset.ready = pfcTarget ? "true" : "false";
    if (pfcInputError) {
      summary.innerHTML = `<div class="pfc-summary-title pfc-summary-error">${pfcInputError}</div>`;
      return;
    }
    if (!pfcTarget) {
      summary.innerHTML = `<div class="pfc-summary-title">身長・体重を入れると、ここに1食の目安が出ます。</div>`
        + `<div class="pfc-note">PFCバランスモードを選ぶと、入力した目安に近い候補を優先します。</div>`;
      return;
    }
    const pfcModeActive = !modeSelect || modeSelect.value === "pfc";
    const selectedMode = modeSelect?.options[modeSelect.selectedIndex]?.textContent || "別のモード";
    const modeNote = pfcModeActive
      ? "「PFCバランス」を選択中。候補のP/F/Cがこの目安に近い順で表示されます。"
      : `現在は${selectedMode}。PFC目安で並べるには「⚖️PFCバランス」を選択してね。`;
    summary.innerHTML = `<div class="pfc-summary-title">${pfcTarget.height}cm / ${pfcTarget.weight}kg の簡易目安</div>`
      + `<div class="pfc-stats">`
      + `<div class="pfc-stat"><strong>${pfcTarget.mealKcal}</strong><span>kcal / 1食</span></div>`
      + `<div class="pfc-stat"><strong>${format(pfcTarget.protein)}g</strong><span>P</span></div>`
      + `<div class="pfc-stat"><strong>${format(pfcTarget.fat)}g</strong><span>F</span></div>`
      + `<div class="pfc-stat"><strong>${format(pfcTarget.carb)}g</strong><span>C</span></div>`
      + `<div class="pfc-stat"><strong>${format(pfcTarget.bmi)}</strong><span>BMI</span></div>`
      + `</div><div class="pfc-note">${modeNote}</div>`;
  }

  function applyProfile() {
    const height = Number(heightInput.value);
    const weight = Number(weightInput.value);
    pfcInputError = "";
    pfcTarget = null;
    pfcLowBmiBlocked = false;
    updatePfcModeLock(false);
    if (!heightInput.value && !weightInput.value) {
      if (modeSelect && modeSelect.value === "pfc") modeSelect.value = "";
    } else if (!heightInput.value || !weightInput.value) {
      pfcInputError = "身長と体重を両方入力してね";
    } else if (height < 100 || height > 230) {
      pfcInputError = "身長は100〜230cmの範囲で入力してね";
    } else if (weight < 25 || weight > 250) {
      pfcInputError = "体重は25〜250kgの範囲で入力してね";
    } else {
      const bmi = weight / ((height / 100) ** 2);
      if (bmi < LOW_BMI_WARNING) {
        pfcLowBmiBlocked = true;
        pfcInputError = `BMI ${bmi.toFixed(1)}（18.5未満）。身長・体重だけの低カロリー目安は表示しません。必要量は医師または管理栄養士に相談してね。`;
        updatePfcModeLock(true);
      } else {
        pfcTarget = calculateTarget(height, weight);
        if (modeSelect && !modeSelect.value) modeSelect.value = "pfc";
      }
    }
    renderTarget();
    window.renderAll();
    renderReference();
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    applyProfile();
  });
  if (modeSelect) modeSelect.addEventListener("change", () => {
    if (pfcLowBmiBlocked && modeSelect.value === "pfc") {
      modeSelect.value = "";
      window.renderAll();
    }
    renderTarget();
  });
  renderReference();
  renderTarget();
  window.renderAll();
})();
