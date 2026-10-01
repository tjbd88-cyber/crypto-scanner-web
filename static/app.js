const PERIODS=['15m','30m','1h','2h','4h','6h','8h','12h','1d','2d','3d','5d','1w'];
const defaults={exchanges:['binance'],markets:['spot'],periods:['4h'],min_previous_day_turnover:10000000};
const $=id=>document.getElementById(id);
const labels={binance:'Binance',okx:'OKX',gate:'Gate',spot:'现货',perpetual:'永续'};
const stageClass={'观察':'observe','临界':'critical','双转换确认':'confirmed','启动':'launched'};
let settings;
try{settings={...defaults,...JSON.parse(localStorage.getItem('bottom-scan-settings')||'{}')}}catch{settings={...defaults}}
if(!Number.isFinite(settings.min_previous_day_turnover))settings.min_previous_day_turnover=settings.min_24h_turnover;
if(!Number.isFinite(settings.min_previous_day_turnover)||settings.min_previous_day_turnover<0)settings.min_previous_day_turnover=10000000;
function checked(group){return [...$(group).querySelectorAll('input:checked')].map(x=>x.value)}
function renderOptions(){
  $('periods').innerHTML=PERIODS.map(p=>`<label><input type="checkbox" value="${p}">${p.toUpperCase()}</label>`).join('');
  for(const group of ['exchanges','markets','periods']) for(const box of $(group).querySelectorAll('input'))box.checked=settings[group].includes(box.value);
  $('turnover').value=settings.min_previous_day_turnover;
}
function collect(){settings={exchanges:checked('exchanges'),markets:checked('markets'),periods:checked('periods'),min_previous_day_turnover:Math.max(0,Number($('turnover').value)||0)};localStorage.setItem('bottom-scan-settings',JSON.stringify(settings));return settings}
for(const id of ['exchanges','markets','periods','turnover'])$(id).addEventListener('change',collect);
renderOptions();
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function render(rows){const q=$('search').value.toLowerCase().trim();const found=rows.filter(r=>(`${r.pair} ${r.exchange} ${r.market} ${r.period} ${r.stage} ${r.reasons.join(' ')}`).toLowerCase().includes(q));$('count').textContent=rows.length;
 $('results').innerHTML=found.length?found.map(r=>`<tr class="result-row" data-index="${lastRows.indexOf(r)}" title="点击查看K线图"><td><b>${esc(r.pair)}</b><small>${esc(labels[r.exchange])} · ${esc(labels[r.market])}</small></td><td>${esc(r.period.toUpperCase())}</td><td><span class="tag ${stageClass[r.stage]||''}">${esc(r.stage)}</span></td><td><b>${r.score}</b></td><td>${r.decline_bars} 根<small>-${r.decline_pct}%</small></td><td>+${r.rebound_pct}%</td><td>${r.ha_flip_bars_ago===null?'平均K线待翻阳':`HA ${r.ha_flip_bars_ago}根前`}<small>${r.sar_flip_bars_ago===null?'SAR 待翻多':`SAR ${r.sar_flip_bars_ago}根前`}</small></td><td class="reason">${r.reasons.map(esc).join(' · ')}${r.warnings.length?`<small>注意：${r.warnings.map(esc).join(' · ')}</small>`:''}<small>${esc(r.candle_time)}</small></td></tr>`).join(''):'<tr><td colspan="8" class="empty">当前没有符合条件的币种。可调整成交额门槛或扩大扫描范围。</td></tr>'}
let lastRows=[];$('search').addEventListener('input',()=>render(lastRows));
$('results').addEventListener('click',event=>{const row=event.target.closest('tr[data-index]');if(row&&window.openResultChart)window.openResultChart(lastRows[Number(row.dataset.index)])});
async function refresh(){
  try{
    const response=await fetch('/api/task');
    if(!response.ok)throw Error(`HTTP ${response.status}`);
    const data=await response.json();
    lastRows=data.results||[];render(lastRows);
    const state={idle:'待扫描',running:data.phase==='filtering'?'筛选成交额':'扫描中',completed:data.filter_error_count?'部分完成':'已完成',stopped:'已停止',failed:'失败'};
    $('status').textContent=state[data.status]||data.status;
    const filtering=data.status==='running'&&data.phase==='filtering';
    const done=filtering?(data.filter_done||0):(data.done||0);
    const total=filtering?(data.filter_total||0):(data.total||0);
    $('progress').textContent=`${done} / ${total}`;
    $('last-scan').textContent=data.finished_at?new Date(data.finished_at).toLocaleString('zh-CN',{hour:'2-digit',minute:'2-digit',month:'2-digit',day:'2-digit'}):'—';
    $('current').textContent=data.current||({completed:'扫描完成',failed:'扫描失败',stopped:'扫描已停止'}[data.status]||'选择市场与周期后开始扫描');
    const pct=total?Math.min(100,Math.round(100*done/total)):0;
    $('pct').textContent=pct+'%';$('bar').style.width=pct+'%';
    $('error-count').textContent=(data.errors||[]).length;
    $('error-list').innerHTML=(data.errors||[]).map(e=>`<li>${esc(e)}</li>`).join('');
    $('start').disabled=data.status==='running';$('stop').disabled=data.status!=='running';
    window.latestUniverse=data.universe||[];
    if(window.updateChartUniverse)window.updateChartUniverse(window.latestUniverse,data.status,data.id||'',data.options||null);
  }catch(e){
    $('status').textContent='服务未连接';
    $('current').textContent='本机扫描服务未运行，请双击项目目录中的“启动选币器.bat”，然后刷新页面';
    $('start').disabled=true;$('stop').disabled=true;
  }
}
$('start').addEventListener('click',async()=>{try{const body=collect();if(!body.exchanges.length||!body.markets.length||!body.periods.length){alert('请至少选择一个交易所、市场和周期');return}const r=await fetch('/api/task',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok){const j=await r.json();throw Error(j.detail||`HTTP ${r.status}`)}await refresh()}catch(e){alert(e instanceof TypeError?'本机扫描服务已断开。请双击项目目录中的“启动选币器.bat”，然后刷新页面。':`无法开始扫描：${e}`)}});
$('stop').addEventListener('click',async()=>{await fetch('/api/task/stop',{method:'POST'});await refresh()});
refresh();setInterval(refresh,3000);
