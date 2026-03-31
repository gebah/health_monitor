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

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function hexToRgba(hex, a = 0.12) {
  // ondersteunt #rgb en #rrggbb
  if (!hex) return `rgba(0,0,0,${a})`;
  let h = hex.replace("#", "").trim();
  if (h.length === 3) h = h.split("").map(ch => ch + ch).join("");
  const n = parseInt(h, 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  return `rgba(${r},${g},${b},${a})`;
}

function tsbMeta(tsb) {
  if (tsb >= 10) return { tone: "good", label: "Race-ready" };
  if (tsb >= 0)  return { tone: "good", label: "Fresh" };
  if (tsb >= -10) return { tone: "ok", label: "Normal" };
  if (tsb >= -25) return { tone: "ok", label: "Heavy" };
  return { tone: "bad", label: "Too much" };
}

function trainingAdvice({ tsb, readinessScore }) {
  // readinessScore kan null zijn; maak robuust
  const r = (readinessScore === null || readinessScore === undefined) ? null : Number(readinessScore);

  // 1) Echt diep rood: altijd remmen
  if (tsb <= -25) {
    return "Advies: rust / herstel (wandelen, mobiliteit, evt. 30–45 min Z1).";
  }

  // 2) Zware fase
  if (tsb <= -10) {
    if (r !== null && r < 50) return "Advies: hersteltraining (Z1–Z2) of rust; geen intensiteit.";
    return "Advies: rustige duur (Z2) of techniek; geen ‘max’ vandaag.";
  }

  // 3) Normaal
  if (tsb < 0) {
    if (r !== null && r < 50) return "Advies: easy day (Z1–Z2) — readiness is laag.";
    if (r !== null && r >= 75) return "Advies: normale training kan, maar hou intensiteit gecontroleerd.";
    return "Advies: normale training (duur/kracht), intervals liever kort.";
  }

  // 4) Fris
  if (tsb < 10) {
    if (r !== null && r >= 75) return "Advies: goede dag voor kwaliteit (intervals/tempo) als je zin hebt.";
    return "Advies: prima dag voor tempo/threshold of stevige duur.";
  }

  // 5) Race-ready
  if (r !== null && r < 50) return "Advies: je bent fris, maar readiness laag → kies techniek of korte prikkel.";
  return "Advies: topdag voor hard (intervals), PR-poging of wedstrijd.";
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
    height: 380,
    margin: { t: 10, r: 10, b: 40, l: 60 },
    xaxis: { type: "date" },
    legend: { orientation: "h" },
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
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
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
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
// Training load (ATL/CTL/TSB) a la TrainingPeaks + zones + today marker
// --------------------
async function loadTrainingLoad() {
  const { days } = cfg();
  const res = await fetch(`/api/training_load?days=${days}`);
  const data = await res.json();

  if (!Array.isArray(data) || data.length === 0) {
    Plotly.newPlot("training_load_chart", [], { title: "No training load data" }, { responsive: true });
    return;
  }

  const x = data.map(d => d.day);
  const tcl = data.map(d => toNum(d.tcl));
  const atl = data.map(d => toNum(d.atl));
  const ctl = data.map(d => toNum(d.ctl));
  const tsb = data.map(d => toNum(d.tsb));

  // Today = laatste datapunt
  const last = data[data.length - 1];
  const xToday = last.day;
  const atlToday = toNum(last.atl);
  const ctlToday = toNum(last.ctl);
  const tsbToday = toNum(last.tsb);

  const GOOD = cssVar("--good");
  const OK = cssVar("--ok");
  const BAD = cssVar("--bad");

  const todayColor =
    tsbToday >= 0 ? GOOD :
    tsbToday >= -10 ? OK :
    BAD;
  
  // Zones (TrainingPeaks-achtig)
  const zones = [
    { y0: 10,  y1: 25,  label: "Race-ready", color: hexToRgba(GOOD, 0.14) },
    { y0: 0,   y1: 10,  label: "Fresh",      color: hexToRgba(GOOD, 0.10) },
    { y0: -10, y1: 0,   label: "Normal",     color: hexToRgba(OK,   0.10) },
    { y0: -25, y1: -10, label: "Heavy",      color: hexToRgba(OK,   0.14) },
    { y0: -60, y1: -25, label: "Too much",   color: hexToRgba(BAD,  0.14) },
  ];

  // shapes: achtergrondbanden op yaxis2 (TSB)
  const shapes = zones.map(z => ({
    type: "rect",
    xref: "paper",
    x0: 0,
    x1: 1,
    yref: "y2",
    y0: z.y0,
    y1: z.y1,
    fillcolor: z.color,
    opacity: 1,
    line: { width: 0 },
    layer: "below",
  }));

  // zone labels rechts
  const zoneAnnotations = zones.map(z => ({
    xref: "paper",
    x: 1,
    xanchor: "right",
    yref: "y2",
    y: (z.y0 + z.y1) / 2,
    yanchor: "middle",
    showarrow: false,
    text: z.label,
    font: { size: 11 },
    opacity: 0.9,
  }));

  // Today marker line (vertical)
  shapes.push({
    type: "line",
    xref: "x",
    x0: xToday,
    x1: xToday,
    yref: "paper",
    y0: 0,
    y1: 1,
    line: { width: 2, dash: "dot" },
    opacity: 0.6,
  });

  // “Today” annotation + summary
  const headerAnnotation = {
    xref: "paper",
    yref: "paper",
    x: 0,
    y: 1.18,
    showarrow: false,
    text: `ATL=vermoeidheid (7d) • CTL=fitheid (42d) • TSB=vorm (CTL−ATL)`,
    font: { size: 12 },
  };

  const todayAnnotation = {
    xref: "paper",
    yref: "paper",
    x: 1,
    y: 1.18,
    xanchor: "right",
    showarrow: false,
    text: `Today: ATL ${atlToday.toFixed(1)} • CTL ${ctlToday.toFixed(1)} • TSB ${tsbToday.toFixed(1)}`,
    font: { size: 12 },
  };

  Plotly.newPlot("training_load_chart", [
    {
      x, y: tcl,
      type: "bar",
      name: "Daily load (TCL)",
      opacity: 0.45,
      yaxis: "y",
      hovertemplate: "Dag: %{x}<br>Training load: %{y:.0f}<extra></extra>",
    },
    {
      x, y: atl,
      mode: "lines",
      name: "ATL – vermoeidheid (7d)",
      yaxis: "y",
      hovertemplate: "Dag: %{x}<br>ATL: %{y:.1f}<extra></extra>",
    },
    {
      x, y: ctl,
      mode: "lines",
      name: "CTL – fitheid (42d)",
      yaxis: "y",
      hovertemplate: "Dag: %{x}<br>CTL: %{y:.1f}<extra></extra>",
    },
    {
      x, y: tsb,
      mode: "lines",
      name: "TSB – vorm (CTL − ATL)",
      yaxis: "y2",
      hovertemplate: "Dag: %{x}<br>TSB (vorm): %{y:.1f}<extra></extra>",
    },
    // today marker point on TSB
    {
      x: [xToday],
      y: [tsbToday],
      mode: "markers",
      name: "Today (TSB)",
      yaxis: "y2",
      marker: {
        size: 10,
        color: todayColor
      },
      hovertemplate: `Today: %{x}<br>TSB: %{y:.1f}<extra></extra>`,
      showlegend: true,
    },
  ], {
    margin: { t: 55, r: 10, b: 40, l: 60 },
    xaxis: { type: "date" },
    yaxis: { title: "Training load" },
    yaxis2: {
      title: "Vorm (TSB)",
      overlaying: "y",
      side: "right",
      zeroline: true,
      zerolinewidth: 1,
    },
    legend: { orientation: "h" },
    shapes,
    annotations: [headerAnnotation, todayAnnotation, ...zoneAnnotations],
  }, { responsive: true });
}

async function loadFormMetric() {
  const { days } = cfg();
  const res = await fetch(`/api/training_load?days=${days}`);
  const data = await res.json();
  if (!Array.isArray(data) || data.length === 0) return;

  const last = data[data.length - 1];

  const tsb = toNum(last.tsb);
  const atl = toNum(last.atl);
  const ctl = toNum(last.ctl);

  const card = document.getElementById("form_metric");
  const valEl = document.getElementById("form_metric_value");
  const labEl = document.getElementById("form_metric_label");
  const loadEl = document.getElementById("form_metric_load");
  const advEl = document.getElementById("form_metric_advice");
  if (!card || !valEl || !labEl || !loadEl || !advEl) return;

  // readiness score uit body-attrs (komt uit server)
  const readinessScore = Number(document.body.dataset.readinessScore || "NaN");
  const r = Number.isFinite(readinessScore) ? readinessScore : null;

  const meta = tsbMeta(tsb);

  card.classList.remove("metric-card--good", "metric-card--ok", "metric-card--bad");
  card.classList.add(`metric-card--${meta.tone}`);
  card.style.display = "";

  valEl.textContent = tsb.toFixed(1);
  labEl.textContent = meta.label;
  loadEl.textContent = `ATL ${atl.toFixed(0)} • CTL ${ctl.toFixed(0)}`;
  advEl.textContent = trainingAdvice({ tsb, readinessScore: r });
}

document.querySelectorAll(".gauge").forEach(g => {
  const val = Number(g.dataset.value || 0);
  const deg = (val / 100) * 360;

  g.style.background = `conic-gradient(var(--brand) ${deg}deg, #e5e7eb ${deg}deg)`;
});

async function renderStravaStatusTrend() {
  const el = document.getElementById("strava_status_trend_chart");
  if (!el || typeof Plotly === "undefined") return;

  const resp = await fetch("/api/strava_status_trend?days=7");
  const data = await resp.json();
  if (!data || !data.length) return;

  const days = data.map(d => d.day);
  const fitness = data.map(d => d.fitness);
  const fatigue = data.map(d => d.fatigue);
  const form = data.map(d => d.form);

  const traces = [
    {
      x: days,
      y: fitness,
      name: "Fitness",
      mode: "lines+markers",
      line: { width: 3 },
      marker: { size: 6 }
    },
    {
      x: days,
      y: fatigue,
      name: "Vermoeidheid",
      mode: "lines+markers",
      line: { width: 3 },
      marker: { size: 6 }
    },
    {
      x: days,
      y: form,
      name: "Vorm",
      mode: "lines+markers",
      line: { width: 3 },
      marker: { size: 6 }
    }
  ];

  const layout = {
    margin: { l: 36, r: 12, t: 8, b: 30 },
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    legend: {
      orientation: "h",
      y: 1.15,
      x: 0
    },
    xaxis: {
      tickangle: -30,
      gridcolor: "rgba(127,127,127,0.15)"
    },
    yaxis: {
      gridcolor: "rgba(127,127,127,0.15)",
      zeroline: true,
      zerolinecolor: "rgba(127,127,127,0.25)"
    }
  };

  const config = {
    responsive: true,
    displayModeBar: false
  };

  Plotly.newPlot("strava_status_trend_chart", traces, layout, config);
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
  plotIfExists("training_load_chart", loadTrainingLoad);
  plotIfExists("strava_status_trend_chart", renderStravaStatusTrend);

  if (document.getElementById("form_metric")) {
    loadFormMetric();
  }
});