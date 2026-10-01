const chartDefaults = {
  showBoll: true, showSar: true, showVolume: true,
  showMacd: true, showKdj: true, bollPeriod: 20, bollStd: 2,
  sarStep: .02, sarMax: .2, macdFast: 5, macdSlow: 21,
  macdSignal: 4, kdjPeriod: 13, kdjSmooth: 3,
};
let chartSettings = {...chartDefaults};
try { chartSettings = {...chartDefaults, ...JSON.parse(localStorage.getItem('bottom-chart-settings') || '{}')}; } catch {}
let chartRow = null;
let chartData = null;
let hoverIndex = -1;
let hoverMode = 'normal';
let loadSequence = 0;

const chartFields = {
  showBoll:'show-boll', showSar:'show-sar',
  showVolume:'show-volume', showMacd:'show-macd', showKdj:'show-kdj',
  bollPeriod:'boll-period', bollStd:'boll-std', sarStep:'sar-step', sarMax:'sar-max',
  macdFast:'macd-fast', macdSlow:'macd-slow', macdSignal:'macd-signal',
  kdjPeriod:'kdj-period', kdjSmooth:'kdj-smooth',
};

function fillChartFields() {
  for (const [key, id] of Object.entries(chartFields)) {
    const element = document.getElementById(id);
    if (element.type === 'checkbox') element.checked = Boolean(chartSettings[key]);
    else element.value = chartSettings[key];
  }
}

function readChartFields() {
  const next = {};
  for (const [key, id] of Object.entries(chartFields)) {
    const element = document.getElementById(id);
    if (element.type === 'number' && (!element.value || !element.checkValidity())) throw Error(`${element.previousElementSibling.textContent} 超出允许范围`);
    next[key] = element.type === 'checkbox' ? element.checked : element.type === 'number' ? Number(element.value) : element.value;
  }
  if (next.macdFast >= next.macdSlow) throw Error('MACD Fast 必须小于 Slow');
  if (next.sarStep > next.sarMax) throw Error('SAR Step 不能大于 Max');
  chartSettings = next;
  localStorage.setItem('bottom-chart-settings', JSON.stringify(next));
}

const previewPeriod = document.getElementById('preview-period');
previewPeriod.innerHTML = PERIODS.map(period => `<option value="${period}">${period.toUpperCase()}</option>`).join('');
document.getElementById('preview-exchange').value = settings.exchanges[0] || 'binance';
document.getElementById('preview-market').value = settings.markets[0] || 'spot';
previewPeriod.value = settings.periods[0] || '4h';
let chartUniverse = [];
let chartUniverseStatus = 'idle';
let chartUniverseTaskId = '';
let chartUniverseOptions = null;
let lastUniverseKey = '';
function updateChartUniverse(rows, status, taskId = '', options = null) {
  chartUniverse = rows;
  chartUniverseStatus = status;
  chartUniverseTaskId = taskId;
  chartUniverseOptions = options;
  const exchange = document.getElementById('preview-exchange').value;
  const market = document.getElementById('preview-market').value;
  const select = document.getElementById('preview-pair');
  const previous = select.value || localStorage.getItem('bottom-preview-pair') || '';
  const matching = rows.filter(row => row.exchange === exchange && row.market === market);
  const key = `${taskId}:${exchange}:${market}:${matching.length}`;
  if (key !== lastUniverseKey) {
    select.innerHTML = matching.length
      ? matching.map(row => `<option value="${esc(row.pair)}">${esc(row.pair)} · ${Math.round(row.previous_day_turnover).toLocaleString('en-US')} USDT</option>`).join('')
      : '<option value="">暂无符合门槛的币种</option>';
    if (matching.some(row => row.pair === previous)) select.value = previous;
    lastUniverseKey = key;
  }
  document.getElementById('preview-open').disabled = !matching.length;
  const note = document.getElementById('chart-universe-note');
  const scannedDay = matching[0]?.volume_date || '';
  const scannedFloor = chartUniverseOptions?.min_previous_day_turnover;
  note.textContent = matching.length
    ? `${exchange.toUpperCase()} ${labels[market]}：上次扫描的 ${scannedDay} UTC 成交额 ≥ ${Number(scannedFloor).toLocaleString('en-US')} USDT，共 ${matching.length} 个币种可查看图表。`
    : status === 'running' ? '正在核对前一日成交额；当前市场筛选完成后会列出符合条件的币种。'
    : '当前选择下没有已筛选币种。请先选择交易所、市场和成交额门槛，然后开始扫描。';
}
window.updateChartUniverse = updateChartUniverse;
for (const id of ['preview-exchange','preview-market']) document.getElementById(id).addEventListener('change', () => updateChartUniverse(chartUniverse,chartUniverseStatus,chartUniverseTaskId,chartUniverseOptions));
updateChartUniverse(window.latestUniverse || [], 'idle');
function openPreviewChart() {
  const pair = document.getElementById('preview-pair').value;
  if (!pair || !chartUniverse.some(row => row.exchange === document.getElementById('preview-exchange').value && row.market === document.getElementById('preview-market').value && row.pair === pair)) return;
  try { localStorage.setItem('bottom-preview-pair', pair); } catch {}
  openResultChart({exchange: document.getElementById('preview-exchange').value,
    market: document.getElementById('preview-market').value, pair,
    period: previewPeriod.value});
}
document.getElementById('preview-open').addEventListener('click', openPreviewChart);
document.getElementById('preview-pair').addEventListener('change', event => {
  try { localStorage.setItem('bottom-preview-pair', event.target.value); } catch {}
});
for (const key of ['showBoll','showSar','showVolume','showMacd','showKdj']) {
  document.getElementById(chartFields[key]).addEventListener('change', event => {
    chartSettings[key] = event.target.checked;
    localStorage.setItem('bottom-chart-settings', JSON.stringify(chartSettings));
    drawAllCharts();
  });
}

