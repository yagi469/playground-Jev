let presetsData = {};
let currentPresetKey = "support";

// DOM Elements
const presetChipsEl = document.getElementById("preset-chips");
const stateInputEl = document.getElementById("state-input");
const questionsListEl = document.getElementById("questions-list");
const btnResetEl = document.getElementById("btn-reset");
const btnEvaluateEl = document.getElementById("btn-evaluate");
const evalLoaderEl = document.getElementById("eval-loader");
const metricsHudEl = document.getElementById("metrics-hud");
const hudTimeEl = document.getElementById("hud-time");
const hudTokensEl = document.getElementById("hud-tokens");
const emptyStateEl = document.getElementById("empty-state");
const resultsCardsEl = document.getElementById("results-cards");

// Gemini DOM Elements
const geminiActionSectionEl = document.getElementById("gemini-action-section");
const btnImproveGeminiEl = document.getElementById("btn-improve-gemini");
const geminiLoaderEl = document.getElementById("gemini-loader");
const geminiResultsBoxEl = document.getElementById("gemini-results-box");

let lastEvaluatedText = "";
let lastEvaluatedResults = null;

// 安全なAPIリクエストヘルパー
async function safeFetchJson(url, options = {}) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (netErr) {
    throw new Error(`サーバーに接続できませんでした。サーバーが起動しているか確認してください (${netErr.message})`);
  }

  const rawText = await res.text();
  let data = null;
  try {
    data = JSON.parse(rawText);
  } catch {
    data = null;
  }

  if (!res.ok) {
    if (res.status === 502) {
      throw new Error("サーバーから 502 Bad Gateway が返されました。サーバーが停止しているか、一時的に接続が遮断された可能性があります。サーバーを再起動してください。");
    }
    if (res.status === 504) {
      throw new Error("処理がタイムアウトしました (504 Gateway Timeout)。");
    }
    const errMsg = (data && data.detail) ? data.detail : (rawText.slice(0, 150) || `HTTPエラー ${res.status}`);
    throw new Error(errMsg);
  }

  if (data === null) {
    throw new Error(`サーバーからの応答がJSONではありません: ${rawText.slice(0, 100)}`);
  }

  return data;
}

// 初期化
async function init() {
  try {
    presetsData = await safeFetchJson("/api/presets");
    renderPresetChips();
    selectPreset("support");
  } catch (err) {
    console.error(err);
    alert("バックエンドとの接続に失敗しました: " + err.message);
  }
}

// プリセットチップのレンダリング
function renderPresetChips() {
  presetChipsEl.innerHTML = "";
  Object.keys(presetsData).forEach((key) => {
    const p = presetsData[key];
    const chip = document.createElement("button");
    chip.className = `preset-chip ${key === currentPresetKey ? "active" : ""}`;
    chip.textContent = p.title;
    chip.addEventListener("click", () => selectPreset(key));
    presetChipsEl.appendChild(chip);
  });
}

// プリセット選択
function selectPreset(key) {
  currentPresetKey = key;
  const p = presetsData[key];
  if (!p) return;

  // チップのアクティブ状態更新
  document.querySelectorAll(".preset-chip").forEach((chip, i) => {
    const k = Object.keys(presetsData)[i];
    chip.classList.toggle("active", k === key);
  });

  // テキストと質問リスト更新
  stateInputEl.value = p.default_text;
  renderQuestionsList(p.questions);
}

// 質問リストプレビュー
function renderQuestionsList(questions) {
  questionsListEl.innerHTML = "";
  Object.keys(questions).forEach((qId) => {
    const q = questions[qId];
    const item = document.createElement("div");
    item.className = `question-chip-item type-${q.type}`;
    item.innerHTML = `
      <div style="display: flex; flex-direction: column; gap: 2px;">
        <span style="font-weight: 500;">${escapeHtml(q.label || qId)}</span>
        <span style="font-size: 11px; color: var(--text-dim);">${escapeHtml(q.instructions)}</span>
      </div>
      <span class="q-badge">${q.type.toUpperCase()}</span>
    `;
    questionsListEl.appendChild(item);
  });
}

// リセットボタン
btnResetEl.addEventListener("click", () => {
  if (presetsData[currentPresetKey]) {
    stateInputEl.value = presetsData[currentPresetKey].default_text;
  }
});

