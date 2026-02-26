function cfg() {
  const b = document.body;

  return {
    days: Number(b.dataset.days || 180),
    userName: String(b.dataset.userName || ""),
    userHeight: Number(b.dataset.userHeight || 1.80),

    targetWeight: Number(b.dataset.targetWeight || 90),
    targetLean: Number(b.dataset.targetLean || 70),
    targetBf: Number(b.dataset.targetBf || 17.5),

    weeklyKcalTarget: Number(b.dataset.weeklyKcalTarget || (2500 * 7)),
  };
}

function toNum(v) {
  if (v === null || v === undefined) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function plotIfExists(id, plotFn) {
  const el = document.getElementById(id);
  if (!el) return;
  plotFn();
}

// --------------------
// Daily trends
// --------------------
async function loadTrends() {
  const { days } = cfg();
  const res = await fetch(`/api/daily_metrics?days=${days}`);
  const rows = await res.json();

  const x = rows.map(r => r.day);

  const sleepScore = rows.map(r => toNum(r.sleep_score));
  const sleepHours = rows.map(r => {
    const s = toNum(r.sleep_seconds);
    return s === null ? null : (s / 3600.0);
  });

  const hrv = rows.map(r => toNum(r.hrv_rmssd));
  const stress = rows.map(r => toNum(r.avg_stress));
  const rhr = rows.map(r => toNum(r.resting_hr));

  const bbLow = rows.map(r => toNum(r.body_battery_low));
  const bbHigh = rows.map(r => toNum(r.body_battery_high));

  const baseLayout = {
    margin: { t: 10, r: 10, b: 40, l: 50 },
    xaxis: { type: "date" },
    legend: { orientation: "h" }
  };

  Plotly.newPlot("sleep_chart", [
    { x, y: sleepScore, mode: "lines+markers", name: "Sleep score", yaxis: "y1" },
    { x, y: sleepHours, mode: "lines", name: "Sleep hours", yaxis: "y2" }
  ], {
    ...baseLayout,
    yaxis: { title: "Score" },
    yaxis2: { title: "Hours", overlaying: "y", side: "right" }
  }, { responsive: true });

  Plotly.newPlot("hrv_chart", [
    { x, y: hrv, mode: "lines+markers", name: "RMSSD" }
  ], { ...baseLayout, yaxis: { title: "ms" } }, { responsive: true });

  Plotly.newPlot("stress_chart", [
    { x, y: stress, mode: "lines+markers", name: "Avg stress" }
  ], { ...baseLayout, yaxis: { title: "Stress" } }, { responsive: true });

  Plotly.newPlot("rhr_chart", [
    { x, y: rhr, mode: "lines+markers", name: "Resting HR" }
  ], { ...baseLayout, yaxis: { title: "bpm" } }, { responsive: true });

  Plotly.newPlot("bb_chart", [
    { x, y: bbHigh, mode: "lines", name: "High" },
    { x, y: bbLow,  mode: "lines", name: "Low", fill: "tonexty" }
  ], { ...baseLayout, yaxis: { title: "Body Battery" } }, { responsive: true });
}

// --------------------
// Activity trends
// --------------------
async function loadActivityTrends() {
  const res = await fetch(`/api/activities?limit=200`);
  const rows = await res.json();

  const x = rows.map(r => r.start_time_local);
  const vo2 = rows.map(r => toNum(r.vo2max_value));
  const te  = rows.map(r => toNum(r.training_effect));

  const layout = {
    margin: { t: 10, r: 10, b: 40, l: 50 },
    xaxis: { type: "date" },
    legend: { orientation: "h" }
  };

  Plotly.newPlot("vo2_chart", [
    { x, y: vo2, mode: "lines+markers", name: "VO2max" }
  ], { ...layout, yaxis: { title: "ml/kg/min" } }, { responsive: true });

  Plotly.newPlot("te_chart", [
    { x, y: te, mode: "lines+markers", name: "Training effect" }
  ], { ...layout, yaxis: { title: "TE" } }, { responsive: true });
}

// --------------------
// Fitatu weekly stacked bar
// --------------------
async function loadFitatuWeekly() {
  const { days, weeklyKcalTarget } = cfg();
  const res = await fetch(`/api/fitatu_weekly?days=${days}`);
  const data = await res.json();

  const x = data.map(d => d.week_start);

  const pK = data.map(d => toNum(d.protein_kcal));
  const cK = data.map(d => toNum(d.carbs_kcal));
  const fK = data.map(d => toNum(d.fat_kcal));

  const pG = data.map(d => toNum(d.protein_g));
  const cG = data.map(d => toNum(d.carbs_g));
  const fG = data.map(d => toNum(d.fat_g));

  const total = data.map(d => toNum(d.kcal_total));

  Plotly.newPlot("fitatu_weekly_chart", [
    {
      x, y: pK, type: "bar", name: "Eiwit",
      customdata: pG,
      hovertemplate: "Week: %{x}<br>Eiwit: %{customdata:.0f} g<br>Eiwit kcal: %{y:.0f}<extra></extra>"
    },
    {
      x, y: cK, type: "bar", name: "Koolhydraten",
      customdata: cG,
      hovertemplate: "Week: %{x}<br>KH: %{customdata:.0f} g<br>KH kcal: %{y:.0f}<extra></extra>"
    },
    {
      x, y: fK, type: "bar", name: "Vet",
      customdata: fG,
      hovertemplate: "Week: %{x}<br>Vet: %{customdata:.0f} g<br>Vet kcal: %{y:.0f}<extra></extra>"
    },
    {
      x, y: total, mode: "lines+markers", name: "Totaal (Fitatu)",
      hovertemplate: "Week: %{x}<br>Totaal (Fitatu): %{y:.0f} kcal<extra></extra>"
    }
  ], {
    barmode: "stack",
    margin: { t: 10, r: 10, b: 40, l: 60 },
    xaxis: { type: "date" },
    yaxis: { title: "kcal / week" },
    legend: { orientation: "h" },
    shapes: [
      { type: "line", xref: "paper", x0: 0, x1: 1, yref: "y", y0: weeklyKcalTarget, y1: weeklyKcalTarget, line: { width: 2, dash: "dash" } }
    ],
    annotations: [
      { xref: "paper", x: 1, xanchor: "right", yref: "y", y: weeklyKcalTarget, yanchor: "bottom", showarrow: false,
        text: "Target: 2500 kcal/dag (17.500/week)" }
    ]
  }, { responsive: true });
}

// --------------------
// Pro analysis chart
// --------------------
async function loadProAnalysis() {
  const { days } = cfg();
  const res = await fetch(`/api/pro_weekly_analysis?days=${days}`);
  const data = await res.json();

  if (!Array.isArray(data) || data.length === 0) {
    const el = document.getElementById("pro_summary");
    if (el) el.textContent = "Geen pro analysis data (nog).";
    Plotly.newPlot("pro_analysis_chart", [], { title: "No data" }, { responsive: true });
    return;
  }

  const x = data.map(d => d.week_start);
  const kcal = data.map(d => toNum(d.kcal_day_avg));
  const tdee = data.map(d => toNum(d.tdee_est));
  const weight = data.map(d => toNum(d.weight_avg));
  const hrv = data.map(d => toNum(d.hrv_avg));

  // simpele tekst (laatste gewicht)
  const lastW = [...data].reverse().find(d => d.weight_avg !== null && d.weight_avg !== undefined);
  const sumEl = document.getElementById("pro_summary");
  if (sumEl) {
    sumEl.textContent = lastW ? `Laatste week: gewicht ~${Number(lastW.weight_avg).toFixed(1)} kg • richtpunt 2500 kcal/dag`
                              : "Nog geen Hume gewicht in de gekozen periode.";
  }

  Plotly.newPlot("pro_analysis_chart", [
    { x, y: kcal, type: "bar", name: "kcal/dag (avg)" },
    { x, y: tdee, mode: "lines+markers", name: "TDEE schatting", yaxis: "y2" },
    { x, y: weight, mode: "lines+markers", name: "Gewicht", yaxis: "y3" },
    { x, y: hrv, mode: "lines", name: "HRV", yaxis: "y4" }
  ], {
    margin: { t: 10, r: 10, b: 40, l: 60 },
    xaxis: { type: "date" },
    yaxis: { title: "kcal/dag" },
    yaxis2: { title: "TDEE", overlaying: "y", side: "right" },
    yaxis3: { title: "kg", anchor: "free", overlaying: "y", side: "left", position: 0.05 },
    yaxis4: { title: "HRV", anchor: "free", overlaying: "y", side: "right", position: 0.95 },
    legend: { orientation: "h" }
  }, { responsive: true });
}

// --------------------
// Hume charts (2x) + LBMI
// --------------------
async function loadHumeBody() {
  const { days, targetWeight, targetLean, targetBf } = cfg();
  const res = await fetch(`/api/hume_body?days=${days}`);
  const rows = await res.json();

  const x = rows.map(r => r.day);

  const weight = rows.map(r => toNum(r.weight_kg));
  const lean   = rows.map(r => toNum(r.lean_mass_kg));
  const bcm    = rows.map(r => toNum(r.body_cell_mass_kg));
  const muscle = rows.map(r => toNum(r.muscle_mass_kg));

  const bf      = rows.map(r => toNum(r.body_fat_pct));
  const water   = rows.map(r => toNum(r.body_water_pct));
  const visceral= rows.map(r => toNum(r.visceral_fat_index));

  const baseLayout = {
    margin: { t: 10, r: 10, b: 40, l: 60 },
    xaxis: { type: "date" },
    legend: { orientation: "h" }
  };

  const kgTraces = [
    { x, y: weight, mode: "lines+markers", name: "Weight (kg)" },
    { x, y: lean,   mode: "lines+markers", name: "Lean mass (kg)" },
    { x, y: bcm,    mode: "lines+markers", name: "BCM (kg)" },
  ];
  if (muscle.some(v => v !== null)) {
    kgTraces.push({ x, y: muscle, mode: "lines+markers", name: "Muscle mass (kg)" });
  }

  Plotly.newPlot("hume_mass_chart", kgTraces, {
    ...baseLayout,
    yaxis: { title: "kg" },
    shapes: [
      { type: "line", xref: "paper", x0: 0, x1: 1, yref: "y", y0: targetWeight, y1: targetWeight, line: { width: 2, dash: "dash" } },
      { type: "line", xref: "paper", x0: 0, x1: 1, yref: "y", y0: targetLean,   y1: targetLean,   line: { width: 2, dash: "dash" } },
    ],
    annotations: [
      { xref: "paper", x: 1, xanchor: "right", yref: "y", y: targetWeight, yanchor: "bottom", showarrow: false, text: `Target gewicht: ${targetWeight}` },
      { xref: "paper", x: 1, xanchor: "right", yref: "y", y: targetLean,   yanchor: "bottom", showarrow: false, text: `Target lean: ${targetLean}` },
    ]
  }, { responsive: true });

  Plotly.newPlot("hume_comp_chart", [
    { x, y: bf, mode: "lines+markers", name: "Body fat (%)" },
    { x, y: water, mode: "lines+markers", name: "Body water (%)" },
    { x, y: visceral, mode: "lines+markers", name: "Visceral fat index" },
  ], {
    ...baseLayout,
    yaxis: { title: "% / index" },
    shapes: [
      { type: "line", xref: "paper", x0: 0, x1: 1, yref: "y", y0: targetBf, y1: targetBf, line: { width: 2, dash: "dash" } },
    ],
    annotations: [
      { xref: "paper", x: 1, xanchor: "right", yref: "y", y: targetBf, yanchor: "bottom", showarrow: false, text: `Target body fat: ${targetBf}%` },
    ]
  }, { responsive: true });
}

async function loadLBMI() {
  const { days, userHeight } = cfg();
  const res = await fetch(`/api/hume_body?days=${days}`);
  const rows = await res.json();

  const x = [];
  const y = [];
  rows.forEach(r => {
    const lm = toNum(r.lean_mass_kg);
    if (lm !== null) {
      x.push(r.day);
      y.push(lm / (userHeight * userHeight));
    }
  });

  Plotly.newPlot("lbmi_chart", [
    { x, y, mode: "lines+markers", name: "LBMI" }
  ], {
    margin: { t: 10, r: 10, b: 40, l: 50 },
    xaxis: { type: "date" },
    yaxis: { title: "LBMI" },
    shapes: [
      { type: "line", xref: "paper", x0: 0, x1: 1, yref: "y", y0: 19, y1: 19, line: { dash: "dash" } },
      { type: "line", xref: "paper", x0: 0, x1: 1, yref: "y", y0: 21, y1: 21, line: { dash: "dash" } },
    ],
    annotations: [
      { xref: "paper", x: 1, xanchor: "right", yref: "y", y: 19, text: "fit", showarrow: false },
      { xref: "paper", x: 1, xanchor: "right", yref: "y", y: 21, text: "atletisch", showarrow: false },
    ],
    legend: { orientation: "h" }
  }, { responsive: true });
}

// --------------------
// Boot
// --------------------
document.addEventListener("DOMContentLoaded", () => {
  plotIfExists("sleep_chart", loadTrends);
  plotIfExists("vo2_chart", loadActivityTrends);
  plotIfExists("fitatu_weekly_chart", loadFitatuWeekly);
  plotIfExists("pro_analysis_chart", loadProAnalysis);
  plotIfExists("hume_mass_chart", loadHumeBody);
  plotIfExists("lbmi_chart", loadLBMI);
});