function chartMessage(message, error = false) {
  const target = document.getElementById('chart-status');
  target.textContent = message;
  target.classList.toggle('error', error);
}

function openResultChart(row) {
  if (!row) return;
  chartRow = row;
  chartData = null;
  hoverIndex = -1;
  hoverMode = 'normal';
  fillChartFields();
  document.getElementById('chart-title').textContent = `${row.pair} · ${row.period.toUpperCase()}${row.stage ? ` · ${row.stage}` : ''}`;
  document.getElementById('chart-subtitle').textContent = `${labels[row.exchange]} ${labels[row.market]}${row.score == null ? '' : ` · 扫描时评分 ${row.score}`} · 两种 K 线同时显示，指标基于原始 K 线计算`;
  document.getElementById('chart-modal').classList.remove('hidden');
  document.body.classList.add('chart-open');
  for (const canvas of document.querySelectorAll('.chart-plot canvas')) {
    canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
  }
  document.getElementById('chart-tooltip').textContent = '将鼠标移到 K 线上查看数值';
  loadChart();
}
window.openResultChart = openResultChart;

function closeChart() {
  document.getElementById('chart-modal').classList.add('hidden');
  document.body.classList.remove('chart-open');
  loadSequence++;
}
document.getElementById('chart-close').addEventListener('click', closeChart);
document.getElementById('chart-modal').addEventListener('click', event => {
  if (event.target.id === 'chart-modal') closeChart();
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !document.getElementById('chart-modal').classList.contains('hidden')) closeChart();
});
document.getElementById('chart-apply').addEventListener('click', () => {
  try { readChartFields(); loadChart(); }
  catch (error) { chartMessage(String(error), true); }
});