// 評価実行
btnEvaluateEl.addEventListener("click", async () => {
  const text = stateInputEl.value.trim();
  if (!text) {
    alert("テキストを入力してください。");
    return;
  }

  // ローディングUI
  btnEvaluateEl.disabled = true;
  evalLoaderEl.style.display = "block";
  document.querySelector(".btn-text-main").textContent = "Jev で評価中...";

  try {
    const data = await safeFetchJson("/api/evaluate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        state_text: text,
        preset_key: currentPresetKey,
      }),
    });

    renderResults(data);
  } catch (err) {
    console.error(err);
    alert("エラー: " + err.message);
  } finally {
    btnEvaluateEl.disabled = false;
    evalLoaderEl.style.display = "none";
    document.querySelector(".btn-text-main").textContent = "Jev で評価を実行";
  }
});

// 結果描画
function renderResults(data) {
  emptyStateEl.style.display = "none";
  resultsCardsEl.style.display = "flex";
  metricsHudEl.style.display = "flex";

  // メトリクス更新
  hudTimeEl.textContent = `⏱️ ${data.elapsed_ms} ms`;
  hudTokensEl.textContent = `🪙 In: ${data.usage.input_tokens} / Out: ${data.usage.output_tokens}`;

  resultsCardsEl.innerHTML = "";

  const results = data.results;
  Object.keys(results).forEach((qId) => {
    const item = results[qId];
    const card = document.createElement("div");
    card.className = "result-card";

    let bodyHtml = "";

    if (item.type === "choice") {
      const choiceDesc = item.criteria[item.choice] || "";
      const confPct = Math.round((item.confidence || 0) * 100);

      // 確率リスト
      let probBars = "";
      Object.keys(item.probabilities).forEach((optKey) => {
        const prob = item.probabilities[optKey];
        const pct = (prob * 100).toFixed(1);
        const optDesc = item.criteria[optKey] ? ` (${item.criteria[optKey]})` : "";
        const isSelected = optKey === item.choice;

        probBars += `
          <div class="prob-item">
            <div class="prob-info">
              <span class="prob-name" style="${isSelected ? 'color: #c4b5fd; font-weight: 600;' : ''}">${escapeHtml(optKey)}${escapeHtml(optDesc)}</span>
              <span class="prob-percent">${pct}%</span>
            </div>
            <div class="prob-bar-track">
              <div class="prob-bar-fill" style="width: ${pct}%; ${isSelected ? 'background: linear-gradient(90deg, #8b5cf6, #d946ef);' : 'background: rgba(255,255,255,0.2);'}"></div>
            </div>
          </div>
        `;
      });

      bodyHtml = `
        <div class="choice-main-answer">
          <span class="choice-key">👉 ${escapeHtml(item.choice)}</span>
          <span class="choice-desc">${escapeHtml(choiceDesc)}</span>
        </div>
        <div class="prob-list">
          ${probBars}
        </div>
      `;
    } else if (item.type === "score") {
      const confPct = Math.round((item.confidence || 0) * 100);
      const scoreVal = (item.score || 0).toFixed(2);

      let probBars = "";
      Object.keys(item.probabilities).forEach((levelKey) => {
        const prob = item.probabilities[levelKey];
        const pct = (prob * 100).toFixed(1);
        const levelIdx = parseInt(levelKey, 10);
        const levelDesc = item.criteria[levelIdx] ? `: ${item.criteria[levelIdx]}` : "";

        probBars += `
          <div class="prob-item">
            <div class="prob-info">
              <span class="prob-name">Lv.${levelKey}${escapeHtml(levelDesc)}</span>
              <span class="prob-percent">${pct}%</span>
            </div>
            <div class="prob-bar-track">
              <div class="prob-bar-fill" style="width: ${pct}%; background: linear-gradient(90deg, #06b6d4, #3b82f6);"></div>
            </div>
          </div>
        `;
      });

      bodyHtml = `
        <div class="score-metric-row">
          <div class="score-number">${scoreVal}</div>
          <div class="score-summary-text">
            スコア値 (0〜${item.criteria.length - 1} の連続値)<br>
            <span style="font-size: 11px; color: var(--text-dim);">※各レベルの確率分布を加重平均して導出</span>
          </div>
        </div>
        <div class="prob-list">
          ${probBars}
        </div>
      `;
    } else if (item.type === "noul") {
      const probYes = item.noul;
      const probNo = 1 - probYes;
      const pctYesNum = probYes * 100;
      const pctNoNum = probNo * 100;
      const pctYes = pctYesNum.toFixed(1);
      const pctNo = pctNoNum.toFixed(1);

      // 二者択一（50%基準）をベースにし、確信度と優勢度を明確化（判断保留の固定デッドゾーンを解消）
      let verdict = "";
      let themeClass = "";
      let badgeColor = "";
      let dominantPill = "";
      const isDominantYes = probYes >= 0.5;

      if (probYes >= 0.70) {
        verdict = "Yes (確実性が高い)";
        themeClass = "theme-yes-high";
        badgeColor = "var(--accent-emerald)";
        dominantPill = '<span class="noul-dominant-pill" style="color: var(--accent-emerald); border: 1px solid rgba(16, 185, 129, 0.4);">Yes 優勢</span>';
      } else if (probYes >= 0.50) {
        verdict = `Yes (優勢: ${pctYes}%)`;
        themeClass = "theme-yes";
        badgeColor = "#34d399";
        dominantPill = '<span class="noul-dominant-pill" style="color: #34d399; border: 1px solid rgba(52, 211, 153, 0.4);">Yes 優勢</span>';
      } else if (probYes >= 0.30) {
        // 30%〜50% (46.0%など): Noが54.0%で優勢。判断保留にせず明確にNo優勢と判定
        verdict = `No (優勢: ${pctNo}%)`;
        themeClass = "theme-no";
        badgeColor = "#60a5fa";
        dominantPill = '<span class="noul-dominant-pill" style="color: #60a5fa; border: 1px solid rgba(96, 165, 250, 0.4);">No 優勢</span>';
      } else {
        verdict = "No (低い / 該当なし)";
        themeClass = "theme-no-high";
        badgeColor = "#94a3b8";
        dominantPill = '<span class="noul-dominant-pill" style="color: #94a3b8; border: 1px solid rgba(148, 163, 184, 0.4);">No 優勢</span>';
      }

      bodyHtml = `
        <div class="noul-visual-box ${themeClass}">
          <div class="noul-value-row">
            <div class="noul-verdict-group">
              <span class="noul-state-badge" style="color: ${badgeColor};">${verdict}</span>
              ${dominantPill}
            </div>
            <div class="noul-prob-summary">
              <span class="noul-prob-subtext">Yes確率:</span>
              <span class="noul-percent-huge" style="color: ${isDominantYes ? 'var(--accent-emerald)' : '#60a5fa'};">${pctYes}%</span>
            </div>
          </div>
          <div class="noul-bar-track" title="中央の線は50%の境界値です">
            <div class="noul-bar-yes" style="width: ${pctYes}%;"></div>
            <div class="noul-bar-no" style="width: ${pctNo}%;"></div>
          </div>
          <div class="noul-legend-row">
            <span class="noul-legend-item ${isDominantYes ? 'is-dominant' : ''}">
              <span class="noul-legend-dot dot-yes"></span>
              Yes: ${pctYes}% ${isDominantYes ? '★' : ''}
            </span>
            <span class="noul-legend-item ${!isDominantYes ? 'is-dominant' : ''}">
              <span class="noul-legend-dot dot-no"></span>
              No: ${pctNo}% ${!isDominantYes ? '★' : ''}
            </span>
          </div>
        </div>
      `;
    }

    // カードヘッダーの確信度バッジ
    const confBadge = item.confidence !== undefined
      ? `<span class="confidence-pill">確信度 ${Math.round(item.confidence * 100)}%</span>`
      : "";

    card.innerHTML = `
      <div class="res-header">
        <div class="res-title-group">
          <div class="res-label">${escapeHtml(item.label)}</div>
          <div class="res-instructions">${escapeHtml(item.instructions || "")}</div>
        </div>
        ${confBadge}
      </div>
      ${bodyHtml}
    `;

    resultsCardsEl.appendChild(card);
  });

  // Jev の評価が完了したら Gemini 改善セクションをスタンバイ
  lastEvaluatedText = stateInputEl.value.trim();
  lastEvaluatedResults = data.results;
  geminiActionSectionEl.style.display = "flex";
  geminiResultsBoxEl.style.display = "none";
}

