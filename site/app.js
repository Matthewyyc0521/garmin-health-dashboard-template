"use strict";

const metrics = [
  ["sleep_minutes", "睡眠时长", "", 0],
  ["hrv_ms", "夜间 HRV", "毫秒", 0],
  ["resting_hr_bpm", "静息心率", "次/分", 0],
  ["body_battery", "身体电量", "/100", 0],
  ["steps", "步数", "步", 0],
  ["water_l", "手填饮水", "升", 2],
  ["weight_kg", "手填体重", "公斤", 2],
];
const sportNames = {
  running: "跑步", trail_running: "越野跑", cycling: "骑行",
  walking: "步行", hiking: "徒步", mountaineering: "登山",
  swimming: "游泳", pool_swimming: "泳池游泳", lap_swimming: "泳池游泳",
  strength_training: "力量训练", indoor_cycling: "室内骑行",
};
const $ = (id) => document.getElementById(id);
let entries = new Map();
let activities = [];
const drawings = new Map();

function labelNumber(n, precision = 0) {
  return n.toLocaleString("zh-CN", {maximumFractionDigits: precision});
}

function minutes(n) {
  const rounded = Math.round(n);
  const hours = Math.floor(rounded / 60);
  return hours ? hours + "小时" + (rounded % 60 ? " " + rounded % 60 + "分" : "") : rounded + "分";
}

function metricValue(field, val, precision) {
  if (!Number.isFinite(val)) return "—";
  if (field === "sleep_minutes") return minutes(val);
  return labelNumber(val, precision);
}

function dateValid(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const time = Date.parse(value + "T00:00:00Z");
  return Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === value;
}

function calendarWeek(end) {
  const last = Date.parse(end + "T00:00:00Z");
  return Array.from({length: 7}, (_, i) =>
    new Date(last - (6 - i) * 86400000).toISOString().slice(0, 10));
}

function plot(canvas, draw) {
  const redraw = () => {
    const {width, height} = canvas.getBoundingClientRect();
    if (!width || !height) return;
    const ratio = Math.min(window.devicePixelRatio || 1, 3);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(ratio, ratio);
    const style = getComputedStyle(canvas);
    draw(ctx, width, height, style.getPropertyValue("--chart"), style.getPropertyValue("--line"));
  };
  drawings.set(canvas, redraw);
  redraw();
}

function redrawAll() {
  for (const [canvas, draw] of drawings) {
    if (canvas.isConnected) draw();
    else drawings.delete(canvas);
  }
}

function trend(canvas, values) {
  plot(canvas, (ctx, width, height, ink, line) => {
    const observed = values.filter(Number.isFinite);
    ctx.strokeStyle = line;
    ctx.beginPath();
    ctx.moveTo(0, height - 3);
    ctx.lineTo(width, height - 3);
    ctx.stroke();
    if (!observed.length) return;
    const lo = Math.min(...observed), hi = Math.max(...observed);
    let previous = null;
    ctx.strokeStyle = ink;
    ctx.fillStyle = ink;
    ctx.lineWidth = 2;
    values.forEach((v, i) => {
      if (!Number.isFinite(v)) { previous = null; return; }
      const x = 4 + i * (width - 8) / 6;
      const y = hi === lo ? height / 2 : height - 6 - (height - 13) * (v - lo) / (hi - lo);
      if (previous) {
        ctx.beginPath();
        ctx.moveTo(...previous);
        ctx.lineTo(x, y);
        ctx.stroke();
      }
      ctx.beginPath();
      ctx.arc(x, y, 2.7, 0, Math.PI * 2);
      ctx.fill();
      previous = [x, y];
    });
  });
}

function metricCard(info, dates) {
  const [field, name, unit, precision] = info;
  const values = dates.map(d => entries.get(d)?.[field]);
  const latest = values[6], observed = values.filter(Number.isFinite);
  const card = $("metric").content.firstElementChild.cloneNode(true);
  card.querySelector("h3").textContent = name;
  card.querySelector(".reading strong").textContent = metricValue(field, latest, precision);
  card.querySelector(".unit").textContent = Number.isFinite(latest) ? unit : "当日无记录";
  const stat = card.querySelector(".stat");
  if (observed.length) {
    const avg = observed.reduce((a, b) => a + b, 0) / observed.length;
    stat.textContent = "均值 " + metricValue(field, avg, precision) + (field === "body_battery" ? "" : unit) +
      " · " + observed.length + "/7 天";
  } else stat.textContent = "近七日无记录";
  const canvas = card.querySelector("canvas");
  canvas.setAttribute("aria-label", name + "七天趋势。" + dates.map((date, i) =>
    date.slice(5) + " " + (Number.isFinite(values[i]) ? metricValue(field, values[i], precision) + unit : "无记录")
  ).join("，"));
  $("metrics").append(card);
  trend(canvas, values);
}

function element(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}

function sport(type) {
  return Object.hasOwn(sportNames, type) ? sportNames[type] : String(type || "其他运动");
}