async function loadChart() {
  if (!chartRow) return;
  const sequence = ++loadSequence;
  chartMessage('正在读取真实已收盘 K 线并计算指标…');
  const query = new URLSearchParams({
    exchange: chartRow.exchange, market: chartRow.market,
    pair: chartRow.pair, period: chartRow.period,
    boll_period: chartSettings.bollPeriod, boll_std: chartSettings.bollStd,
    sar_step: chartSettings.sarStep, sar_max: chartSettings.sarMax,
    macd_fast: chartSettings.macdFast, macd_slow: chartSettings.macdSlow,
    macd_signal: chartSettings.macdSignal,
    kdj_period: chartSettings.kdjPeriod, kdj_smooth: chartSettings.kdjSmooth,
  });
  try {
    const response = await fetch(`/api/chart?${query}`);
    const body = await response.json();
    if (!response.ok) throw Error(body.detail || `HTTP ${response.status}`);
    if (sequence !== loadSequence) return;
    chartData = body;
    hoverIndex = -1;
    chartMessage(`${body.bars_count} 根已收盘 K 线 · ${body.source === 'scan' ? '扫描时行情' : '重新读取的最新行情（可能与扫描时不同）'} · 图表参数不改变扫描结果`);
    drawAllCharts();
  } catch (error) {
    if (sequence !== loadSequence) return;
    chartData = null;
    chartMessage(`图表加载失败：${error}`, true);
  }
}

const chartColors = {up:'#30c89e',down:'#ee6674',grid:'#253854',text:'#8da3c4',
  mid:'#87a9ff',upper:'#5f83e4',lower:'#5f83e4',sar:'#79d7fa',
  dif:'#65d6ba',dea:'#ffbd71',k:'#f5cd66',d:'#77aaff',j:'#dd85ee'};