// Gemini 改善リライト実行
btnImproveGeminiEl.addEventListener("click", async () => {
  if (!lastEvaluatedText || !lastEvaluatedResults) {
    alert("先に Jev による評価を実行してください。");
    return;
  }

  btnImproveGeminiEl.disabled = true;
  geminiLoaderEl.style.display = "block";
  btnImproveGeminiEl.querySelector("span").textContent = "Gemini 推敲中...";

  try {
    const data = await safeFetchJson("/api/improve_with_gemini", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        state_text: lastEvaluatedText,
        results: lastEvaluatedResults,
      }),
    });

    renderGeminiResults(data);
  } catch (err) {
    console.error(err);
    alert("Gemini エラー: " + err.message);
  } finally {
    btnImproveGeminiEl.disabled = false;
    geminiLoaderEl.style.display = "none";
    btnImproveGeminiEl.querySelector("span").textContent = "✨ リライト案を生成";
  }
});

// Gemini リライト結果のレンダリング
function renderGeminiResults(resp) {
  const g = resp.data;
  geminiResultsBoxEl.style.display = "flex";

  // 検出課題バッジ
  let weaknessChips = "";
  if (g.detected_weaknesses && g.detected_weaknesses.length > 0) {
    g.detected_weaknesses.forEach((w) => {
      weaknessChips += `<span class="weakness-chip">⚠️ ${escapeHtml(w)}</span>`;
    });
  }

  // 改善ポイント一覧
  let improvementsList = "";
  if (g.improvements && g.improvements.length > 0) {
    g.improvements.forEach((item) => {
      improvementsList += `
        <div class="improvement-item">
          <span class="improvement-icon">✓</span>
          <span>${escapeHtml(item)}</span>
        </div>
      `;
    });
  }

  geminiResultsBoxEl.innerHTML = `
    <div class="gemini-res-header">
      <h4><span>✨</span> ${escapeHtml(resp.model || "Gemini")} による改善リライト案 (${resp.elapsed_ms} ms)</h4>
      <button class="btn-copy" id="btn-copy-rewrite">📋 コピー</button>
    </div>

    ${weaknessChips ? `
      <div class="weaknesses-container">
        <span class="weaknesses-title">Jev が特定した改善課題:</span>
        <div class="weakness-chips">${weaknessChips}</div>
      </div>
    ` : ''}

    <div>
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; flex-wrap: wrap; gap: 8px;">
        <span style="font-size: 12px; font-weight: 600; color: var(--text-dim); text-transform: uppercase;">リライト後の文章:</span>
        <button class="btn-apply-rewrite" id="btn-apply-rewrite">
          <span>📝 入力欄 (State) に反映する</span>
        </button>
      </div>
      <div class="rewrite-box" id="improved-text-content">${escapeHtml(g.improved_text)}</div>
    </div>

    <!-- Before vs After 比較セクションコンテナ -->
    <div class="comparison-section" id="comparison-container">
      <div style="display: flex; align-items: center; gap: 8px; color: #c4b5fd; font-size: 13px;">
        <div class="loader-spinner" style="display: inline-block; width: 14px; height: 14px; border-width: 2px;"></div>
        <span>Jev でリライト後の文章を再推論し、数値を比較中...</span>
      </div>
    </div>

    ${improvementsList ? `
      <div>
        <div style="font-size: 12px; font-weight: 600; color: var(--text-dim); text-transform: uppercase; margin-bottom: 6px;">改善のポイント解説:</div>
        <div class="improvements-list">${improvementsList}</div>
      </div>
    ` : ''}

    ${g.editor_note ? `
      <div class="editor-note-banner">
        <strong>💡 編集長の助言:</strong> ${escapeHtml(g.editor_note)}
      </div>
    ` : ''}
  `;

  // コピーボタン
  document.getElementById("btn-copy-rewrite").addEventListener("click", () => {
    navigator.clipboard.writeText(g.improved_text).then(() => {
      const btn = document.getElementById("btn-copy-rewrite");
      btn.textContent = "✓ コピー完了！";
      setTimeout(() => { btn.textContent = "📋 コピー"; }, 2000);
    });
  });

  // 入力欄に反映ボタン
  document.getElementById("btn-apply-rewrite").addEventListener("click", () => {
    stateInputEl.value = g.improved_text;
    const btn = document.getElementById("btn-apply-rewrite");
    btn.innerHTML = `<span>✓ 入力欄に反映しました！</span>`;
    stateInputEl.scrollIntoView({ behavior: "smooth", block: "center" });
    stateInputEl.style.borderColor = "var(--accent-emerald)";
    setTimeout(() => {
      btn.innerHTML = `<span>📝 入力欄 (State) に反映する</span>`;
      stateInputEl.style.borderColor = "";
    }, 2500);
  });

  // スクロールして結果を見せる
  geminiResultsBoxEl.scrollIntoView({ behavior: "smooth", block: "nearest" });

  // リライト後の Jev 自動再評価と数値比較レンダリング
  evaluateAndRenderComparison(lastEvaluatedResults, g.improved_text);
}

