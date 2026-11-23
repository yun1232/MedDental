'use strict';
(() => {
  const el = id => document.getElementById(id);
  let loaded = null, urls = [], chosenImages = new Map();
  const notice = text => { el('medical-status').textContent = text; };
  async function api(path, body, image = false) {
    const response = await fetch(path, { method: body ? 'POST' : 'GET', headers: { Authorization: 'Bearer ' + el('token').value, 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    if (image && response.ok) return response.blob();
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || result.error);
    return result;
  }
  const selected = () => loaded?.runs.find(r => r.run_id === el('medical-run').value);
  const latest = (r, role) => [...r.outputs].reverse().find(o => o.role === role)?.value;
  async function load() {
    const encounter = el('encounter').value;
    loaded = await api('/api/medical?encounter_id=' + encodeURIComponent(encounter));
    el('medical-run').replaceChildren(...loaded.runs.map(r => new Option(r.run_id + ' · v' + r.revision + ' · ' + r.status, r.run_id)));
    await render();
    notice('当前病例 v' + loaded.case.revision + '；' + loaded.runs.length + ' 个评估轮次。');
  }
  async function render() {
    for (const url of urls) URL.revokeObjectURL(url);
    urls = [];
    el('medical-images').replaceChildren();
    el('medical-readable').replaceChildren();
    chosenImages = new Map();
    el('medical-feedback').replaceChildren();
    const run = selected();
    if (!run) { el('medical-results').textContent = '患者上传原图并调用 medical_consult 后，这里显示五角色结果。'; return; }
    el('medical-results').textContent = JSON.stringify({ revision: run.revision, current_revision: loaded.case.revision, status: run.status, outputs: run.outputs, doctor_review: run.doctor_review }, null, 2);
    const clinical = document.createElement('div');
    for (const [role, label] of [['patient','患者资料'],['visual','视觉观察'],['reasoning','综合评估'],['review','独立审查']]) {
      const value = latest(run, role); if (!value) continue;
      const heading = document.createElement('h3'); heading.textContent = label; clinical.append(heading);
      const lines = role === 'patient' ? [value.summary, '待补充：' + value.missing.join('；'), value.next_question] : role === 'visual' ? [...value.findings.map(f => f.description + '（' + (f.certainty === 'clear' ? '清晰' : '不确定') + '）'), ...value.quality_issues] : role === 'reasoning' ? [value.summary, ...value.hypotheses.map(h => h.name + '；尚缺：' + h.missing.join('、')), ...value.next_steps, ...value.uncertainties] : ['流转：' + ({approve:'等待医生',patient:'患者补充',visual:'视觉复核',retrieval:'补充病例',reasoning:'修改评估',human:'人工核查'}[value.route] || value.route), ...value.issues, value.required_changes];
      for (const line of lines.filter(Boolean)) { const p = document.createElement('p'); p.textContent = line; clinical.append(p); }
    }
    el('medical-readable').append(clinical);
    const images = run.input.images.map(i => ({ id: i.image_id, label: '患者原图 ' + i.image_id }));
    images.push(...run.views.map(v => ({ id: v.asset_id, label: v.kind + ' · ' + v.description })));
    for (const image of images) {
      const blob = await api('/api/medical/image?run_id=' + encodeURIComponent(run.run_id) + '&image_id=' + encodeURIComponent(image.id), null, true);
      const url = URL.createObjectURL(blob); urls.push(url);
      const figure = document.createElement('figure'), img = document.createElement('img'), caption = document.createElement('figcaption');
      img.src = url; img.alt = image.label; caption.textContent = image.label; figure.append(img, caption);
      if (run.input.images.some(i => i.image_id === image.id)) {
        const label = document.createElement('label'), checkbox = document.createElement('input'), redaction = document.createElement('input');
        checkbox.type = 'checkbox'; label.append(checkbox, document.createTextNode('选择此图用于共享病例'));
        redaction.placeholder = '可选遮挡框：x1,y1,x2,y2，范围0..1000';
        redaction.setAttribute('aria-label', '身份标识遮挡框'); figure.append(label, redaction);
        chosenImages.set(image.id, { checkbox, redaction });
      }
      el('medical-images').append(figure);
    }
    // Only a blank redacted template is supplied: original patient text is never auto-published.
    el('medical-case').textContent = '填写病例并确认发布时，这里显示医生整理的结构化病例。';
    const cases = new Map(run.retrievals.flatMap(r => r.cases).map(c => [c.case_id + ':' + c.version, c]));
    for (const c of cases.values()) {
      const row = document.createElement('div'), label = document.createElement('p');
      const reranked = [...(run.rerankings || [])].reverse().flatMap(r => r.ranking).find(r => r.case_id === c.case_id && r.version === c.version);
      label.textContent = c.case_id + ' · v' + c.version + ' · 患者经历匹配 ' + (c.patient_experience_score ?? c.history_score ?? 0).toFixed(2) + ' · 白斑匹配 ' + (c.retrieval_channels?.includes('lesion') ? c.phenotype_score.toFixed(2) : '未用于视觉初排') + ' · 模型重排分 ' + (reranked ? reranked.relevance : '未推荐') + ' · 迭代分 ' + (Number.isFinite(reranked?.decay_score ?? c.decay_score) ? (reranked?.decay_score ?? c.decay_score).toFixed(2) : '旧轮次未记录') + ' · 最终分 ' + (Number.isFinite(reranked?.final_score) ? reranked.final_score.toFixed(2) : '未打分');
      row.append(label);
      for (const [kind, text] of [['helpful', '医生确认本例有参考价值'], ['not_helpful', '医生确认本例无参考价值']]) {
        const button = document.createElement('button'); button.type = 'button'; button.textContent = text;
        button.onclick = () => action(async () => { const reason = el('medical-note').value.trim(); if (!reason) throw new Error('请填写反馈依据。'); await api('/api/medical/feedback', { run_id: run.run_id, case_id: c.case_id, version: c.version, kind, reason }); notice('反馈已记录，同一病例版本在本轮仅计分一次。'); });
        row.append(button);
      }
      el('medical-feedback').append(row);
    }
    const recommendation = run.rerankings?.at(-1) || latest(run, 'retrieval');
    if (recommendation) { const pre = document.createElement('pre'); pre.textContent = JSON.stringify(recommendation, null, 2); el('medical-feedback').prepend(pre); }
  }
  async function action(fn) { try { await fn(); } catch (error) { notice(error.message); } }
  el('medical-load').onclick = () => action(load);
  el('medical-run').onchange = () => action(render);
  el('medical-review-form').onsubmit = event => { event.preventDefault(); action(async () => {
    const run = selected(); if (!run || !el('medical-confirmed').checked) throw new Error('请先选择评估并确认。');
    await api('/api/medical/review', { run_id: run.run_id, expected_revision: loaded.case.revision, confirmed: true, note: el('medical-note').value, outcome: el('medical-outcome').value });
    await load(); notice('医生复核已保存；尚未发布到共享病例库。');
  }); };
  el('medical-correct').onclick = () => action(async () => {
    if (!loaded) throw new Error('请先读取评估。');
    await api('/api/medical/correct', { encounter_id: el('encounter').value, expected_revision: loaded.case.revision, event_id: crypto.randomUUID(), note: el('medical-correction').value });
    await load(); notice('纠正已保存，旧评估已失效；请让患者端重新运行评估。');
  });
  el('medical-publish').onclick = () => action(async () => {
    const run = selected(); if (!run || !el('medical-deidentified').checked) throw new Error('请先完成医生复核，并确认脱敏与使用授权。');
    const lines = id => el(id).value.split('\n').map(s => s.trim()).filter(Boolean);
    const imageIds = [...chosenImages].filter(([, controls]) => controls.checkbox.checked).map(([id]) => id);
    const redactions = [];
    for (const id of imageIds) {
      const text = chosenImages.get(id).redaction.value.trim();
      if (text) {
        const box = text.split(',').map(Number);
        if (box.length !== 4 || box.some(n => !Number.isInteger(n) || n < 0 || n > 1000) || box[0] >= box[2] || box[1] >= box[3]) throw new Error('遮挡框需四个递增的整数坐标，范围0..1000。');
        redactions.push({ image_id: id, box, description: '医生指定的身份信息遮挡', basis: [] });
      }
    }
    const value = { phenotype: lines('publish-phenotype'), context: lines('publish-context'), evidence: lines('publish-evidence'), experience: el('publish-experience').value, pitfalls: lines('publish-pitfalls'), outcome: el('publish-outcome').value, source_ref: el('publish-source').value, image_ids: imageIds, regions: el('publish-regions').checked ? (latest(run,'visual')?.regions || []).filter(r => imageIds.includes(r.image_id)) : [], redactions };
    el('medical-case').textContent = JSON.stringify(value, null, 2);
    const result = await api('/api/medical/publish', { run_id: run.run_id, case_id: el('medical-case-id').value, expected_version: Number(el('medical-case-version').value), confirmed: true, deidentified: true, case: value });
    el('medical-case-version').value = result.version; notice('已发布 ' + result.case_id + ' · v' + result.version + '，其他患者可在同病种内召回。');
  });
  window.addEventListener('pagehide', () => urls.forEach(url => URL.revokeObjectURL(url)));
})();