function plotCanvas(id, height) {
  const canvas = document.getElementById(id);
  const width = Math.max(250, canvas.clientWidth);
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return {ctx, width, height};
}
function visibleChartBars(width) {
  const bars = chartData.bars;
  const count = Math.min(bars.length, Math.max(30, Math.floor((width-78)/7)));
  const start = bars.length-count;
  return {bars:bars.slice(start), start, step:(width-78)/count, left:12, right:66};
}
function fmtPrice(value) {
  if (value == null || !Number.isFinite(value)) return '—';
  const decimals = Math.abs(value) < .01 ? 8 : Math.abs(value) < 1 ? 6 : Math.abs(value) < 100 ? 3 : 2;
  return Number(value).toLocaleString('en-US', {maximumFractionDigits:decimals});
}
function fmtVolume(value) {
  if (value == null) return '—';
  if (Math.abs(value) >= 1e9) return `${(value/1e9).toFixed(1)}B`;
  if (Math.abs(value) >= 1e6) return `${(value/1e6).toFixed(1)}M`;
  if (Math.abs(value) >= 1e3) return `${(value/1e3).toFixed(1)}K`;
  return fmtPrice(value);
}
function grid(ctx, width, height, yFor, min, max, lines = 4) {
  ctx.lineWidth = 1;
  ctx.font = '10px system-ui';
  for (let i=0;i<=lines;i++) {
    const value = min+(max-min)*i/lines;
    const y = yFor(value);
    ctx.strokeStyle = chartColors.grid;
    ctx.beginPath(); ctx.moveTo(12,y); ctx.lineTo(width-64,y); ctx.stroke();
    ctx.fillStyle = chartColors.text;
    ctx.fillText(fmtPrice(value), width-58, y+3);
  }
}
function series(ctx, bars, field, color, xFor, yFor, dash = []) {
  ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.setLineDash(dash);
  ctx.beginPath(); let started = false;
  bars.forEach((bar,i) => {
    const value = bar[field];
    if (value == null || !Number.isFinite(value)) { started = false; return; }
    if (!started) ctx.moveTo(xFor(i),yFor(value));
    else ctx.lineTo(xFor(i),yFor(value));
    started = true;
  });
  ctx.stroke(); ctx.setLineDash([]);
}
function crosshair(ctx, view, height) {
  if (hoverIndex < view.start || hoverIndex >= view.start+view.bars.length) return;
  const x = view.left+(hoverIndex-view.start+.5)*view.step;
  ctx.strokeStyle = '#d1ddf0aa'; ctx.lineWidth = 1; ctx.setLineDash([3,3]);
  ctx.beginPath(); ctx.moveTo(x,5); ctx.lineTo(x,height-12); ctx.stroke(); ctx.setLineDash([]);
}
function drawPrice(canvasId, mode) {
  const {ctx,width,height} = plotCanvas(canvasId, 325);
  const view = visibleChartBars(width), {bars,step,left} = view;
  const prefix = mode === 'ha' ? 'ha_' : '';
  const values = bars.flatMap(b => [b[`${prefix}low`],b[`${prefix}high`],
    chartSettings.showBoll ? b.boll_lower : null, chartSettings.showBoll ? b.boll_upper : null,
    chartSettings.showSar ? b.sar : null]).filter(v => v != null && Number.isFinite(v));
  if (!values.length) return;
  const low = Math.min(...values), high = Math.max(...values), padding = Math.max((high-low)*.07,Math.abs(high)*.001);
  const min=low-padding, max=high+padding, top=10, bottom=height-25;
  const yFor=v=>top+(max-v)/(max-min)*(bottom-top), xFor=i=>left+(i+.5)*step;
  grid(ctx,width,height,yFor,min,max);
  if (chartSettings.showBoll) {
    series(ctx,bars,'boll_upper',chartColors.upper,xFor,yFor);
    series(ctx,bars,'boll_mid',chartColors.mid,xFor,yFor);
    series(ctx,bars,'boll_lower',chartColors.lower,xFor,yFor);
  }
  bars.forEach((bar,i)=>{
    const open=bar[`${prefix}open`],close=bar[`${prefix}close`],up=close>=open;
    const x=xFor(i),color=up?chartColors.up:chartColors.down;
    ctx.strokeStyle=color;ctx.fillStyle=color;ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(x,yFor(bar[`${prefix}high`]));ctx.lineTo(x,yFor(bar[`${prefix}low`]));ctx.stroke();
    const y=Math.min(yFor(open),yFor(close)), body=Math.max(1,Math.abs(yFor(open)-yFor(close)));
    ctx.fillRect(x-Math.max(1.5,Math.min(5,step*.34)),y,Math.max(3,Math.min(10,step*.68)),body);
    if (chartSettings.showSar && bar.sar != null) {
      ctx.beginPath();ctx.arc(x,yFor(bar.sar),2.2,0,Math.PI*2);ctx.fillStyle=chartColors.sar;ctx.fill();
    }
  });
  ctx.fillStyle=chartColors.text;ctx.font='10px system-ui';
  const every=Math.max(1,Math.floor(bars.length/6));
  for(let i=0;i<bars.length;i+=every)ctx.fillText(new Date(bars[i].time).toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit'}),xFor(i)-12,height-6);
  crosshair(ctx,view,height);
}
function drawVolume() {
  const {ctx,width,height}=plotCanvas('volume-canvas',85),view=visibleChartBars(width),{bars,step,left}=view;
  const max=Math.max(...bars.map(b=>b.volume),1),bottom=height-10;
  bars.forEach((b,i)=>{
    const x=left+(i+.5)*step,h=(b.volume/max)*(height-17);
    ctx.fillStyle=b.close>=b.open?chartColors.up:chartColors.down;
    ctx.fillRect(x-Math.max(1,Math.min(4,step*.3)),bottom-h,Math.max(2,Math.min(8,step*.6)),h);
  });
  ctx.fillStyle=chartColors.text;ctx.font='10px system-ui';ctx.fillText(fmtVolume(max),width-56,13);
  crosshair(ctx,view,height);
}
function drawMacd() {
  const {ctx,width,height}=plotCanvas('macd-canvas',135),view=visibleChartBars(width),{bars,step,left}=view;
  const values=bars.flatMap(b=>[b.macd_hist,b.macd_dif,b.macd_dea]);
  const span=Math.max(Math.abs(Math.min(...values)),Math.abs(Math.max(...values)),1e-9)*1.15;
  const yFor=v=>(height-6)/2-v/span*(height-22)/2,xFor=i=>left+(i+.5)*step;
  grid(ctx,width,height,yFor,-span,span,2);
  bars.forEach((b,i)=>{
    const zero=yFor(0),y=yFor(b.macd_hist),x=xFor(i);
    ctx.fillStyle=b.macd_hist>=0?chartColors.up:chartColors.down;
    ctx.fillRect(x-Math.max(1,Math.min(4,step*.3)),Math.min(zero,y),Math.max(2,Math.min(8,step*.6)),Math.max(1,Math.abs(y-zero)));
  });
  series(ctx,bars,'macd_dif',chartColors.dif,xFor,yFor);
  series(ctx,bars,'macd_dea',chartColors.dea,xFor,yFor);
  crosshair(ctx,view,height);
}
function drawKdj() {
  const {ctx,width,height}=plotCanvas('kdj-canvas',135),view=visibleChartBars(width),{bars,step,left}=view;
  const values=bars.flatMap(b=>[b.kdj_k,b.kdj_d,b.kdj_j]);
  const min=Math.min(-10,...values),max=Math.max(110,...values);
  const yFor=v=>8+(max-v)/(max-min)*(height-19),xFor=i=>left+(i+.5)*step;
  grid(ctx,width,height,yFor,min,max,2);
  for(const level of [20,80]){
    ctx.strokeStyle='#536d91';ctx.setLineDash([4,4]);ctx.beginPath();ctx.moveTo(12,yFor(level));ctx.lineTo(width-64,yFor(level));ctx.stroke();ctx.setLineDash([]);
  }
  series(ctx,bars,'kdj_k',chartColors.k,xFor,yFor);
  series(ctx,bars,'kdj_d',chartColors.d,xFor,yFor);
  series(ctx,bars,'kdj_j',chartColors.j,xFor,yFor);
  crosshair(ctx,view,height);
}
function updateChartTooltip() {
  if (!chartData?.bars?.length) return;
  const bar=chartData.bars[hoverIndex >= 0 ? hoverIndex : chartData.bars.length-1];
  const prefix=hoverIndex >= 0 ? '' : '最新已收盘 · ';
  const mode = hoverMode === 'ha' ? 'ha_' : '';
  document.getElementById('chart-tooltip').textContent = `${prefix}${new Date(bar.time).toLocaleString('zh-CN')}  ${mode ? '平均K线' : '普通K线'} 开 ${fmtPrice(bar[`${mode}open`])}  高 ${fmtPrice(bar[`${mode}high`])}  低 ${fmtPrice(bar[`${mode}low`])}  收 ${fmtPrice(bar[`${mode}close`])}  BOLL中 ${fmtPrice(bar.boll_mid)}  SAR ${fmtPrice(bar.sar)}  MACD柱 ${fmtPrice(bar.macd_hist)}  K/D/J ${bar.kdj_k.toFixed(1)}/${bar.kdj_d.toFixed(1)}/${bar.kdj_j.toFixed(1)}  量 ${fmtVolume(bar.volume)}`;
}
function drawAllCharts() {
  if (!chartData) return;
  document.getElementById('volume-panel').classList.toggle('hidden',!chartSettings.showVolume);
  document.getElementById('macd-panel').classList.toggle('hidden',!chartSettings.showMacd);
  document.getElementById('kdj-panel').classList.toggle('hidden',!chartSettings.showKdj);
  drawPrice('price-canvas','normal');
  drawPrice('ha-canvas','ha');
  if(chartSettings.showMacd)drawMacd();
  if(chartSettings.showKdj)drawKdj();
  if(chartSettings.showVolume)drawVolume();
  updateChartTooltip();
}
for (const [id, mode] of [['price-canvas','normal'],['ha-canvas','ha']]) {
  document.getElementById(id).addEventListener('mousemove',event=>{
    if(!chartData)return;
    const canvas=event.currentTarget,rect=canvas.getBoundingClientRect();
    const view=visibleChartBars(rect.width),x=event.clientX-rect.left;
    hoverIndex=Math.max(view.start,Math.min(chartData.bars.length-1,view.start+Math.floor((x-view.left)/view.step)));
    hoverMode=mode;
    drawAllCharts();
  });
  document.getElementById(id).addEventListener('mouseleave',()=>{hoverIndex=-1;drawAllCharts()});
}
window.addEventListener('resize',()=>{if(chartData&&!document.getElementById('chart-modal').classList.contains('hidden'))drawAllCharts()});
