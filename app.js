// Общие функции интерфейса и построения графиков (Chart.js)

function toggleTheme() {
  const t = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.bsTheme = t;
  try { localStorage.setItem('theme', t); } catch (e) {}
  location.reload();
}

function toggleCustom(sel) {
  document.querySelectorAll('.custom-range').forEach(el => el.classList.toggle('d-none', sel.value !== 'custom'));
}

const fmt = v => new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 0}).format(v);

if (window.Chart) {
  const dark = document.documentElement.dataset.bsTheme === 'dark';
  Chart.defaults.color = dark ? '#adb5bd' : '#6c757d';
  Chart.defaults.borderColor = dark ? 'rgba(255,255,255,.08)' : 'rgba(0,0,0,.06)';
  Chart.defaults.font.family = '"Segoe UI", system-ui, sans-serif';
}

function barChart(id, labels, datasets, opts = {}) {
  const el = document.getElementById(id); if (!el) return;
  datasets.forEach(d => d.borderRadius = 6);
  return new Chart(el, {
    type: 'bar', data: {labels, datasets},
    options: {
      responsive: true,
      plugins: {legend: {display: opts.legend !== false},
        tooltip: {callbacks: {label: c => `${c.dataset.label}: ${fmt(c.parsed.y)}`}}},
      scales: {y: {beginAtZero: true, ticks: {callback: fmt}}, x: {grid: {display: false}}}
    }
  });
}

function doughnutChart(id, cats) {
  const el = document.getElementById(id); if (!el) return;
  return new Chart(el, {
    type: 'doughnut',
    data: {labels: cats.map(c => c.name),
      datasets: [{data: cats.map(c => c.amount), backgroundColor: cats.map(c => c.color), borderWidth: 2}]},
    options: {cutout: '62%', plugins: {legend: {display: false},
      tooltip: {callbacks: {label: c => `${c.label}: ${fmt(c.parsed)} (${cats[c.dataIndex].share}%)`}}}}
  });
}

function mixedChart(id, d) {
  const el = document.getElementById(id); if (!el) return;
  return new Chart(el, {
    data: {labels: d.labels, datasets: [
      {type: 'line', label: 'Сальдо', data: d.balance, borderColor: '#0d6efd', backgroundColor: '#0d6efd',
       tension: .35, pointRadius: 3, order: 0},
      {type: 'bar', label: 'Доходы', data: d.income, backgroundColor: '#198754', borderRadius: 6, order: 1},
      {type: 'bar', label: 'Расходы', data: d.expense, backgroundColor: '#dc3545', borderRadius: 6, order: 1}]},
    options: {responsive: true, interaction: {mode: 'index', intersect: false},
      plugins: {tooltip: {callbacks: {label: c => `${c.dataset.label}: ${fmt(c.parsed.y)}`}}},
      scales: {y: {ticks: {callback: fmt}}, x: {grid: {display: false}}}}
  });
}