// リライト前後の Jev 数値比較
async function evaluateAndRenderComparison(beforeResults, improvedText) {
  const container = document.getElementById("comparison-container");
  if (!container || !beforeResults) return;

  try {
    const afterData = await safeFetchJson("/api/evaluate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        state_text: improvedText,
        preset_key: currentPresetKey,
      }),
    });

    const afterResults = afterData.results;
    let cardsHtml = "";

    Object.keys(beforeResults).forEach((qId) => {
      const bItem = beforeResults[qId];
      const aItem = afterResults[qId];
      if (!aItem) return;

      const label = bItem.label || qId;
      let valRowHtml = "";
      let deltaBadgeHtml = "";
      let barsHtml = "";

      if (bItem.type === "score") {
        const bScore = bItem.score;
        const aScore = aItem.score;
        const delta = aScore - bScore;
        const maxScore = (bItem.criteria && bItem.criteria.length > 1) ? bItem.criteria.length - 1 : 3;
        const bPct = Math.min(100, Math.max(0, (bScore / maxScore) * 100)).toFixed(1);
        const aPct = Math.min(100, Math.max(0, (aScore / maxScore) * 100)).toFixed(1);

        const isNegativeMetric = qId.includes("clickbait") || qId.includes("frustration") || qId.includes("risk");
        if (isNegativeMetric) {
          if (delta < -0.15) {
            deltaBadgeHtml = `<span class="comp-delta-badge reduced-risk">${delta.toFixed(2)} ↓ 抑制改善</span>`;
          } else if (delta > 0.15) {
            deltaBadgeHtml = `<span class="comp-delta-badge warning">+${delta.toFixed(2)} ↑ 上昇</span>`;
          } else {
            deltaBadgeHtml = `<span class="comp-delta-badge neutral">±0.00 維持</span>`;
          }
        } else {
          if (delta > 0.15) {
            deltaBadgeHtml = `<span class="comp-delta-badge improved">+${delta.toFixed(2)} ↑ 向上</span>`;
          } else if (delta < -0.15) {
            deltaBadgeHtml = `<span class="comp-delta-badge warning">${delta.toFixed(2)} ↓ 低下</span>`;
          } else {
            deltaBadgeHtml = `<span class="comp-delta-badge neutral">±0.00 維持</span>`;
          }
        }

        valRowHtml = `
          <div class="comp-values-row">
            <div class="comp-col">
              <span class="comp-col-tag">Before (元)</span>
              <span class="comp-col-val before-val">${bScore.toFixed(2)}</span>
            </div>
            <span class="comp-arrow">➔</span>
            <div class="comp-col" style="text-align: right;">
              <span class="comp-col-tag">After (リライト)</span>
              <span class="comp-col-val after-val">${aScore.toFixed(2)}</span>
            </div>
          </div>
        `;

        barsHtml = `
          <div class="comp-bars-container">
            <div class="comp-bar-row">
              <span class="comp-bar-tag">Before</span>
              <div class="comp-bar-track">
                <div class="comp-bar-fill-before" style="width: ${bPct}%;"></div>
              </div>
              <span style="font-size: 10px; color: var(--text-dim); width: 32px; text-align: right;">${bScore.toFixed(1)}</span>
            </div>
            <div class="comp-bar-row">
              <span class="comp-bar-tag" style="color: #38bdf8;">After</span>
              <div class="comp-bar-track">
                <div class="comp-bar-fill-after" style="width: ${aPct}%;"></div>
              </div>
              <span style="font-size: 10px; color: #38bdf8; font-weight: 600; width: 32px; text-align: right;">${aScore.toFixed(1)}</span>
            </div>
          </div>
        `;
      } else if (bItem.type === "noul") {
        const bYes = bItem.noul;
        const aYes = aItem.noul;
        const bPct = (bYes * 100).toFixed(1);
        const aPct = (aYes * 100).toFixed(1);
        const delta = (parseFloat(aPct) - parseFloat(bPct)).toFixed(1);

        const isRiskMetric = qId.includes("misunderstanding") || qId.includes("block") || qId.includes("exploit") || qId.includes("risk");
        if (isRiskMetric) {
          if (parseFloat(delta) < -2.0) {
            deltaBadgeHtml = `<span class="comp-delta-badge reduced-risk">${delta}% ↓ リスク低減</span>`;
          } else if (parseFloat(delta) > 2.0) {
            deltaBadgeHtml = `<span class="comp-delta-badge warning">+${delta}% ↑ リスク増加</span>`;
          } else {
            deltaBadgeHtml = `<span class="comp-delta-badge neutral">±0% 維持</span>`;
          }
        } else {
          if (parseFloat(delta) > 2.0) {
            deltaBadgeHtml = `<span class="comp-delta-badge improved">+${delta}% ↑ 実践度向上</span>`;
          } else if (parseFloat(delta) < -2.0) {
            deltaBadgeHtml = `<span class="comp-delta-badge warning">${delta}% ↓</span>`;
          } else {
            deltaBadgeHtml = `<span class="comp-delta-badge neutral">±0% 維持</span>`;
          }
        }

        valRowHtml = `
          <div class="comp-values-row">
            <div class="comp-col">
              <span class="comp-col-tag">Before (Yes確率)</span>
              <span class="comp-col-val before-val">${bPct}%</span>
            </div>
            <span class="comp-arrow">➔</span>
            <div class="comp-col" style="text-align: right;">
              <span class="comp-col-tag">After (Yes確率)</span>
              <span class="comp-col-val after-val">${aPct}%</span>
            </div>
          </div>
        `;

        barsHtml = `
          <div class="comp-bars-container">
            <div class="comp-bar-row">
              <span class="comp-bar-tag">Before</span>
              <div class="comp-bar-track">
                <div class="comp-bar-fill-before" style="width: ${bPct}%;"></div>
              </div>
              <span style="font-size: 10px; color: var(--text-dim); width: 38px; text-align: right;">${bPct}%</span>
            </div>
            <div class="comp-bar-row">
              <span class="comp-bar-tag" style="color: #38bdf8;">After</span>
              <div class="comp-bar-track">
                <div class="comp-bar-fill-after" style="width: ${aPct}%;"></div>
              </div>
              <span style="font-size: 10px; color: #38bdf8; font-weight: 600; width: 38px; text-align: right;">${aPct}%</span>
            </div>
          </div>
        `;
      } else if (bItem.type === "choice") {
        const bChoice = bItem.choice;
        const aChoice = aItem.choice;
        const isChanged = bChoice !== aChoice;

        deltaBadgeHtml = isChanged
          ? `<span class="comp-delta-badge improved">カテゴリ変化</span>`
          : `<span class="comp-delta-badge neutral">維持</span>`;

        valRowHtml = `
          <div class="comp-values-row">
            <div class="comp-col">
              <span class="comp-col-tag">Before</span>
              <span class="comp-col-val before-val" style="font-size: 13px;">${escapeHtml(bChoice)}</span>
            </div>
            <span class="comp-arrow">➔</span>
            <div class="comp-col" style="text-align: right;">
              <span class="comp-col-tag">After</span>
              <span class="comp-col-val after-val" style="font-size: 13px;">${escapeHtml(aChoice)}</span>
            </div>
          </div>
        `;
      }

      cardsHtml += `
        <div class="comp-card">
          <div class="comp-card-top">
            <span class="comp-label">${escapeHtml(label)}</span>
            ${deltaBadgeHtml}
          </div>
          ${valRowHtml}
          ${barsHtml}
        </div>
      `;
    });

    container.innerHTML = `
      <div class="comparison-header">
        <div class="comparison-title-group">
          <span class="comparison-title">📊 Jev による改善効果の数値測定 (Before vs After)</span>
          <span class="comparison-subtitle">同じ評価軸で即座に再推論し、客観的な変化を数値化</span>
        </div>
        <span style="font-size: 11px; color: var(--accent-emerald);">⏱️ 再評価完了 (${afterData.elapsed_ms} ms)</span>
      </div>
      <div class="comparison-cards-grid">
        ${cardsHtml}
      </div>
    `;
  } catch (err) {
    console.error("Comparison re-eval error:", err);
    container.innerHTML = `
      <div style="font-size: 12px; color: var(--accent-rose); padding: 8px;">
        ⚠️ リライト後の数値比較の取得中にエラーが発生しました: ${escapeHtml(err.message)}
      </div>
    `;
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// 起動
init();