function routePreview(target, raw) {
  const segments = [];
  let previous = null, lonPrevious = 0;
  let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
  for (const point of Array.isArray(raw) ? raw : []) {
    if (!Array.isArray(point) || point.length !== 2 || !point.every(Number.isFinite) ||
        Math.abs(point[0]) > 90 || Math.abs(point[1]) > 180) {
      previous = null;
      continue;
    }
    if (!previous) { previous = []; segments.push(previous); }
    const lat = point[0];
    let lon = point[1];
    if (Number.isFinite(xmin)) {
      while (lon - lonPrevious > 180) lon -= 360;
      while (lon - lonPrevious < -180) lon += 360;
    }
    lonPrevious = lon;
    previous.push([lon, lat]);
    xmin = Math.min(xmin, lon); xmax = Math.max(xmax, lon);
    ymin = Math.min(ymin, lat); ymax = Math.max(ymax, lat);
  }
  if (!segments.some(seg => seg.length > 1) || (xmin === xmax && ymin === ymax)) {
    target.append(element("p", "本次活动暂无可绘制的 GPS 路线。", "note"));
    return;
  }
  const canvas = element("canvas", undefined, "route");
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", "本次活动的 GPS 轨迹，北向上");
  target.append(canvas);
  plot(canvas, (ctx, width, height, ink) => {
    const correction = Math.max(.0001, Math.cos((ymax + ymin) * Math.PI / 360));
    const dx = (xmax - xmin) * correction, dy = ymax - ymin;
    const scale = Math.min((width - 36) / (dx || 1e-12), (height - 36) / (dy || 1e-12));
    ctx.strokeStyle = ink;
    ctx.lineWidth = 2.6;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    for (const seg of segments) {
      ctx.beginPath();
      seg.forEach(([lon, lat], i) => {
        const x = (width - dx * scale) / 2 + (lon - xmin) * correction * scale;
        const y = (height - dy * scale) / 2 + (ymax - lat) * scale;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }
  });
}

function showActivity(activity, selected) {
  for (const btn of $("activities").querySelectorAll("button"))
    btn.setAttribute("aria-pressed", String(btn === selected));
  const detail = $("detail");
  detail.replaceChildren();
  const distance = Number.isFinite(activity.distance_km) ? labelNumber(activity.distance_km, 2) + " 公里" : "里程缺失";
  const duration = Number.isFinite(activity.duration_minutes) ? minutes(activity.duration_minutes) : "时长缺失";
  detail.append(element("h3", sport(activity.type) + " · " + activity.date),
                element("p", distance + " / " + duration, "detail-stats"));
  routePreview(detail, activity.route);
  redrawAll();
}

function renderActivities(end) {
  const list = activities.filter(a => a.date <= end);
  $("count").textContent = "截至当日 · " + list.length + " 条";
  const buttons = $("activities");
  buttons.replaceChildren();
  $("detail").replaceChildren();
  if (!list.length) {
    buttons.append(element("p", "截至所选日期尚无收录的活动。", "note"));
    return;
  }
  for (const activity of list) {
    const button = element("button", undefined, "activity-row");
    button.type = "button";
    const summary = element("span");
    summary.append(element("strong", sport(activity.type)), element("small", activity.date));
    button.append(summary, element("span", Number.isFinite(activity.distance_km)
      ? labelNumber(activity.distance_km, 2) + " km" : "—"));
    button.addEventListener("click", () => showActivity(activity, button));
    buttons.append(button);
  }
  showActivity(list[0], buttons.firstElementChild);
}

function render() {
  const end = $("anchor").value;
  if (!dateValid(end)) return;
  const dates = calendarWeek(end);
  $("window").textContent = dates[0] + " — " + end;
  $("metrics").replaceChildren();
  for (const metric of metrics) metricCard(metric, dates);
  renderActivities(end);
  redrawAll();
}

async function load() {
  try {
    const response = await fetch("/api/overview", {cache: "no-store", credentials: "omit"});
    if (!response.ok) throw new Error();
    const data = await response.json();
    $("source").textContent = response.headers.get("X-Overview-Source") === "demo" ? "演示数据" : "本地数据";
    const time = new Date(data.updated_at);
    $("sync").textContent = Number.isNaN(time.getTime()) ? "同步时间未知" :
      "数据更新于 " + time.toLocaleString("zh-CN", {hour12: false});
    entries = new Map(data.days.filter(r => r && dateValid(r.date)).map(r => [r.date, r]));
    activities = data.activities.filter(r => r && dateValid(r.date)).sort((a, b) => {
      const startA = a.start_local || a.date, startB = b.start_local || b.date;
      return b.date.localeCompare(a.date) || startB.localeCompare(startA) ||
        String(b.id).localeCompare(String(a.id));
    });
    const dates = Array.from(new Set([...entries.keys(), ...activities.map(x => x.date)])).sort();
    if (!dates.length) {
      $("message").textContent = "当前没有已导入的健康或运动记录。";
      $("message").hidden = false;
      return;
    }
    const anchor = $("anchor");
    anchor.min = dates[0]; anchor.max = dates.at(-1); anchor.value = dates.at(-1);
    anchor.disabled = false;
    anchor.addEventListener("change", render);
    $("dashboard").hidden = false;
    render();
  } catch {
    $("sync").textContent = "读取失败";
    $("message").textContent = "无法读取概览数据。请检查本机服务和数据文件，然后刷新页面。";
    $("message").hidden = false;
  }
}

window.addEventListener("resize", redrawAll);
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redrawAll);
load();
